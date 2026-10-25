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
- Secrets: the session key comes from `ZEDRO_SECRET`, or is generated into `instance/secret_key`.
- Out of scope on purpose: payments, delivery, buyer accounts, reviews, chat, seller sign-up.
