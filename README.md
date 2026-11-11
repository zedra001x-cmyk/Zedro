# ZEDRO

**See it before you go.** A validation prototype: Ughelli phone stores publish inventory; buyers search, compare and contact the store before visiting.

Stack: Python, Flask, Jinja2, stdlib `sqlite3`, vanilla HTML/CSS/JS. Tests use `unittest`.

## Run

```
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000. On first run the demo database is created and the demo logins are printed **once** in the terminal (random passwords). To regenerate: `python db.py --reset`.

To try it on an Android phone, put the phone on the same Wi-Fi and open `http://<your-computer-ip>:5000`.

Logins: `seller1@demo.zedro` to `seller5@demo.zedro` (one per demo store) and `admin@demo.zedro`.

## Test

```
python -m unittest discover -s tests -v
```

## Notes

- All stores and listings are **demo data** with fictional store names and fake phone numbers. A banner says so on every page. Set `ZEDRO_DEMO=0` to hide it once real stores are onboarded.
- Freshness comes only from each listing's `updated_at`: under 24h 🟢 Available, 24-72h 🟡 Confirm availability, over 72h ⚠️ Needs confirmation. It is not live stock.
- Secrets: the session key comes from `ZEDRO_SECRET_KEY` (the older `ZEDRO_SECRET` is still read), or in demo mode is generated into `instance/secret_key`.
- Out of scope on purpose: payments, delivery, buyer accounts, reviews, chat. Seller sign-up exists only in Raycom pilot mode, behind a private invitation code (see below).


## Raycom pilot

Run the first real-store pilot with `ZEDRO_MODE=raycom`. This seeds only the Raycom store and staff/admin accounts; it intentionally creates **no fake inventory**.

Set `ZEDRO_RAYCOM_PASSWORD` before first startup if you want a known password. Otherwise ZEDRO generates one and prints it once.

```bash
export ZEDRO_MODE=raycom
export ZEDRO_RAYCOM_PASSWORD='choose-a-strong-password'
python app.py
```

Confirm the exact Raycom storefront address and contact number with the business before presenting the pilot publicly.

## Raycom accounts and configuration

Staff accounts are self-service. Nobody has to create or reset employee accounts by hand.

| Variable | Purpose |
| --- | --- |
| `ZEDRO_MODE=raycom` | Enables the Raycom pilot and `/seller/register`. |
| `ZEDRO_SECRET_KEY` | **Required in Raycom mode** (startup fails without it). Long, random, secret, and stable: it signs sessions, so every worker and every restart must use the same value. Changing it logs everyone out. |
| `ZEDRO_SELLER_INVITE_CODE` | Private code staff need to register. Never commit it. If it is unset or empty, registration is refused (it never becomes open). |
| `ZEDRO_RAYCOM_PASSWORD` | Bootstrap password for the seeded seller `raycom@zedro.local` (first start of an empty database only). |
| `ZEDRO_ADMIN_PASSWORD` | Bootstrap password for the seeded admin `admin@zedro.local`. If unset, the admin falls back to `ZEDRO_RAYCOM_PASSWORD`, then to a random password printed once. |
| `ZEDRO_SESSION_SECURE=1` | Marks the session cookie `Secure` (HTTPS only). Use it in production; leave it off for local HTTP. |
| `ZEDRO_TRUST_PROXY=1` | Trust one proxy hop (`X-Forwarded-For`) for the client IP used by the rate limits. Set it only when running behind the proxy you expect (Render). Without it the real socket address is used and forwarded headers are ignored. |
| `ZEDRO_DATABASE` | Path of the SQLite file. Default: `instance/zedro.db`. |
| `ZEDRO_UPLOAD_DIR` | Folder for uploaded images. Default: `instance/uploads`. |

How it works:

- **Registration** (`/seller/register`, Raycom mode only) needs the invitation code. New accounts are always sellers of the Raycom Raymond store; the form cannot choose a role or store. Passwords need at least 10 characters.
- **Account page** (`/account`): any signed-in user can change their own password. Doing so logs them out everywhere, because sessions carry a fingerprint of the current password hash and every request re-checks it.
- **Admin accounts** (`/admin/accounts`): admins can disable and re-enable accounts and reset passwords. A reset shows a temporary password once, in that response only. Only its hash is stored, and the user must change it at their next login. Resetting does not re-enable a disabled account. An admin cannot disable themselves or the last active admin, or reset their own password there.
- A disabled account is rejected on its very next request, not only at login.
- **Rate limits** are stored in SQLite so they hold across workers and restarts: failed logins are limited per IP + email (5 per 15 minutes) and per email across all IPs (30 per hour); registration attempts are limited per IP (10 per hour). Unknown emails count exactly like wrong passwords and get the same message. Temporary lockouts expire by themselves; there is no admin unlock yet.

Database safety: Raycom mode will not start against a database that already has stores but no "Raycom Raymond" store (for example a demo database). It changes nothing and tells you to use a fresh `ZEDRO_DATABASE` path. Existing databases are upgraded in place (`active` and `must_change_password` are added to `user` with `ALTER TABLE`; no rows are touched).

**Persistence is a separate deployment requirement.** SQLite and uploads live on disk. On Render's ordinary filesystem they are wiped on every deploy and restart. `ZEDRO_DATABASE` and `ZEDRO_UPLOAD_DIR` only help if they point into a mounted persistent disk. Setting them to ordinary paths does not give persistence. Until that is in place, and the other items a real launch needs (backups, monitoring), ZEDRO should be treated as a pilot, not as production-ready.
