#!/usr/bin/env python3
"""
Social Post Scheduler — posts short vertical videos from a Google Sheet queue
to YouTube (Shorts), Instagram (Reels) and a Facebook Page (Reels or video).

Usage:
    python3 social_scheduler.py --config config.json            # post everything due
    python3 social_scheduler.py --config config.json --dry-run  # show what would post
    python3 social_scheduler.py --config config.json --row 5    # post row 5 now (exact-time task uses this)
    python3 social_scheduler.py --config config.json --list     # show the queue
    python3 social_scheduler.py --config config.json --add --time "2026-09-25 18:00" --video clip.mp4 \
        --title "..." --caption "..." --hashtags "#a #b" --platforms all

Only dependency: `requests` (pip install requests).

Sheet layout (first tab, header row 1):
    A post_time   YYYY-MM-DD HH:MM in the configured timezone
    B video       Drive file name (in the Videos folder) or a Drive link
    C title       YouTube title (<=100 chars)
    D caption     Caption for IG/FB, also the YouTube description
    E hashtags    optional, appended to caption/description
    F platforms   "all" or any of youtube, instagram, facebook (comma separated)
    G status      leave blank. Script writes: posting / posted / partial / failed / skipped-late
    H youtube_url
    I instagram_url
    J facebook_url
    K log
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE = "https://www.googleapis.com/drive/v3"
YT_UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
COLS = ["post_time", "video", "title", "caption", "hashtags", "platforms",
        "status", "youtube_url", "instagram_url", "facebook_url", "log"]
STATUS_COL = "G"  # status..log = G..K


# ----------------------------------------------------------------- helpers
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    for key in ("sheet_id", "videos_folder_id", "google"):
        if key not in cfg:
            die(f"config missing '{key}'")
    cfg.setdefault("timezone", "Europe/Budapest")
    cfg.setdefault("sheet_tab", None)  # None = first tab in the spreadsheet
    cfg.setdefault("max_late_hours", 48)
    cfg.setdefault("facebook_format", "reel")
    cfg.setdefault("youtube", {})
    cfg["youtube"].setdefault("privacy", "public")
    cfg["youtube"].setdefault("category_id", "22")
    cfg["youtube"].setdefault("default_tags", [])
    cfg.setdefault("meta", {})
    cfg["meta"].setdefault("graph_version", "v23.0")
    return cfg


class Google:
    def __init__(self, g):
        self.client_id = g["client_id"]
        self.client_secret = g["client_secret"]
        self.refresh_token = g["refresh_token"]
        self.api_key = g.get("api_key")
        self._token = None
        self._exp = 0

    def token(self):
        if self._token and time.time() < self._exp - 60:
            return self._token
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token"})
        if r.status_code != 200:
            die(f"Google token refresh failed: {r.text}")
        d = r.json()
        self._token, self._exp = d["access_token"], time.time() + d.get("expires_in", 3600)
        return self._token

    def h(self):
        return {"Authorization": f"Bearer {self.token()}"}

    # --- Sheets
    def first_tab(self, sheet_id):
        r = requests.get(f"{SHEETS}/{sheet_id}", params={"fields": "sheets.properties.title"}, headers=self.h())
        if r.status_code != 200:
            die(f"Sheet metadata read failed: {r.text}")
        return r.json()["sheets"][0]["properties"]["title"]

    def read_sheet(self, sheet_id, tab):
        # UNFORMATTED_VALUE: dates the sheet auto-converted come back as serial numbers,
        # typed text comes back as-is — both handled by parse_time, independent of locale.
        r = requests.get(f"{SHEETS}/{sheet_id}/values/'{tab}'!A1:K",
                         params={"valueRenderOption": "UNFORMATTED_VALUE"}, headers=self.h())
        if r.status_code != 200:
            die(f"Sheet read failed: {r.text}")
        return r.json().get("values", [])

    def append_row(self, sheet_id, tab, values):
        r = requests.post(f"{SHEETS}/{sheet_id}/values/'{tab}'!A1:K:append",
                          params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                          headers=self.h(), json={"values": [values]})
        if r.status_code != 200:
            die(f"Sheet append failed: {r.text}")
        rng = r.json().get("updates", {}).get("updatedRange", "")
        m = re.search(r"!A(\d+)", rng)
        return int(m.group(1)) if m else None

    def write_row_status(self, sheet_id, tab, row_num, status, yt="", ig="", fb="", note=""):
        rng = f"'{tab}'!{STATUS_COL}{row_num}:K{row_num}"
        r = requests.put(f"{SHEETS}/{sheet_id}/values/{rng}",
                         params={"valueInputOption": "RAW"}, headers=self.h(),
                         json={"values": [[status, yt, ig, fb, note]]})
        if r.status_code != 200:
            log(f"WARNING: could not write status for row {row_num}: {r.text}")

    # --- Drive
    def find_video(self, folder_id, ref):
        m = re.search(r"/d/([A-Za-z0-9_-]{20,})", ref) or re.search(r"[?&]id=([A-Za-z0-9_-]{20,})", ref)
        if m:
            fid = m.group(1)
        elif re.fullmatch(r"[A-Za-z0-9_-]{20,}", ref.strip()):
            fid = ref.strip()
        else:
            name = ref.strip().replace("'", "\\'")
            q = f"name = '{name}' and '{folder_id}' in parents and trashed = false"
            r = requests.get(f"{DRIVE}/files", params={"q": q, "fields": "files(id,name,size,mimeType)"},
                             headers=self.h())
            files = r.json().get("files", []) if r.status_code == 200 else []
            if not files:
                raise RuntimeError(f"video '{ref}' not found in Videos folder")
            fid = files[0]["id"]
        r = requests.get(f"{DRIVE}/files/{fid}", params={"fields": "id,name,size,mimeType"}, headers=self.h())
        if r.status_code != 200:
            raise RuntimeError(f"cannot read Drive file {fid}: {r.text}")
        return r.json()

    def make_public(self, fid):
        r = requests.post(f"{DRIVE}/files/{fid}/permissions", headers=self.h(),
                          json={"role": "reader", "type": "anyone"})
        if r.status_code not in (200, 201):
            raise RuntimeError(f"could not share file publicly: {r.text}")
        return r.json()["id"]

    def make_private(self, fid, perm_id):
        requests.delete(f"{DRIVE}/files/{fid}/permissions/{perm_id}", headers=self.h())

    def public_url(self, fid):
        if not self.api_key:
            raise RuntimeError("google.api_key missing in config (needed for a direct public video URL)")
        return f"{DRIVE}/files/{fid}?alt=media&key={self.api_key}"

    def download(self, fid, dest):
        with requests.get(f"{DRIVE}/files/{fid}", params={"alt": "media"}, headers=self.h(), stream=True) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        return dest

    def upload(self, path, name, folder_id):
        meta = json.dumps({"name": name, "parents": [folder_id]})
        with open(path, "rb") as f:
            files = {"metadata": ("metadata", meta, "application/json; charset=UTF-8"),
                     "file": (name, f, "video/mp4")}
            r = requests.post("https://www.googleapis.com/upload/drive/v3/files",
                              params={"uploadType": "multipart", "fields": "id,name,size"},
                              headers=self.h(), files=files)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Drive upload failed: {r.text[:300]}")
        return r.json()


# ----------------------------------------------------------------- video prep
def probe(path):
    """Return (video_codec, width, height, fps, audio_codec) via ffprobe, or None if unavailable."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "stream=codec_type,codec_name,width,height,r_frame_rate",
                              "-of", "json", path], capture_output=True, text=True, check=True).stdout
    except Exception:
        return None
    v, a = {}, {}
    for s in json.loads(out).get("streams", []):
        if s.get("codec_type") == "video" and not v:
            v = s
        elif s.get("codec_type") == "audio" and not a:
            a = s
    try:
        num, den = v.get("r_frame_rate", "30/1").split("/")
        fps = float(num) / float(den or 1)
    except Exception:
        fps = 30.0
    return v.get("codec_name"), v.get("width"), v.get("height"), fps, a.get("codec_name")


