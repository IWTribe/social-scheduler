#!/usr/bin/env python3
"""
One-time Google authorization for the social scheduler (desktop / loopback flow).
Runs on a stock Mac python3 — no packages needed. Needs an OAuth client of type "Desktop app".

Usage:
    python3 setup_google_auth.py --client-id X --client-secret Y --api-key Z [--config config.json]

It opens your browser on Google's consent page; approve, and the refresh token is
written into config.json under "google".
"""
import argparse
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import urllib.error
import webbrowser

SCOPES = " ".join([
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/drive",   # Drive files + Sheets read/write
])
PORT = 8765


def post(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {"error": str(e)}


def get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"error": {"message": f"{e.code} {e.read().decode()[:200]}"}}


class Handler(http.server.BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        Handler.result = {k: v[0] for k, v in q.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "Authorised. You can close this tab and go back to Terminal." if "code" in q \
            else f"Authorisation failed: {q.get('error', ['unknown'])[0]}. Go back to Terminal."
        self.wfile.write(f"<html><body style='font-family:sans-serif;padding:2em'><h2>{msg}</h2></body></html>".encode())

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", required=True)
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--config", default="config.json")
    a = ap.parse_args()

    redirect = f"http://127.0.0.1:{PORT}/"
    state = secrets.token_urlsafe(16)
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": a.client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPES, "access_type": "offline", "prompt": "consent", "state": state,
    })

    srv = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()

    print("\nOpening your browser for Google sign-in. If nothing opens, paste this URL into a browser:\n")
    print(auth_url + "\n")
    webbrowser.open(auth_url)
    print("Waiting for you to approve in the browser...", flush=True)
    # handle_request() returns after the one redirect arrives
    while not Handler.result:
        threading.Event().wait(0.5)

    res = Handler.result
    if "code" not in res:
        sys.exit(f"authorisation failed: {res}")
    if res.get("state") != state:
        sys.exit("state mismatch - run again")

    st, j = post("https://oauth2.googleapis.com/token", {
        "code": res["code"], "client_id": a.client_id, "client_secret": a.client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code"})
    if st != 200 or "refresh_token" not in j:
        sys.exit(f"token exchange failed: {j}")
    print("approved.")

    cfg = {}
    if os.path.exists(a.config):
        with open(a.config) as f:
            cfg = json.load(f)
    cfg["google"] = {"client_id": a.client_id, "client_secret": a.client_secret,
                     "refresh_token": j["refresh_token"], "api_key": a.api_key}
    with open(a.config, "w") as f:
        json.dump(cfg, f, indent=2)

    me = get("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
             {"Authorization": f"Bearer {j['access_token']}"})
    items = me.get("items", [])
    if items:
        print(f"YouTube channel: {items[0]['snippet']['title']}")
    else:
        print(f"note: could not list a YouTube channel for this account ({me.get('error', {}).get('message', me)})")
    print(f"Google credentials saved to {os.path.abspath(a.config)}")


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
One-time Google authorization for the social scheduler (device flow).
Runs on a stock Mac python3 — no packages needed.

Usage:
    python3 setup_google_auth.py --client-id X --client-secret Y --api-key Z [--config config.json]

It prints a code, you open https://www.google.com/device on any device, enter the
code, approve, and the refresh token is written into config.json under "google".
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

SCOPES = " ".join([
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/drive",   # Drive files + Sheets read/write
])


def post(url, data, headers=None):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {"error": str(e)}


def get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"error": {"message": f"{e.code} {e.read().decode()[:200]}"}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", required=True)
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--config", default="config.json")
    a = ap.parse_args()

    st, d = post("https://oauth2.googleapis.com/device/code", {"client_id": a.client_id, "scope": SCOPES})
    if st != 200:
        sys.exit(f"device code request failed: {d}")
    print("\n==============================================")
    print(f"  Open:  {d.get('verification_url', 'https://www.google.com/device')}")
    print(f"  Enter code:  {d['user_code']}")
    print("==============================================\n")
    print("Waiting for approval", end="", flush=True)

    interval = d.get("interval", 5)
    deadline = time.time() + d.get("expires_in", 1800)
    while time.time() < deadline:
        time.sleep(interval)
        st, j = post("https://oauth2.googleapis.com/token", {
            "client_id": a.client_id, "client_secret": a.client_secret,
            "device_code": d["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if "refresh_token" in j:
            print("\napproved.")
            break
        err = j.get("error")
        if err == "authorization_pending":
            print(".", end="", flush=True)
            continue
        if err == "slow_down":
            interval += 5
            continue
        sys.exit(f"\nauthorization failed: {j}")
    else:
        sys.exit("\ncode expired, run again")

    cfg = {}
    if os.path.exists(a.config):
        with open(a.config) as f:
            cfg = json.load(f)
    cfg["google"] = {"client_id": a.client_id, "client_secret": a.client_secret,
                     "refresh_token": j["refresh_token"], "api_key": a.api_key}
    with open(a.config, "w") as f:
        json.dump(cfg, f, indent=2)

    me = get("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
             {"Authorization": f"Bearer {j['access_token']}"})
    items = me.get("items", [])
    if items:
        print(f"YouTube channel: {items[0]['snippet']['title']}")
    else:
        print(f"note: could not list a YouTube channel for this account ({me.get('error', {}).get('message', me)})")
    print(f"Google credentials saved to {os.path.abspath(a.config)}")


if __name__ == "__main__":
    main()
