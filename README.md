# Social Scheduler

Posts short vertical videos from the **Social Post Queue** Google Sheet to YouTube Shorts,
Instagram Reels and a Facebook Page at the time in the sheet.

- `social_scheduler.py` — the poster. Runs every 10 minutes via GitHub Actions (`post.yml`).
- `add-post.yml` — Actions → "Add a post to the queue" → Run workflow: a form to add a row.
- `config.template.json` — shape of the `CONFIG_JSON` repository secret (Settings → Secrets → Actions).
- `setup_google_auth.py`, `setup_meta.py` — one-time helpers, run once on your Mac to fill in credentials.

Row statuses in the sheet: blank/`queued` → `posting` → `posted` / `partial` / `failed` / `skipped-late`.
Retry a row by setting its status back to `queued`.