def needs_transcode(info):
    if info is None:
        return False
    vcodec, w, h, fps, acodec = info
    return vcodec != "h264" or (acodec not in (None, "aac")) or fps > 60.5


def transcode(src, dst):
    """Re-encode to the H.264/AAC MP4 that YouTube, Instagram and Facebook all accept."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", src,
           "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-profile:v", "high", "-level", "4.1",
           "-pix_fmt", "yuv420p", "-r", "30", "-movflags", "+faststart",
           "-c:a", "aac", "-b:a", "160k", "-ar", "48000", dst]
    subprocess.run(cmd, check=True)
    return dst


def ensure_compatible(g, cfg, f):
    """Given a Drive file dict, return (file_id, local_path) of an H.264 version, creating and
    uploading '<name>-h264.mp4' next to the original if needed (reused on later runs)."""
    name = f["name"]
    base = re.sub(r"\.mp4$", "", name, flags=re.I)
    if base.endswith("-h264"):
        return f["id"], None
    q = f"name = '{base}-h264.mp4' and '{cfg['videos_folder_id']}' in parents and trashed = false"
    r = requests.get(f"{DRIVE}/files", params={"q": q, "fields": "files(id,name)"}, headers=g.h())
    existing = r.json().get("files", []) if r.status_code == 200 else []
    if existing:
        log(f"using existing converted copy {existing[0]['name']}")
        return existing[0]["id"], None
    src = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
    g.download(f["id"], src)
    info = probe(src)
    if not needs_transcode(info):
        return f["id"], src
    log(f"video is {info[0]}/{info[4]} - converting to H.264/AAC for Meta compatibility")
    dst = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
    transcode(src, dst)
    os.remove(src)
    up = g.upload(dst, f"{base}-h264.mp4", cfg["videos_folder_id"])
    log(f"uploaded converted copy {up['name']} ({int(up.get('size', 0)) / 1e6:.1f} MB)")
    return up["id"], dst


# ----------------------------------------------------------------- YouTube
def post_youtube(g, cfg, path, title, description, tags):
    yt = cfg["youtube"]
    body = {
        "snippet": {"title": title[:100], "description": description[:5000],
                    "tags": tags[:30], "categoryId": yt["category_id"]},
        "status": {"privacyStatus": yt["privacy"], "selfDeclaredMadeForKids": False},
    }
    size = os.path.getsize(path)
    r = requests.post(YT_UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"},
                      headers={**g.h(), "Content-Type": "application/json; charset=UTF-8",
                               "X-Upload-Content-Type": "video/mp4",
                               "X-Upload-Content-Length": str(size)},
                      json=body)
    if r.status_code != 200:
        raise RuntimeError(f"YouTube upload init failed: {r.text}")
    upload_url = r.headers["Location"]
    with open(path, "rb") as f:
        r = requests.put(upload_url, data=f, headers={**g.h(), "Content-Type": "video/mp4",
                                                        "Content-Length": str(size)})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"YouTube upload failed: {r.status_code} {r.text[:500]}")
    vid = r.json()["id"]
    return f"https://youtube.com/shorts/{vid}"


# ----------------------------------------------------------------- Meta
class Meta:
    def __init__(self, m):
        self.token = m["page_access_token"]
        self.page_id = m.get("page_id")
        self.ig_user_id = m.get("ig_user_id")
        self.base = f"https://graph.facebook.com/{m.get('graph_version', 'v23.0')}"

    def _check(self, r, what):
        try:
            d = r.json()
        except Exception:
            raise RuntimeError(f"{what}: non-JSON response {r.status_code} {r.text[:300]}")
        if r.status_code != 200 or "error" in d:
            raise RuntimeError(f"{what}: {d.get('error', d)}")
        return d

    def post_instagram_reel(self, video_url, caption):
        if not self.ig_user_id:
            raise RuntimeError("meta.ig_user_id missing in config")
        d = self._check(requests.post(f"{self.base}/{self.ig_user_id}/media", data={
            "media_type": "REELS", "video_url": video_url, "caption": caption[:2200],
            "share_to_feed": "true", "access_token": self.token}), "IG create container")
        cid = d["id"]
        for _ in range(60):  # up to ~10 min
            time.sleep(10)
            s = self._check(requests.get(f"{self.base}/{cid}", params={
                "fields": "status_code,status", "access_token": self.token}), "IG container status")
            if s.get("status_code") == "FINISHED":
                break
            if s.get("status_code") == "ERROR":
                raise RuntimeError(f"IG processing error: {s}")
        else:
            raise RuntimeError("IG container never finished processing")
        d = self._check(requests.post(f"{self.base}/{self.ig_user_id}/media_publish", data={
            "creation_id": cid, "access_token": self.token}), "IG publish")
        mid = d["id"]
        p = self._check(requests.get(f"{self.base}/{mid}", params={
            "fields": "permalink", "access_token": self.token}), "IG permalink")
        return p.get("permalink", f"instagram media {mid}")

    def post_facebook_reel(self, video_url, description):
        if not self.page_id:
            raise RuntimeError("meta.page_id missing in config")
        d = self._check(requests.post(f"{self.base}/{self.page_id}/video_reels", data={
            "upload_phase": "start", "access_token": self.token}), "FB reel start")
        vid, upload_url = d["video_id"], d["upload_url"]
        r = requests.post(upload_url, headers={"Authorization": f"OAuth {self.token}",
                                               "file_url": video_url})
        self._check(r, "FB reel upload")
        self._check(requests.post(f"{self.base}/{self.page_id}/video_reels", data={
            "upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
            "description": description[:5000], "access_token": self.token}), "FB reel finish")
        for _ in range(60):
            time.sleep(10)
            s = self._check(requests.get(f"{self.base}/{vid}", params={
                "fields": "status", "access_token": self.token}), "FB reel status")
            st = s.get("status", {})
            if st.get("video_status") in ("ready", "published"):
                break
            if st.get("video_status") == "error":
                raise RuntimeError(f"FB reel processing error: {st}")
        return f"https://www.facebook.com/reel/{vid}"

    def post_facebook_video(self, video_url, description):
        if not self.page_id:
            raise RuntimeError("meta.page_id missing in config")
        d = self._check(requests.post(f"{self.base}/{self.page_id}/videos", data={
            "file_url": video_url, "description": description[:5000],
            "access_token": self.token}), "FB video post")
        return f"https://www.facebook.com/{self.page_id}/videos/{d['id']}"


# ----------------------------------------------------------------- queue
def parse_rows(values):
    rows = []
    for i, raw in enumerate(values[1:], start=2):  # row 1 is the header
        vals = list(raw) + [""] * (len(COLS) - len(raw))
        row = dict(zip(COLS, [v.strip() if isinstance(v, str) else v for v in vals]))
        row["_row"] = i
        rows.append(row)
    return rows


def parse_time(s, tz):
    if isinstance(s, (int, float)):  # Google Sheets date serial (days since 1899-12-30)
        base = datetime(1899, 12, 30, tzinfo=tz)
        return (base + timedelta(days=float(s))).replace(second=0, microsecond=0)
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            pass
    raise ValueError(f"unrecognised post_time '{s}' (use YYYY-MM-DD HH:MM)")


def wanted_platforms(s):
    s = (s or "all").lower()
    if "all" in s or not s.strip():
        return {"youtube", "instagram", "facebook"}
    return {p.strip() for p in re.split(r"[,; /]+", s) if p.strip()} & {"youtube", "instagram", "facebook"}


def due_rows(rows, tz, now, max_late_hours, only_row=None, force=False):
    out = []
    for r in rows:
        if only_row and r["_row"] != only_row:
            continue
        if only_row:
            # --row: post this row now (ignore the time); still skip rows already posted/in progress
            # unless --force, so an exact-time task and the hourly run can never double-post.
            if r["status"] in ("", "queued") or force:
                out.append((r, "forced"))
            else:
                log(f"row {only_row} has status '{r['status']}' - not posting (use --force to override)")
            continue
        if r["status"] not in ("", "queued") or not r["post_time"] or not r["video"]:
            continue
        try:
            t = parse_time(r["post_time"], tz)
        except ValueError as e:
            out.append((r, f"bad-time:{e}"))
            continue
        if t > now:
            continue
        if now - t > timedelta(hours=max_late_hours):
            out.append((r, "late"))
            continue
        out.append((r, "due"))
    return out


def process_row(g, meta, cfg, row):
    tab, sid = cfg["sheet_tab"], cfg["sheet_id"]
    n = row["_row"]
    platforms = wanted_platforms(row["platforms"])
    caption = row["caption"]
    if row["hashtags"]:
        caption = f"{caption}\n\n{row['hashtags']}".strip()
    title = row["title"] or (row["caption"].split("\n")[0][:100] if row["caption"] else row["video"])
    tags = cfg["youtube"]["default_tags"] + [t.lstrip("#") for t in re.findall(r"#(\w+)", row["hashtags"] or "")]

    g.write_row_status(sid, tab, n, "posting", note=f"started {datetime.now():%Y-%m-%d %H:%M}")
    results, errors, perm_id, fid = {}, {}, None, None
    tmp = None
    try:
        f = g.find_video(cfg["videos_folder_id"], row["video"])
        log(f"row {n}: video {f['name']} ({int(f.get('size', 0)) / 1e6:.1f} MB) -> {sorted(platforms)}")
        fid, tmp = ensure_compatible(g, cfg, f)   # H.264 copy for Meta; tmp = local file if already fetched

        if platforms & {"instagram", "facebook"}:
            perm_id = g.make_public(fid)
            url = g.public_url(fid)
            if "instagram" in platforms:
                try:
                    results["instagram"] = meta.post_instagram_reel(url, caption)
                    log(f"row {n}: instagram ok {results['instagram']}")
                except Exception as e:
                    errors["instagram"] = str(e); log(f"row {n}: instagram FAILED {e}")
            if "facebook" in platforms:
                try:
                    if cfg["facebook_format"] == "video":
                        results["facebook"] = meta.post_facebook_video(url, caption)
                    else:
                        results["facebook"] = meta.post_facebook_reel(url, caption)
                    log(f"row {n}: facebook ok {results['facebook']}")
                except Exception as e:
                    errors["facebook"] = str(e); log(f"row {n}: facebook FAILED {e}")

        if "youtube" in platforms:
            try:
                if not tmp:
                    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
                    g.download(fid, tmp)
                results["youtube"] = post_youtube(g, cfg, tmp, title, caption, tags)
                log(f"row {n}: youtube ok {results['youtube']}")
            except Exception as e:
                errors["youtube"] = str(e); log(f"row {n}: youtube FAILED {e}")
    except Exception as e:
        errors["setup"] = str(e); log(f"row {n}: FAILED before posting: {e}")
    finally:
        if perm_id and fid:
            g.make_private(fid, perm_id)
        if tmp and os.path.exists(tmp):
            os.remove(tmp)

    if errors and results:
        status = "partial"
    elif errors:
        status = "failed"
    else:
        status = "posted"
    note = f"{datetime.now():%Y-%m-%d %H:%M} " + ("; ".join(f"{k}: {v[:300]}" for k, v in errors.items()) or "ok")
    g.write_row_status(sid, tab, n, status, results.get("youtube", ""), results.get("instagram", ""),
                       results.get("facebook", ""), note)
    return {"row": n, "video": row["video"], "status": status, "results": results, "errors": errors}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--row", type=int, help="post this sheet row now, ignoring its post_time (skips rows already posted)")
    ap.add_argument("--force", action="store_true", help="with --row: post even if the row is already posted/failed")
    ap.add_argument("--list", action="store_true", help="print the queue and exit")
    ap.add_argument("--add", action="store_true", help="append a row: --time --video [--title --caption --hashtags --platforms]")
    ap.add_argument("--time"); ap.add_argument("--video"); ap.add_argument("--title", default="")
    ap.add_argument("--caption", default=""); ap.add_argument("--hashtags", default="")
    ap.add_argument("--platforms", default="all")
    a = ap.parse_args()

    cfg = load_config(a.config)
    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(tz)
    g = Google(cfg["google"])
    meta = Meta(cfg["meta"]) if cfg["meta"].get("page_access_token") else None
    if not cfg["sheet_tab"]:
        cfg["sheet_tab"] = g.first_tab(cfg["sheet_id"])

    if a.add:
        if not a.time or not a.video:
            die("--add needs --time 'YYYY-MM-DD HH:MM' and --video NAME")
        t = parse_time(a.time, tz)
        n = g.append_row(cfg["sheet_id"], cfg["sheet_tab"],
                         [t.strftime("%Y-%m-%d %H:%M"), a.video, a.title, a.caption, a.hashtags, a.platforms, ""])
        print(f"added row {n}: {a.video} at {t:%Y-%m-%d %H:%M %Z} -> {sorted(wanted_platforms(a.platforms))}")
        return

    rows = parse_rows(g.read_sheet(cfg["sheet_id"], cfg["sheet_tab"]))
    if a.list:
        for r in rows:
            print(f"row {r['_row']:>3}  {str(r['post_time']):<17} {r['status'] or 'queued':<13} "
                  f"{r['video'][:40]:<40} {r['platforms'] or 'all'}")
        return
    todo = due_rows(rows, tz, now, cfg["max_late_hours"], a.row, a.force)
    log(f"{len(rows)} rows in queue, {len(todo)} to handle (now {now:%Y-%m-%d %H:%M %Z})")

    summary = []
    for row, why in todo:
        if why.startswith("bad-time"):
            if not a.dry_run:
                g.write_row_status(cfg["sheet_id"], cfg["sheet_tab"], row["_row"], "failed", note=why)
            summary.append({"row": row["_row"], "status": "failed", "errors": {"time": why}})
            continue
        if why == "late":
            if not a.dry_run:
                g.write_row_status(cfg["sheet_id"], cfg["sheet_tab"], row["_row"], "skipped-late",
                                   note=f"post_time more than {cfg['max_late_hours']}h ago; set status to 'queued' to force")
            summary.append({"row": row["_row"], "status": "skipped-late"})
            continue
        if a.dry_run:
            log(f"DRY RUN row {row['_row']}: would post '{row['video']}' to {sorted(wanted_platforms(row['platforms']))}")
            summary.append({"row": row["_row"], "status": "dry-run", "video": row["video"]})
            continue
        needs_meta = bool(wanted_platforms(row["platforms"]) & {"instagram", "facebook"})
        if needs_meta and not meta:
            g.write_row_status(cfg["sheet_id"], cfg["sheet_tab"], row["_row"], "failed",
                               note="meta.page_access_token missing in config")
            summary.append({"row": row["_row"], "status": "failed", "errors": {"config": "no Meta token"}})
            continue
        summary.append(process_row(g, meta, cfg, row))

    print("\nSUMMARY " + json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
