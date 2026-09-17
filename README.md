# MikroNet + Lending + Bibi Payment — one app

Everything is now one Flask script (`app.py`), one database (`lending.db`),
one uploads folder, one port. No Fly, no Docker needed for this — it's meant
to run on your PC first.

## Run it

**Windows:** double-click `start-local.ps1` (right-click → Run with PowerShell
the first time, if double-click just opens it in a text editor).

**Mac/Linux:** `./start-local.sh`

Either script creates a `venv/`, installs Flask + waitress into it, and
starts the server at `http://127.0.0.1:<port>` (port comes from
`config.json`, currently `8077`). Leave the window open; close it to stop
the server. Your data lives in `lending.db` and `uploads/` right next to
`app.py` — back those two up, that's the whole business.

If you'd rather do it by hand:
```
python -m venv venv
venv\Scripts\pip install -r requirements.txt      (Windows)
./venv/bin/pip install -r requirements.txt        (Mac/Linux)
python app.py
```

## What's where

- **`/`** — the public MikroNet site (was `mikronet.html`). Anyone can see
  this, no login. Has the "Pay Bill" button up top and the "Sign up in
  minutes" apply form.
- **`/apply`** — where the site's sign-up form now posts (replacing the old
  Formspree webhook). Saves the applicant + their uploaded valid ID straight
  into `lending.db` / `uploads/`.
- **`/pay`** — what the "Pay Bill" button goes to. Sends an already
  logged-in admin to the dashboard, an already logged-in client to their
  portal, and anyone else to the password screen.
- **`/login`** — the one password box for everyone. Your admin password
  (from `config.json`) opens the ledger side. A client's access key (the
  one you hand them, e.g. `AB12-CD34-EF56`) opens their own payment portal
  and nothing else.
- **`/dashboard`** — the admin lending ledger (was at `/` before; moved to
  make room for the public site).
- **`/portal`** — a client's own page: how to pay, upload a screenshot as
  proof, see their payment history. Unchanged from before.
- **`/applications`** (admin only) — every signup from the site, each with
  their uploaded valid ID (photo or PDF), contact info, and a status you can
  set (new / contacted / scheduled / converted / declined). Once you've
  installed someone, add them for real under **Clients** so they get an
  access key and can start paying — this page doesn't do that automatically.
- **Clients / Payments (Review)** (admin only) — unchanged: approve or
  reject the payment screenshots clients send in.

## Admin access, in one place

Log in with the admin password and you can see everything: the lending
ledger, every client's payment screenshots, and every applicant's uploaded
valid ID. Nothing sensitive is reachable without that password (or a
client's own key, which only ever unlocks that one client's own portal).

## Notes / things I didn't touch

- The apply form's ID upload now accepts PDF too (it always said
  `accept="image/*,.pdf"`, but the backend only took images before — fixed).
- Formspree is no longer used by the apply form — everything goes to your
  own database instead. If you still want a copy emailed to you too, say so
  and I can send to both.
- `fly.toml`, `Dockerfile`, `entrypoint.sh` from the old repo weren't carried
  over since you're testing locally for now; the old lending-only routes
  (Borrowers, Cash box, etc.) are all unchanged, just living under this same
  app now.
