#!/usr/bin/env python3
"""
One-time Meta (Facebook Page + Instagram) authorization for the social scheduler.
Runs on a stock Mac python3 — no packages needed.

Before running, at https://developers.facebook.com:
  1. My Apps > Create App > type "Business". Note the App ID and App Secret (App settings > Basic).
  2. Make sure your Instagram account is a Professional (Business/Creator) account and is
     linked to your Facebook Page (Page settings > Linked accounts > Instagram).
  3. Open Tools > Graph API Explorer, pick your app, click "Generate Access Token", and grant:
       pages_show_list, pages_read_engagement, pages_manage_posts,
       instagram_basic, instagram_content_publish, business_management
     Copy the short-lived user token it shows.

Usage:
    python3 setup_meta.py --app-id X --app-secret Y --user-token Z [--config config.json]
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error

V = "v23.0"
G = f"https://graph.facebook.com/{V}"


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-id", required=True)
    ap.add_argument("--app-secret", required=True)
    ap.add_argument("--user-token", required=True, help="short-lived user token from Graph API Explorer")
    ap.add_argument("--page-id", help="pick this page if you manage several")
    ap.add_argument("--config", default="config.json")
    a = ap.parse_args()

    ll = get(f"{G}/oauth/access_token", grant_type="fb_exchange_token", client_id=a.app_id,
             client_secret=a.app_secret, fb_exchange_token=a.user_token)
    user_token = ll["access_token"]
    print("long-lived user token obtained")

    pages = get(f"{G}/me/accounts", fields="id,name,access_token,instagram_business_account{id,username}",
                access_token=user_token).get("data", [])
    if not pages:
        sys.exit("No Facebook Pages found for this user. Did you grant pages_show_list?")
    print("Pages you manage:")
    for p in pages:
        ig = p.get("instagram_business_account", {})
        print(f"  - {p['name']}  (page id {p['id']})  IG: {ig.get('username', '— not linked')}")

    page = next((p for p in pages if p["id"] == a.page_id), None) if a.page_id else pages[0]
    if not page:
        sys.exit(f"page id {a.page_id} not in the list above")
    if len(pages) > 1 and not a.page_id:
        print(f"\nUsing the first page ({page['name']}). Re-run with --page-id to choose another.")

    ig = page.get("instagram_business_account", {})
    if not ig:
        print("\nWARNING: no Instagram professional account is linked to this Page — Instagram posting "
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
