"""ZEDRO validation prototype: Flask + stdlib sqlite3."""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
from datetime import timedelta
from functools import wraps

from flask import (Flask, abort, current_app, flash, g, redirect, render_template,
                   request, send_from_directory, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import db as dbm
from db import FMT, now_utc, ts

CONDITIONS = ["New", "Fairly Used", "Used"]
BRANDS = ["Apple", "Samsung", "Tecno", "Infinix", "Xiaomi", "Google", "Itel", "Oppo", "Nokia", "Other"]
DUMMY_HASH = generate_password_hash("not-a-real-password")
MIN_PASSWORD = 10
MAX_PASSWORD = 128
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# Limiter settings: (max attempts, window in seconds).
LOGIN_IP_EMAIL = (5, 900)      # failed logins per client IP + email
LOGIN_EMAIL = (30, 3600)       # failed logins per email across all IPs
REGISTER_IP = (10, 3600)       # registration attempts per client IP
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


def norm_email(raw):
    return (raw or "").strip().lower()[:254]


def limiter_key(*parts):
    """Hash the parts so stored keys are fixed-size whatever the client sends."""
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def pw_fingerprint(secret, password_hash):
    """HMAC of the stored hash: changes whenever the password changes, reveals nothing."""
    key = secret.encode() if isinstance(secret, str) else secret
    return hmac.new(key, b"zedro-pw-fp:" + password_hash.encode(), hashlib.sha256).hexdigest()


def disable_block_reason(d, actor_id, target):
    """Why `target` may not be disabled by `actor_id`, or None if it may.

    Keeps the invariant that at least one active admin always remains. Only
    active admins are counted, so a disabled admin never props the count up.
    """
    if target["id"] == actor_id:
        return "You cannot disable your own account."
    if target["role"] == "admin" and target["active"] == 1:
        n = d.execute("SELECT COUNT(*) FROM user WHERE role = 'admin' AND active = 1").fetchone()[0]
        if n <= 1:
            return "That is the last active admin. Create or enable another admin first."
    return None


def load_secret(app, mode="demo"):
    env = (os.environ.get("ZEDRO_SECRET_KEY") or os.environ.get("ZEDRO_SECRET") or "").strip()
    if env:
        return env
    if mode == "raycom":
        raise RuntimeError(
            "ZEDRO_SECRET_KEY must be set when ZEDRO_MODE=raycom. A generated key would not "
            "survive restarts or be shared between workers.")
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
def create_app(db_path=None, upload_dir=None, demo=True, seed_demo=True, seed_mode=None, trust_proxy=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    app.config.update(
        DATABASE=db_path or os.environ.get("ZEDRO_DATABASE") or os.path.join(app.instance_path, "zedro.db"),
        UPLOAD_DIR=upload_dir or os.environ.get("ZEDRO_UPLOAD_DIR") or os.path.join(app.instance_path, "uploads"),
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=os.environ.get("ZEDRO_SESSION_SECURE") == "1",
        TRUST_PROXY=(os.environ.get("ZEDRO_TRUST_PROXY") == "1") if trust_proxy is None else bool(trust_proxy),
        CLOCK=time.time,
        DEMO=demo and os.environ.get("ZEDRO_DEMO", "1") != "0",
        MODE=seed_mode or os.environ.get("ZEDRO_MODE", "demo"),
    )
    app.secret_key = load_secret(app, app.config["MODE"])
    if app.config["TRUST_PROXY"]:
        # Trust exactly one proxy hop (Render's) for the client IP; nothing else.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)
    db_dir = os.path.dirname(os.path.abspath(app.config["DATABASE"]))
    os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(app.config["DATABASE"])
    dbm.init_db(conn)
    if app.config["MODE"] == "raycom":
        state = dbm.raycom_db_state(conn)
        if state in ("foreign", "ambiguous"):
            conn.close()
            raise RuntimeError(
                "ZEDRO_MODE=raycom found a database that already has stores but "
                + ("no" if state == "foreign" else "more than one")
                + " 'Raycom Raymond' store (likely a demo database). Nothing was changed. "
                "Point ZEDRO_DATABASE at a fresh file for the pilot.")
        if state == "ready" and conn.execute("SELECT COUNT(*) FROM store").fetchone()[0] > 1:
            print("WARNING: this database also contains stores other than Raycom Raymond.")
    if seed_demo:
        if app.config["MODE"] == "raycom":
            created = dbm.seed_raycom(conn, password=os.environ.get("ZEDRO_RAYCOM_PASSWORD"),
                                      admin_password=os.environ.get("ZEDRO_ADMIN_PASSWORD"))
            label = "Raycom pilot logins"
        else:
            created = dbm.seed(conn)
            label = "Demo logins"
        if created:
            print(f"\n{label} (shown once):")
            for email, role, pw in created:
                print(f"  {role:7} {email}  {pw}")
            print()
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

    def client_ip():
        return request.remote_addr or "unknown"

    def now_ts():
        return app.config["CLOCK"]()

    def start_session(u):
        session.clear()
        session["uid"] = u["id"]
        session["pwfp"] = pw_fingerprint(app.secret_key, u["password_hash"])

    # Endpoints a user may reach while a password change is pending.
    FORCED_CHANGE_OK = {"account", "logout", "static"}

    @app.before_request
    def load_user_and_check_csrf():
        g.user = None
        g.session_rejected = False
        uid = session.get("uid")
        if uid:
            u = get_db().execute("SELECT * FROM user WHERE id = ?", (uid,)).fetchone()
            # Validated on every request, not just at login: the account must still
            # exist, be active, and the session must match the current password.
            if (u is not None and u["active"] == 1
                    and hmac.compare_digest(str(session.get("pwfp") or ""),
                                            pw_fingerprint(app.secret_key, u["password_hash"]))):
                g.user = u
            else:
                session.clear()
                g.session_rejected = True
        if g.user is not None and g.user["must_change_password"] and request.endpoint not in FORCED_CHANGE_OK:
            return redirect(url_for("account"))
        if g.session_rejected and request.method == "POST":
            return redirect(url_for("login"))
        if request.method == "POST":
            sent = (request.form.get("csrf_token") or "").encode()
            have = (session.get("csrf") or "").encode()
            if not have or not hmac.compare_digest(sent, have):
                abort(400)

    @app.context_processor
    def inject():
        return {
            "demo": app.config["DEMO"],
            "raycom_pilot": app.config["MODE"] == "raycom",
        }

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://wa.me https://www.google.com;"
        )
        if request.is_secure or current_app.config.get("SESSION_COOKIE_SECURE"):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
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
        pilot_store = None
        if app.config["MODE"] == "raycom":
            pilot_store = get_db().execute(
                "SELECT * FROM store WHERE name = ? AND approved = 1", ("Raycom Raymond",)
            ).fetchone()
        return render_template("home.html", popular=popular, pilot_store=pilot_store)

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
            d = get_db()
            email = norm_email(request.form.get("email"))
            pw = request.form.get("password") or ""
            k_pair = limiter_key("login", client_ip(), email)
            k_email = limiter_key("login", email)
            # Every attempt is recorded up front (inside one transaction, so workers
            # cannot race), and forgotten again on success. Only failures remain.
            allowed = dbm.rl_reserve(d, [
                ("login_ip_email", k_pair, *LOGIN_IP_EMAIL),
                ("login_email", k_email, *LOGIN_EMAIL),
            ], now_ts())
            if not allowed:
                flash("Too many login attempts. Wait a few minutes and try again.")
                return render_template("login.html"), 429
            u = d.execute("SELECT * FROM user WHERE email = ?", (email,)).fetchone()
            ok = check_password_hash(u["password_hash"] if u else DUMMY_HASH, pw)
            if u and ok and u["active"] == 1:
                dbm.rl_clear(d, [("login_ip_email", k_pair), ("login_email", k_email)])
                start_session(u)
                if u["must_change_password"]:
                    return redirect(url_for("account"))
                return redirect(url_for("admin_home" if u["role"] == "admin" else "dashboard"))
            flash("Email or password is incorrect.")
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("home"))

    # ----- account (any signed-in user) -----
    def require_login(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if g.user is None:
                return redirect(url_for("login"))
            return fn(*a, **kw)
        return wrapper

    def password_errors(new, confirm):
        errors = []
        if len(new) < MIN_PASSWORD:
            errors.append(f"New password must be at least {MIN_PASSWORD} characters.")
        if len(new) > MAX_PASSWORD:
            errors.append(f"New password must be {MAX_PASSWORD} characters or fewer.")
        if new != confirm:
            errors.append("The two new passwords do not match.")
        return errors

    @app.route("/account", methods=["GET", "POST"])
    @require_login
    def account():
        d = get_db()
        store_row = None
        if g.user["store_id"]:
            store_row = d.execute("SELECT name FROM store WHERE id = ?", (g.user["store_id"],)).fetchone()
        if request.method == "POST":
            current = request.form.get("current_password") or ""
            new = request.form.get("new_password") or ""
            confirm = request.form.get("confirm_password") or ""
            errors = []
            if not check_password_hash(g.user["password_hash"], current):
                errors.append("Current password is incorrect.")
            errors += password_errors(new, confirm)
            if not errors and hmac.compare_digest(new.encode(), current.encode()):
                errors.append("Choose a new password that is different from the current one.")
            if errors:
                for e in errors:
                    flash(e)
                return render_template("account.html", store=store_row), 400
            d.execute("UPDATE user SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                      (generate_password_hash(new), g.user["id"]))
            d.commit()
            # The hash changed, so every session for this user (other devices too)
            # now fails the fingerprint check. Clear this one and ask for a fresh login.
            session.clear()
            flash("Password changed. Log in with your new password.")
            return redirect(url_for("login"))
        return render_template("account.html", store=store_row)

    # ----- seller self-registration (Raycom pilot only) -----
    if app.config["MODE"] == "raycom":
        @app.route("/seller/register", methods=["GET", "POST"])
        def seller_register():
            if g.user:
                return redirect(url_for("account"))
            if request.method == "GET":
                return render_template("register.html", form={})
            d = get_db()
            if not dbm.rl_reserve(d, [("register_ip", limiter_key("register", client_ip()), *REGISTER_IP)],
                                  now_ts()):
                flash("Too many attempts. Wait a while and try again.")
                return render_template("register.html", form={}), 429
            configured = os.environ.get("ZEDRO_SELLER_INVITE_CODE") or ""
            if not configured:
                # Never fall back to open registration.
                flash("Registration is not available right now.")
                return render_template("register.html", form={}), 403
            supplied = request.form.get("invite_code") or ""
            if not hmac.compare_digest(supplied.encode(), configured.encode()):
                flash("That invitation code is not valid.")
                return render_template("register.html", form={}), 403
            email = norm_email(request.form.get("email"))
            pw = request.form.get("password") or ""
            confirm = request.form.get("confirm_password") or ""
            errors = []
            if not EMAIL_RE.fullmatch(email):
                errors.append("Enter a valid email address.")
            if len(pw) < MIN_PASSWORD:
                errors.append(f"Password must be at least {MIN_PASSWORD} characters.")
            if len(pw) > MAX_PASSWORD:
                errors.append(f"Password must be {MAX_PASSWORD} characters or fewer.")
            if pw != confirm:
                errors.append("The two passwords do not match.")
            if not errors and d.execute("SELECT 1 FROM user WHERE email = ?", (email,)).fetchone():
                errors.append("An account with that email already exists.")
            ids = dbm.raycom_store_ids(d)
            if len(ids) != 1:
                flash("Registration is not available right now.")
                return render_template("register.html", form={}), 503
            if errors:
                for e in errors:
                    flash(e)
                return render_template("register.html", form={"email": email}), 400
            # Role and store are fixed here; nothing in the form can change them.
            try:
                d.execute(
                    "INSERT INTO user (email, password_hash, role, store_id, created_at)"
                    " VALUES (?, ?, 'seller', ?, ?)",
                    (email, generate_password_hash(pw), ids[0], ts()))
                d.commit()
            except sqlite3.IntegrityError:
                d.rollback()
                flash("An account with that email already exists.")
                return render_template("register.html", form={"email": email}), 400
            flash("Account created. You can log in now.")
            return redirect(url_for("login"))

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

    # ----- admin: account management -----
    def target_user(d, id):
        u = d.execute("SELECT * FROM user WHERE id = ?", (id,)).fetchone()
        if u is None:
            abort(404)
        return u

    @app.route("/admin/accounts")
    @require_role("admin")
    def admin_accounts():
        users = get_db().execute(
            "SELECT u.id, u.email, u.role, u.active, u.must_change_password, u.created_at,"
            " s.name AS store_name FROM user u LEFT JOIN store s ON s.id = u.store_id"
            " ORDER BY u.role, u.email").fetchall()
        return render_template("admin_accounts.html", users=users)

    @app.route("/admin/accounts/<int:id>/disable", methods=["POST"])
    @require_role("admin")
    def admin_disable(id):
        d = get_db()
        d.commit()
        d.execute("BEGIN IMMEDIATE")  # count and update under one lock
        try:
            u = target_user(d, id)
            reason = disable_block_reason(d, g.user["id"], u)
            if reason:
                d.rollback()
                flash(reason)
            else:
                d.execute("UPDATE user SET active = 0 WHERE id = ?", (id,))
                d.commit()
                flash(f"Disabled {u['email']}. Their sessions stop on their next request.")
        except Exception:
            d.rollback()
            raise
        return redirect(url_for("admin_accounts"))

    @app.route("/admin/accounts/<int:id>/enable", methods=["POST"])
    @require_role("admin")
    def admin_enable(id):
        d = get_db()
        u = target_user(d, id)
        d.execute("UPDATE user SET active = 1 WHERE id = ?", (id,))
        d.commit()
        flash(f"Enabled {u['email']}.")
        return redirect(url_for("admin_accounts"))

    @app.route("/admin/accounts/<int:id>/reset-password", methods=["POST"])
    @require_role("admin")
    def admin_reset_password(id):
        d = get_db()
        u = target_user(d, id)
        if u["id"] == g.user["id"]:
            flash("Use the Account page to change your own password.")
            return redirect(url_for("admin_accounts"))
        temp = secrets.token_urlsafe(12)
        # Only the hash is stored. `active` is deliberately left as it was, so
        # resetting a disabled account does not re-enable it.
        d.execute("UPDATE user SET password_hash = ?, must_change_password = 1 WHERE id = ?",
                  (generate_password_hash(temp), id))
        d.commit()
        # Shown only in this response: never in a redirect, flash, URL or log.
        resp = app.make_response(render_template("account_reset.html", target=u, temp=temp,
                                                 still_disabled=(u["active"] != 1)))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
