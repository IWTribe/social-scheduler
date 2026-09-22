#!/usr/bin/env python3
"""
One-time Meta (Facebook Page + Instagram) authorization for the social scheduler.
Runs on a stock Mac python3 — no packages needed. The app must be in Development mode
(localhost redirects are allowed then) and you must be an admin/developer of it.

Usage:
    python3 setup_meta.py --app-id X --app-secret Y [--page-id 123] [--config config.json]

It opens your browser on Facebook's permission dialog; approve, and a never-expiring
Page token plus page id / Instagram id are written into config.json under "meta".
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

V = "v23.0"
G = f"https://graph.facebook.com/{V}"
SCOPES = ",".join(["pages_show_list", "pages_read_engagement", "pages_manage_posts",
                   "instagram_basic", "instagram_content_publish", "business_management"])
PORT = 8766


def get(url, **params):
    full = url + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(full, timeout=30) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode())
        except Exception:
            d = {"error": str(e)}
    if "error" in d:
        sys.exit(f"Graph API error: {d['error']}")
    return d


class Handler(http.server.BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        Handler.result = {k: v[0] for k, v in q.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "Authorised. You can close this tab and go back to Terminal." if "code" in q \
            else f"Authorisation failed: {q.get('error_description', q.get('error', ['unknown']))[0]}. Go back to Terminal."
        self.wfile.write(f"<html><body style='font-family:sans-serif;padding:2em'><h2>{msg}</h2></body></html>".encode())

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-id", required=True)
    ap.add_argument("--app-secret", required=True)
    ap.add_argument("--page-id", help="pick this page if you manage several")
    ap.add_argument("--config", default="config.json")
    a = ap.parse_args()

    redirect = f"http://localhost:{PORT}/"
    state = secrets.token_urlsafe(16)
    auth_url = f"https://www.facebook.com/{V}/dialog/oauth?" + urllib.parse.urlencode({
        "client_id": a.app_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPES, "state": state, "auth_type": "rerequest",
    })
    srv = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()

    print("\nOpening your browser for Facebook sign-in. If nothing opens, paste this URL into a browser:\n")
    print(auth_url + "\n")
    webbrowser.open(auth_url)
    print("Waiting for you to approve in the browser...", flush=True)
    while not Handler.result:
        threading.Event().wait(0.5)
    res = Handler.result
    if "code" not in res:
        sys.exit(f"authorisation failed: {res}")
    if res.get("state") != state:
        sys.exit("state mismatch - run again")

    short = get(f"{G}/oauth/access_token", client_id=a.app_id, client_secret=a.app_secret,
                redirect_uri=redirect, code=res["code"])["access_token"]
    ll = get(f"{G}/oauth/access_token", grant_type="fb_exchange_token", client_id=a.app_id,
             client_secret=a.app_secret, fb_exchange_token=short)
    user_token = ll["access_token"]
    print("approved. long-lived user token obtained")

    pages = get(f"{G}/me/accounts", fields="id,name,access_token,instagram_business_account{id,username},connected_instagram_account{id,username}",
                access_token=user_token, limit=100).get("data", [])
    if not pages:
        sys.exit("No Facebook Pages came back. Did you tick the Page in the permission dialog?")
    print("Pages granted:")
    for p in pages:
        ig = p.get("instagram_business_account") or p.get("connected_instagram_account") or {}
        print(f"  - {p['name']}  (page id {p['id']})  IG: {ig.get('username', '— not linked')}")

    page = next((p for p in pages if p["id"] == a.page_id), None) if a.page_id else pages[0]
    if not page:
        sys.exit(f"page id {a.page_id} not in the list above - re-run and tick that Page in the dialog")
    if len(pages) > 1 and not a.page_id:
        print(f"\nUsing the first page ({page['name']}). Re-run with --page-id to choose another.")

    ig = page.get("instagram_business_account") or page.get("connected_instagram_account") or {}
    if not ig:
        print("\nWARNING: no Instagram professional account is linked to this Page - Instagram posting "
              "will fail until you link one (Page settings > Linked accounts).")

    who = get(f"{G}/me", fields="id,name", access_token=page["access_token"])
    print(f"\nPage token OK for: {who['name']}")

    cfg = {}
    if os.path.exists(a.config):
        with open(a.config) as f:
            cfg = json.load(f)
    cfg["meta"] = {"page_id": page["id"], "page_name": page["name"],
                   "page_access_token": page["access_token"],
                   "ig_user_id": ig.get("id", ""), "ig_username": ig.get("username", ""),
                   "graph_version": V}
    with open(a.config, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"Meta credentials saved to {os.path.abspath(a.config)}")


if __name__ == "__main__":
    main()
