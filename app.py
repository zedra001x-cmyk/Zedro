"""ZEDRO validation prototype: Flask + stdlib sqlite3."""
import hmac
import os
import re
import secrets
import sqlite3
import urllib.parse
from datetime import timedelta
from functools import wraps

from flask import (Flask, abort, current_app, flash, g, redirect, render_template,
                   request, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import db as dbm
from db import FMT, now_utc, ts

CONDITIONS = ["New", "Fairly Used", "Used"]
BRANDS = ["Apple", "Samsung", "Tecno", "Infinix", "Xiaomi", "Google", "Itel", "Oppo", "Nokia", "Other"]
DUMMY_HASH = generate_password_hash("not-a-real-password")
LISTING_SELECT = (
    "SELECT l.*, s.name AS store_name, s.whatsapp AS whatsapp, s.phone AS store_phone"
    " FROM listing l JOIN store s ON s.id = l.store_id"
)
PUBLIC_WHERE = "l.status = 'available' AND l.approved = 1 AND s.approved = 1"


# ---------- helpers ----------
def parse_ts(s):
    from datetime import datetime, timezone
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'} ago"


def ago(stamp):
    secs = max(0, int((now_utc() - parse_ts(stamp)).total_seconds()))
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return plural(mins, "minute")
    hours = mins // 60
    if hours < 24:
        return plural(hours, "hour")
    return plural(hours // 24, "day")


def freshness(listing):
    """Freshness comes only from the seller's last update, never from live stock."""
    if listing["status"] == "sold":
        return {"level": "sold", "badge": "Sold", "note": "Marked sold by the seller"}
    hours = (now_utc() - parse_ts(listing["updated_at"])).total_seconds() / 3600
    note = f"Updated {ago(listing['updated_at'])}"
    if hours < 24:
        return {"level": "fresh", "badge": "🟢 Available", "note": note}
    if hours <= 72:
        return {"level": "aging", "badge": "🟡 Confirm availability", "note": note}
    return {"level": "stale", "badge": "⚠️ Needs confirmation", "note": note}


def naira(n):
    return f"₦{int(n):,}"


def phone_name(l):
    brand, model = l["brand"], l["model"]
    if brand.lower() in model.lower() or brand == "Apple":
        return model
    return f"{brand} {model}"


def wa_msg(l):
    return f"Hi, I saw the {phone_name(l)} ({naira(l['price'])}) on ZEDRO. Is it still available?"


def wa_link(number, text=None):
    digits = re.sub(r"\D", "", number or "")
    url = f"https://wa.me/{digits}"
    return url + ("?text=" + urllib.parse.quote(text) if text else "")


def directions_link(store):
    q = f"{store['name']} {store['address'] or 'Ughelli, Delta State'}"
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(q)


def like_escape(term):
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def sniff_image(data):
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def load_secret(app):
    env = os.environ.get("ZEDRO_SECRET")
    if env:
        return env
    path = os.path.join(app.instance_path, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def require_role(role):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if g.user is None:
                return redirect(url_for("login"))
            if g.user["role"] != role:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def parse_listing_form(form):
    """Return (values, errors)."""
    errors = []

    def text(name, label, maxlen, required=False):
        v = (form.get(name) or "").strip()
        if required and not v:
            errors.append(f"{label} is required.")
        if len(v) > maxlen:
            errors.append(f"{label} must be {maxlen} characters or fewer.")
        return v

    vals = {
        "brand": text("brand", "Brand", 40, True),
        "model": text("model", "Model", 80, True),
        "storage": text("storage", "Storage", 20),
        "repair_history": text("repair_history", "Repair history", 300),
        "description": text("description", "Description", 1000),
    }
    cond = (form.get("condition") or "").strip()
    if cond not in CONDITIONS:
        errors.append("Choose a condition: New, Fairly Used or Used.")
    vals["condition"] = cond
    raw = re.sub(r"[,\s₦]", "", form.get("price") or "")
    if not re.fullmatch(r"\d{1,9}", raw) or not (1000 <= int(raw) <= 20_000_000):
        errors.append("Enter a price in naira between 1,000 and 20,000,000.")
        vals["price"] = None
    else:
        vals["price"] = int(raw)
    batt = (form.get("battery_health") or "").strip().rstrip("%")
    if batt:
        if not batt.isdigit() or not (1 <= int(batt) <= 100):
            errors.append("Battery health must be a number from 1 to 100.")
            vals["battery_health"] = None
        else:
            vals["battery_health"] = int(batt)
    else:
        vals["battery_health"] = None
    return vals, errors


def save_image(file, errors):
    if not file or not file.filename:
        return None
    data = file.read()
    ext = sniff_image(data)
    if ext is None or len(data) > 3 * 1024 * 1024:
        errors.append("Image must be a JPG, PNG or WebP under 3 MB.")
        return None
    os.makedirs(current_app.config["UPLOAD_DIR"], exist_ok=True)
    name = f"{secrets.token_hex(8)}.{ext}"
    with open(os.path.join(current_app.config["UPLOAD_DIR"], name), "wb") as f:
        f.write(data)
    return name


def remove_image(name):
    if name and re.fullmatch(r"[0-9a-f]{16}\.(jpg|png|webp)", name):
        try:
            os.remove(os.path.join(current_app.config["UPLOAD_DIR"], name))
        except OSError:
            pass


# ---------- app ----------
def create_app(db_path=None, upload_dir=None, demo=True):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    app.config.update(
        DATABASE=db_path or os.path.join(app.instance_path, "zedro.db"),
        UPLOAD_DIR=upload_dir or os.path.join(app.instance_path, "uploads"),
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
        SESSION_COOKIE_SAMESITE="Lax",
        DEMO=demo and os.environ.get("ZEDRO_DEMO", "1") != "0",
    )
    app.secret_key = load_secret(app)
    conn = sqlite3.connect(app.config["DATABASE"])
    dbm.init_db(conn)
    conn.close()

    app.jinja_env.globals.update(
        freshness=freshness, wa_link=wa_link, wa_msg=wa_msg, directions_link=directions_link,
        csrf_token=csrf_token, CONDITIONS=CONDITIONS, BRANDS=BRANDS)
    app.jinja_env.filters.update(naira=naira, ago=ago, phone_name=phone_name)

    @app.teardown_appcontext
    def close_db(_):
        d = g.pop("db", None)
        if d is not None:
            d.close()

    @app.before_request
    def load_user_and_check_csrf():
        g.user = None
        uid = session.get("uid")
        if uid:
            g.user = get_db().execute("SELECT * FROM user WHERE id = ?", (uid,)).fetchone()
        if request.method == "POST":
            sent = (request.form.get("csrf_token") or "").encode()
            have = (session.get("csrf") or "").encode()
            if not have or not hmac.compare_digest(sent, have):
                abort(400)

    @app.context_processor
    def inject():
        return {"demo": app.config["DEMO"]}

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        return resp

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def error(e):
        return render_template("error.html", code=e.code, name=e.name), e.code

    # ----- buyer -----
    @app.route("/")
    def home():
        popular = ["iPhone 13", "iPhone 12", "Samsung S23", "Samsung A55", "Tecno Camon", "Redmi Note"]
        return render_template("home.html", popular=popular)

    @app.route("/search")
    def search():
        a = request.args
        q = (a.get("q") or "").strip()[:80]
        sql = [LISTING_SELECT, "WHERE", PUBLIC_WHERE]
        params = []
        for term in q.split()[:6]:
            sql.append("AND (l.brand || ' ' || l.model || ' ' || COALESCE(l.storage, '')) LIKE ? ESCAPE '\\'")
            params.append(f"%{like_escape(term)}%")
        brand = (a.get("brand") or "").strip()
        if brand:
            sql.append("AND LOWER(l.brand) = LOWER(?)")
            params.append(brand)
        cond = a.get("condition") or ""
        if cond in CONDITIONS:
            sql.append("AND l.condition = ?")
            params.append(cond)
        storage = (a.get("storage") or "").strip()
        if storage:
            sql.append("AND l.storage = ?")
            params.append(storage)
        for key, op in (("min_price", ">="), ("max_price", "<=")):
            raw = re.sub(r"[,\s₦]", "", a.get(key) or "")
            if raw.isdigit() and len(raw) <= 9:
                sql.append(f"AND l.price {op} ?")
                params.append(int(raw))
        avail = a.get("availability") or ""
        if avail in ("24", "72"):
            sql.append("AND l.updated_at >= ?")
            params.append(ts(now_utc() - timedelta(hours=int(avail))))
        sort = a.get("sort") if a.get("sort") in ("price", "recent") else "recent"
        sql.append("ORDER BY l.price ASC, l.id" if sort == "price" else "ORDER BY l.updated_at DESC, l.id")
        sql.append("LIMIT 60")
        d = get_db()
        results = d.execute(" ".join(sql), params).fetchall()
        brands = [r[0] for r in d.execute(
            "SELECT DISTINCT l.brand FROM listing l JOIN store s ON s.id = l.store_id"
            f" WHERE {PUBLIC_WHERE} ORDER BY 1")]
        storages = [r[0] for r in d.execute(
            "SELECT DISTINCT l.storage FROM listing l JOIN store s ON s.id = l.store_id"
            f" WHERE {PUBLIC_WHERE} AND l.storage IS NOT NULL AND l.storage != ''"
            " ORDER BY CAST(l.storage AS INTEGER)")]
        return render_template("search.html", results=results, q=q, brands=brands, storages=storages,
                               args={"brand": brand, "condition": cond, "storage": storage,
                                     "min_price": a.get("min_price", ""), "max_price": a.get("max_price", ""),
                                     "availability": avail, "sort": sort})

    @app.route("/phone/<int:id>")
    def phone(id):
        l = get_db().execute(LISTING_SELECT + " WHERE l.id = ? AND l.approved = 1 AND s.approved = 1",
                             (id,)).fetchone()
        if l is None:
            abort(404)
        store = get_db().execute("SELECT * FROM store WHERE id = ?", (l["store_id"],)).fetchone()
        return render_template("phone.html", l=l, store=store)

    @app.route("/store/<int:id>")
    def store(id):
        d = get_db()
        s = d.execute("SELECT * FROM store WHERE id = ? AND approved = 1", (id,)).fetchone()
        if s is None:
            abort(404)
        items = d.execute(LISTING_SELECT + f" WHERE l.store_id = ? AND {PUBLIC_WHERE}"
                          " ORDER BY l.updated_at DESC", (id,)).fetchall()
        last = items[0]["updated_at"] if items else None
        return render_template("store.html", s=s, items=items, last=last)

    @app.route("/uploads/<name>")
    def uploaded(name):
        return send_from_directory(current_app.config["UPLOAD_DIR"], name)

    # ----- auth -----
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(url_for("admin_home" if g.user["role"] == "admin" else "dashboard"))
        if request.method == "POST":
            email = (request.form.get("email") or "").strip().lower()[:254]
            pw = request.form.get("password") or ""
            u = get_db().execute("SELECT * FROM user WHERE email = ?", (email,)).fetchone()
            ok = check_password_hash(u["password_hash"] if u else DUMMY_HASH, pw)
            if u and ok:
                session.clear()
                session["uid"] = u["id"]
                return redirect(url_for("admin_home" if u["role"] == "admin" else "dashboard"))
            flash("Email or password is incorrect.")
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("home"))

    # ----- seller -----
    def own_listing(id):
        l = get_db().execute("SELECT * FROM listing WHERE id = ? AND store_id = ?",
                             (id, g.user["store_id"])).fetchone()
        if l is None:
            abort(404)
        return l

    @app.route("/seller")
    @require_role("seller")
    def dashboard():
        d = get_db()
        cutoff = ts(now_utc() - timedelta(hours=72))
        counts = d.execute(
            "SELECT COUNT(*) AS total,"
            " COALESCE(SUM(status = 'available'), 0) AS available,"
            " COALESCE(SUM(status = 'sold'), 0) AS sold,"
            " COALESCE(SUM(status = 'available' AND updated_at < ?), 0) AS needs"
            " FROM listing WHERE store_id = ?", (cutoff, g.user["store_id"])).fetchone()
        items = d.execute(LISTING_SELECT + " WHERE l.store_id = ? ORDER BY l.updated_at DESC",
                          (g.user["store_id"],)).fetchall()
        s = d.execute("SELECT * FROM store WHERE id = ?", (g.user["store_id"],)).fetchone()
        return render_template("dashboard.html", counts=counts, items=items, s=s)

    def save_listing(existing=None):
        vals, errors = parse_listing_form(request.form)
        image = save_image(request.files.get("image"), errors)
        if errors:
            remove_image(image)
            for e in errors:
                flash(e)
            return render_template("listing_form.html", l=existing, form=request.form), 400
        d = get_db()
        now = ts()
        if existing is None:
            cur = d.execute(
                "INSERT INTO listing (store_id, brand, model, storage, condition, price, battery_health,"
                " repair_history, description, image, status, approved, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'available', 1, ?, ?)",
                (g.user["store_id"], vals["brand"], vals["model"], vals["storage"], vals["condition"],
                 vals["price"], vals["battery_health"], vals["repair_history"], vals["description"],
                 image, now, now))
            flash("Published. Buyers can see it now.")
        else:
            if image:
                remove_image(existing["image"])
            d.execute(
                "UPDATE listing SET brand=?, model=?, storage=?, condition=?, price=?, battery_health=?,"
                " repair_history=?, description=?, image=?, updated_at=? WHERE id=?",
                (vals["brand"], vals["model"], vals["storage"], vals["condition"], vals["price"],
                 vals["battery_health"], vals["repair_history"], vals["description"],
                 image or existing["image"], now, existing["id"]))
            flash("Saved.")
        d.commit()
        return redirect(url_for("dashboard"))

    @app.route("/seller/add", methods=["GET", "POST"])
    @require_role("seller")
    def add_listing():
        if request.method == "POST":
            return save_listing()
        return render_template("listing_form.html", l=None, form={})

    @app.route("/seller/listing/<int:id>/edit", methods=["GET", "POST"])
    @require_role("seller")
    def edit_listing(id):
        l = own_listing(id)
        if request.method == "POST":
            return save_listing(l)
        return render_template("listing_form.html", l=l, form={})

    def set_status(id, status, msg):
        l = own_listing(id)
        d = get_db()
        d.execute("UPDATE listing SET status = ?, updated_at = ? WHERE id = ?", (status, ts(), l["id"]))
        d.commit()
        flash(msg)
        return redirect(url_for("dashboard"))

    @app.route("/seller/listing/<int:id>/sold", methods=["POST"])
    @require_role("seller")
    def mark_sold(id):
        return set_status(id, "sold", "Marked sold. It no longer shows in search.")

    @app.route("/seller/listing/<int:id>/available", methods=["POST"])
    @require_role("seller")
    def mark_available(id):
        return set_status(id, "available", "Marked available. Freshness reset to now.")

    @app.route("/seller/listing/<int:id>/delete", methods=["POST"])
    @require_role("seller")
    def delete_listing(id):
        l = own_listing(id)
        d = get_db()
        d.execute("DELETE FROM listing WHERE id = ?", (l["id"],))
        d.commit()
        remove_image(l["image"])
        flash("Deleted.")
        return redirect(url_for("dashboard"))

    # ----- admin -----
    @app.route("/admin")
    @require_role("admin")
    def admin_home():
        d = get_db()
        stores = d.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM listing WHERE store_id = s.id) AS n_listings,"
            " (SELECT COUNT(*) FROM listing WHERE store_id = s.id AND status = 'available') AS n_available"
            " FROM store s ORDER BY s.name").fetchall()
        recent = d.execute(LISTING_SELECT + " ORDER BY l.created_at DESC, l.id DESC LIMIT 100").fetchall()
        return render_template("admin.html", stores=stores, recent=recent)

    def moderate(id, value):
        d = get_db()
        if d.execute("SELECT 1 FROM listing WHERE id = ?", (id,)).fetchone() is None:
            abort(404)
        d.execute("UPDATE listing SET approved = ? WHERE id = ?", (value, id))
        d.commit()
        flash("Listing approved." if value else "Listing disabled. It is hidden from buyers.")
        return redirect(url_for("admin_home"))

    @app.route("/admin/listing/<int:id>/approve", methods=["POST"])
    @require_role("admin")
    def admin_approve(id):
        return moderate(id, 1)

    @app.route("/admin/listing/<int:id>/reject", methods=["POST"])
    @require_role("admin")
    def admin_reject(id):
        return moderate(id, 0)

    return app


if __name__ == "__main__":
    app = create_app()
    c = sqlite3.connect(app.config["DATABASE"])
    created = dbm.seed(c)
    c.close()
    if created:
        print("\nDemo logins (shown once; run `python db.py --reset` to regenerate):")
        for email, role, pw in created:
            print(f"  {role:7} {email}  {pw}")
        print()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
