import io
import contextlib
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as dbm  # noqa: E402
from app import create_app, disable_block_reason, freshness, limiter_key, pw_fingerprint  # noqa: E402
from werkzeug.security import check_password_hash  # noqa: E402

PW = "test-pass-123"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dbfile = os.path.join(self.tmp, "t.db")
        self.app = create_app(db_path=self.dbfile, upload_dir=os.path.join(self.tmp, "up"), seed_demo=False)
        self.app.config["TESTING"] = True
        c = sqlite3.connect(self.dbfile)
        dbm.seed(c, password=PW)
        c.close()
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def q(self, sql, *args):
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        try:
            rows = c.execute(sql, args).fetchall()
            c.commit()
            return rows
        finally:
            c.close()

    def post(self, client, url, data=None, **kw):
        with client.session_transaction() as s:
            s["csrf"] = "tok"
        d = dict(data or {})
        d["csrf_token"] = "tok"
        return client.post(url, data=d, **kw)

    def login(self, email="seller1@demo.zedro", password=PW):
        c = self.app.test_client()
        r = self.post(c, "/login", {"email": email, "password": password})
        return c, r

    @staticmethod
    def ids(resp):
        return {int(i) for i in re.findall(r'href="/phone/(\d+)"', resp.get_data(as_text=True))}

    def new_listing(self, client, **over):
        data = {"brand": "Apple", "model": "iPhone 13 Zedtest", "price": "450,000",
                "condition": "Fairly Used", "storage": "128GB", "battery_health": "90"}
        data.update(over)
        return self.post(client, "/seller/add", data)


class BuyerTests(Base):
    def test_01_homepage_loads(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("See it before you go.", html)
        self.assertIn("Search iPhone, Samsung, Tecno...", html)
        self.assertIn("Redmi Note", html)

    def test_02_search_works(self):
        r = self.client.get("/search?q=iPhone+13")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.ids(r))

    def test_03_search_returns_relevant_listings(self):
        r = self.client.get("/search?q=iPhone+13")
        expected = {x["id"] for x in self.q(
            "SELECT id FROM listing WHERE model LIKE 'iPhone 13%' AND status='available' AND approved=1")}
        self.assertEqual(self.ids(r), expected)
        self.assertGreater(len(expected), 3)
        self.assertEqual(self.ids(self.client.get("/search?q=zzzznothing")), set())

    def test_04_condition_filter(self):
        r = self.client.get("/search?q=iPhone&condition=New")
        expected = {x["id"] for x in self.q(
            "SELECT id FROM listing WHERE brand='Apple' AND condition='New' AND status='available'")}
        self.assertTrue(expected)
        self.assertEqual(self.ids(r), expected)

    def test_05_price_filter(self):
        r = self.client.get("/search?min_price=400000&max_price=500000")
        expected = {x["id"] for x in self.q(
            "SELECT id FROM listing WHERE price BETWEEN 400000 AND 500000 AND status='available'")}
        self.assertTrue(expected)
        self.assertEqual(self.ids(r), expected)

    def test_brand_storage_availability_and_sort(self):
        r = self.client.get("/search?brand=Tecno")
        expected = {x["id"] for x in self.q("SELECT id FROM listing WHERE brand='Tecno'")}
        self.assertEqual(self.ids(r), expected)
        r = self.client.get("/search?storage=256GB")
        expected = {x["id"] for x in self.q("SELECT id FROM listing WHERE storage='256GB' AND status='available'")}
        self.assertEqual(self.ids(r), expected)
        r = self.client.get("/search?availability=24")
        cutoff = dbm.ts(dbm.now_utc() - timedelta(hours=24))
        expected = {x["id"] for x in self.q(
            "SELECT id FROM listing WHERE status='available' AND updated_at >= ?", cutoff)}
        self.assertEqual(self.ids(r), expected)
        html = self.client.get("/search?sort=price").get_data(as_text=True)
        prices = [int(p.replace(",", "")) for p in re.findall(r'class="price">₦([\d,]+)', html)]
        self.assertEqual(prices, sorted(prices))
        self.assertGreater(len(prices), 10)

    def test_06_listing_details_load(self):
        lid = self.q("SELECT id FROM listing WHERE status='available' LIMIT 1")[0]["id"]
        r = self.client.get(f"/phone/{lid}")
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        for text in ("WhatsApp store", "Call store", "Get directions", "Battery health", "Repair history"):
            self.assertIn(text, html)
        self.assertEqual(self.client.get("/phone/999999").status_code, 404)

    def test_07_store_page_loads_and_count_from_db(self):
        r = self.client.get("/store/1")
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        n = self.q("SELECT COUNT(*) AS n FROM listing WHERE store_id=1 AND status='available'")[0]["n"]
        self.assertIn(f"<dd>{n}</dd>", html)
        self.assertEqual(len(self.ids(r)), n)
        self.assertEqual(self.client.get("/store/999").status_code, 404)

    def test_demo_banner_and_no_fake_claims(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Demonstration inventory", html)


class FreshnessTests(Base):
    def test_thresholds(self):
        def f(hours, status="available"):
            stamp = dbm.ts(dbm.now_utc() - timedelta(hours=hours))
            return freshness({"status": status, "updated_at": stamp})
        self.assertEqual(f(0.5)["level"], "fresh")
        self.assertIn("minutes ago", f(0.5)["note"])
        self.assertEqual(f(23.5)["level"], "fresh")
        self.assertEqual(f(30)["level"], "aging")
        self.assertEqual(f(71)["level"], "aging")
        self.assertEqual(f(80)["level"], "stale")
        self.assertIn("Needs confirmation", f(80)["badge"])
        self.assertEqual(f(1, "sold")["level"], "sold")


class StartupTests(unittest.TestCase):
    def test_factory_seeds_empty_database_once(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "startup.db")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                create_app(db_path=path, upload_dir=os.path.join(tmp, "uploads"))
            first_output = output.getvalue()
            self.assertIn("seller1@demo.zedro", first_output)

            output.seek(0)
            output.truncate(0)
            with contextlib.redirect_stdout(output):
                create_app(db_path=path, upload_dir=os.path.join(tmp, "uploads"))
            self.assertEqual(output.getvalue(), "")

            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM store").fetchone()[0], 5)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM user").fetchone()[0], 6)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM listing").fetchone()[0], len(dbm.LISTINGS))
            finally:
                conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SellerTests(Base):
    def test_08_seller_login_works(self):
        c, r = self.login()
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/seller"))
        self.assertEqual(c.get("/seller").status_code, 200)

    def test_bad_login_rejected(self):
        c, r = self.login(password="wrong")
        self.assertEqual(r.status_code, 200)
        self.assertIn("incorrect", r.get_data(as_text=True))
        self.assertEqual(c.get("/seller").status_code, 302)

    def test_passwords_are_hashed(self):
        for row in self.q("SELECT password_hash FROM user"):
            self.assertNotIn(PW, row["password_hash"])
            self.assertTrue(row["password_hash"].startswith(("scrypt:", "pbkdf2:")))

    def test_09_seller_creates_listing(self):
        c, _ = self.login()
        r = self.new_listing(c)
        self.assertEqual(r.status_code, 302)
        row = self.q("SELECT * FROM listing WHERE model='iPhone 13 Zedtest'")[0]
        self.assertEqual(row["status"], "available")
        self.assertEqual(row["store_id"], 1)
        self.assertEqual(row["price"], 450000)
        self.assertEqual(row["created_at"], row["updated_at"])
        self.assertIn(row["id"], self.ids(self.client.get("/search?q=Zedtest")))

    def test_create_validation(self):
        c, _ = self.login()
        before = self.q("SELECT COUNT(*) AS n FROM listing")[0]["n"]
        for bad in ({"price": "abc"}, {"price": "5"}, {"condition": "Broken"}, {"brand": ""},
                    {"battery_health": "150"}):
            self.assertEqual(self.new_listing(c, **bad).status_code, 400, bad)
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM listing")[0]["n"], before)

    def test_10_11_edit_updates_updated_at(self):
        c, _ = self.login()
        self.new_listing(c)
        lid = self.q("SELECT id FROM listing WHERE model='iPhone 13 Zedtest'")[0]["id"]
        old = dbm.ts(dbm.now_utc() - timedelta(days=5))
        self.q("UPDATE listing SET updated_at=? WHERE id=?", old, lid)
        r = self.post(c, f"/seller/listing/{lid}/edit", {
            "brand": "Apple", "model": "iPhone 13 Zedtest", "price": "430000", "condition": "Used"})
        self.assertEqual(r.status_code, 302)
        row = self.q("SELECT * FROM listing WHERE id=?", lid)[0]
        self.assertEqual(row["price"], 430000)
        self.assertEqual(row["condition"], "Used")
        self.assertGreater(row["updated_at"], old)

    def test_12_13_mark_sold_hides_then_available_restores(self):
        c, _ = self.login()
        self.new_listing(c)
        lid = self.q("SELECT id FROM listing WHERE model='iPhone 13 Zedtest'")[0]["id"]
        self.post(c, f"/seller/listing/{lid}/sold")
        self.assertEqual(self.q("SELECT status FROM listing WHERE id=?", lid)[0]["status"], "sold")
        self.assertNotIn(lid, self.ids(self.client.get("/search?q=Zedtest")))
        self.assertNotIn(lid, self.ids(self.client.get("/store/1")))
        self.post(c, f"/seller/listing/{lid}/available")
        self.assertIn(lid, self.ids(self.client.get("/search?q=Zedtest")))

    def test_still_available_resets_freshness(self):
        c, _ = self.login()
        lid = self.q("SELECT id FROM listing WHERE store_id=1 AND status='available' LIMIT 1")[0]["id"]
        old = dbm.ts(dbm.now_utc() - timedelta(days=10))
        self.q("UPDATE listing SET updated_at=? WHERE id=?", old, lid)
        self.post(c, f"/seller/listing/{lid}/available")
        self.assertGreater(self.q("SELECT updated_at FROM listing WHERE id=?", lid)[0]["updated_at"], old)

    def test_delete(self):
        c, _ = self.login()
        lid = self.q("SELECT id FROM listing WHERE store_id=1 LIMIT 1")[0]["id"]
        self.post(c, f"/seller/listing/{lid}/delete")
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM listing WHERE id=?", lid)[0]["n"], 0)

    def test_dashboard_counts_match_database(self):
        c, _ = self.login()
        html = c.get("/seller").get_data(as_text=True)
        cutoff = dbm.ts(dbm.now_utc() - timedelta(hours=72))
        total = self.q("SELECT COUNT(*) AS n FROM listing WHERE store_id=1")[0]["n"]
        avail = self.q("SELECT COUNT(*) AS n FROM listing WHERE store_id=1 AND status='available'")[0]["n"]
        sold = self.q("SELECT COUNT(*) AS n FROM listing WHERE store_id=1 AND status='sold'")[0]["n"]
        needs = self.q("SELECT COUNT(*) AS n FROM listing WHERE store_id=1 AND status='available'"
                       " AND updated_at < ?", cutoff)[0]["n"]
        got = re.findall(r"<li><b>(\d+)</b>", html)
        self.assertEqual([int(x) for x in got], [total, avail, sold, needs])
        self.assertGreater(needs, 0)

    def test_seller_cannot_touch_other_store(self):
        c, _ = self.login()
        other = self.q("SELECT id, price FROM listing WHERE store_id=2 LIMIT 1")[0]
        self.assertEqual(c.get(f"/seller/listing/{other['id']}/edit").status_code, 404)
        self.assertEqual(self.post(c, f"/seller/listing/{other['id']}/sold").status_code, 404)
        self.assertEqual(self.post(c, f"/seller/listing/{other['id']}/delete").status_code, 404)
        self.assertEqual(self.q("SELECT status FROM listing WHERE id=?", other["id"])[0]["status"], "available")

    def test_image_upload_and_rejection(self):
        c, _ = self.login()
        r = self.new_listing(c, image=(io.BytesIO(PNG), "p.png"))
        self.assertEqual(r.status_code, 302)
        name = self.q("SELECT image FROM listing WHERE model='iPhone 13 Zedtest'")[0]["image"]
        self.assertTrue(name.endswith(".png"))
        img = self.client.get(f"/uploads/{name}")
        self.assertEqual(img.status_code, 200)
        img.close()
        r = self.new_listing(c, model="Fake img", image=(io.BytesIO(b"<script>x</script>"), "x.png"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM listing WHERE model='Fake img'")[0]["n"], 0)

    def test_user_content_is_escaped(self):
        c, _ = self.login()
        self.new_listing(c, model="<script>alert(1)</script>", description="<b>x</b>")
        html = self.client.get("/search?q=script").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_search_input_with_sql_chars_is_safe(self):
        for q in ("'; DROP TABLE listing; --", "%", "_", "a\\b"):
            self.assertEqual(self.client.get("/search", query_string={"q": q}).status_code, 200)
        self.assertGreater(self.q("SELECT COUNT(*) AS n FROM listing")[0]["n"], 30)


class SecurityTests(Base):
    def test_14_unauthorized_cannot_access_seller_dashboard(self):
        for url in ("/seller", "/seller/add", "/seller/listing/1/edit", "/admin"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertTrue(r.headers["Location"].endswith("/login"), url)
        r = self.post(self.client, "/seller/add", {"brand": "X", "model": "Y", "price": "5000", "condition": "New"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM listing WHERE model='Y'")[0]["n"], 0)

    def test_roles_are_separated(self):
        c, _ = self.login()
        self.assertEqual(c.get("/admin").status_code, 403)
        a, _ = self.login("admin@demo.zedro")
        self.assertEqual(a.get("/seller").status_code, 403)

    def test_csrf_required(self):
        c, _ = self.login()
        r = c.post("/seller/add", data={"brand": "A", "model": "B", "price": "5000", "condition": "New"})
        self.assertEqual(r.status_code, 400)
        r = c.post("/seller/listing/1/sold", data={"csrf_token": "wrong"})
        self.assertEqual(r.status_code, 400)

    def test_15_admin_can_moderate_listings(self):
        a, r = self.login("admin@demo.zedro")
        self.assertTrue(r.headers["Location"].endswith("/admin"))
        html = a.get("/admin").get_data(as_text=True)
        self.assertIn("Demo Mobile Point", html)
        self.assertIn("Recent listings", html)
        lid = self.q("SELECT id FROM listing WHERE status='available' AND model LIKE 'iPhone 13%' LIMIT 1")[0]["id"]
        self.post(a, f"/admin/listing/{lid}/reject")
        self.assertEqual(self.q("SELECT approved FROM listing WHERE id=?", lid)[0]["approved"], 0)
        self.assertNotIn(lid, self.ids(self.client.get("/search?q=iPhone+13")))
        self.assertEqual(self.client.get(f"/phone/{lid}").status_code, 404)
        self.post(a, f"/admin/listing/{lid}/approve")
        self.assertIn(lid, self.ids(self.client.get("/search?q=iPhone+13")))
        self.assertEqual(self.client.get(f"/phone/{lid}").status_code, 200)


# ---------------------------------------------------------------------------
# Account lifecycle: registration, password change, disable/reset, rate limits
# ---------------------------------------------------------------------------
INVITE = "invite-code-for-tests"
APW = "admin-pass-12345"
NEWPW = "brand-new-pass-99"
SECRET = "s" * 40


def flash_texts(resp):
    return re.findall(r'<p class="flash"[^>]*>(.*?)</p>', resp.get_data(as_text=True))


class RaycomBase(unittest.TestCase):
    seller = "raycom@zedro.local"
    admin = "admin@zedro.local"

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"ZEDRO_SECRET_KEY": SECRET, "ZEDRO_SELLER_INVITE_CODE": INVITE})
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in ("ZEDRO_TRUST_PROXY", "ZEDRO_SESSION_SECURE", "ZEDRO_DATABASE", "ZEDRO_UPLOAD_DIR",
                  "ZEDRO_SECRET"):
            os.environ.pop(k, None)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dbfile = os.path.join(self.tmp, "r.db")
        self.app = create_app(db_path=self.dbfile, upload_dir=os.path.join(self.tmp, "up"),
                              seed_demo=False, seed_mode="raycom")
        self.app.config["TESTING"] = True
        c = sqlite3.connect(self.dbfile)
        dbm.seed_raycom(c, password=PW, admin_password=APW)
        c.close()
        self.t = 1_700_000_000
        self.app.config["CLOCK"] = lambda: self.t
        self.client = self.app.test_client()

    def q(self, sql, *args):
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        try:
            rows = c.execute(sql, args).fetchall()
            c.commit()
            return rows
        finally:
            c.close()

    def post(self, client, url, data=None, **kw):
        with client.session_transaction() as s:
            s["csrf"] = "tok"
        d = dict(data or {})
        d["csrf_token"] = "tok"
        return client.post(url, data=d, **kw)

    def login(self, email=None, password=PW, ip="127.0.0.1", client=None):
        c = client or self.app.test_client()
        r = self.post(c, "/login", {"email": email or self.seller, "password": password},
                      environ_base={"REMOTE_ADDR": ip})
        return c, r

    def user(self, email):
        return self.q("SELECT * FROM user WHERE email = ?", email)[0]

    def uid(self, email):
        return self.user(email)["id"]

    def register(self, client=None, **over):
        data = {"email": "new.staff@example.com", "password": "longenough-pass", "confirm_password": "longenough-pass",
                "invite_code": INVITE}
        data.update(over)
        return self.post(client or self.app.test_client(), "/seller/register", data)

    def rl_rows(self, bucket=None):
        if bucket:
            return self.q("SELECT * FROM rate_limit_attempt WHERE bucket = ?", bucket)
        return self.q("SELECT * FROM rate_limit_attempt")


class RegistrationTests(RaycomBase):
    def test_route_absent_outside_raycom_mode(self):
        tmp = tempfile.mkdtemp()
        try:
            app = create_app(db_path=os.path.join(tmp, "d.db"), upload_dir=tmp, seed_demo=False)
            self.assertEqual(app.test_client().get("/seller/register").status_code, 404)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_form_and_login_link(self):
        self.assertEqual(self.client.get("/seller/register").status_code, 200)
        self.assertIn("/seller/register", self.client.get("/login").get_data(as_text=True))

    def test_valid_registration_creates_raycom_seller(self):
        r = self.register(email="  New.Staff@Example.COM ")
        self.assertEqual(r.status_code, 302)
        u = self.user("new.staff@example.com")
        raycom = self.q("SELECT id FROM store WHERE name = 'Raycom Raymond'")[0]["id"]
        self.assertEqual((u["role"], u["store_id"], u["active"], u["must_change_password"]),
                         ("seller", raycom, 1, 0))
        self.assertTrue(u["password_hash"].startswith(("scrypt:", "pbkdf2:")))
        self.assertNotIn("longenough-pass", u["password_hash"])

    def test_new_seller_can_log_in_and_sees_raycom(self):
        self.register()
        c, r = self.login("new.staff@example.com", "longenough-pass")
        self.assertEqual(r.status_code, 302)
        page = c.get("/seller")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Raycom Raymond", page.get_data(as_text=True))

    def test_invalid_invite_rejected(self):
        r = self.register(invite_code="wrong-code")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM user WHERE email = 'new.staff@example.com'")[0]["n"], 0)
        r = self.register(invite_code="")
        self.assertEqual(r.status_code, 403)

    def test_missing_or_empty_invite_configuration_never_opens_registration(self):
        for value in (None, ""):
            with mock.patch.dict(os.environ):
                os.environ.pop("ZEDRO_SELLER_INVITE_CODE", None)
                if value is not None:
                    os.environ["ZEDRO_SELLER_INVITE_CODE"] = value
                for code in ("", "anything"):
                    r = self.register(invite_code=code)
                    self.assertEqual(r.status_code, 403, (value, code))
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM user")[0]["n"], 2)

    def test_duplicate_email_rejected_case_insensitively(self):
        self.assertEqual(self.register().status_code, 302)
        r = self.register(email="NEW.STAFF@example.com")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM user WHERE email LIKE 'new.staff@%'")[0]["n"], 1)
        r = self.register(email=self.seller)
        self.assertEqual(r.status_code, 400)

    def test_weak_password_and_mismatch_rejected(self):
        self.assertEqual(self.register(password="short", confirm_password="short").status_code, 400)
        self.assertEqual(self.register(password="longenough-pass", confirm_password="different-pass1").status_code, 400)
        self.assertEqual(self.register(email="not-an-email").status_code, 400)
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM user")[0]["n"], 2)

    def test_cannot_choose_role_or_store(self):
        self.register(role="admin", store_id="999", **{"store": "Other"})
        u = self.user("new.staff@example.com")
        raycom = self.q("SELECT id FROM store WHERE name = 'Raycom Raymond'")[0]["id"]
        self.assertEqual((u["role"], u["store_id"]), ("seller", raycom))
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM store")[0]["n"], 1)

    def test_registration_needs_csrf(self):
        r = self.client.post("/seller/register", data={"email": "x@y.zz", "password": "longenough-pass",
                                                      "confirm_password": "longenough-pass", "invite_code": INVITE})
        self.assertEqual(r.status_code, 400)

    def test_registration_refused_if_raycom_store_missing(self):
        self.q("DELETE FROM user WHERE role = 'seller'")
        self.q("UPDATE store SET name = 'Something Else'")
        r = self.register()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM store")[0]["n"], 1)

    def test_registration_rate_limited_by_ip(self):
        for i in range(10):
            self.register(email=f"u{i}@example.com", invite_code="wrong")
        r = self.register()
        self.assertEqual(r.status_code, 429)
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM user WHERE email = 'new.staff@example.com'")[0]["n"], 0)
        self.t += 3601
        self.assertEqual(self.register().status_code, 302)


class LoginLimiterTests(RaycomBase):
    def fail(self, email=None, ip="127.0.0.1"):
        return self.login(email or self.seller, "wrong-password", ip)[1]

    def test_ip_email_limit_blocks_even_correct_password(self):
        for _ in range(5):
            self.assertEqual(self.fail().status_code, 200)
        self.assertEqual(self.fail().status_code, 429)
        c, r = self.login()
        self.assertEqual(r.status_code, 429)
        self.assertEqual(c.get("/seller").status_code, 302)

    def test_other_email_or_ip_not_affected(self):
        for _ in range(6):
            self.fail()
        self.assertEqual(self.login(self.admin, APW)[1].status_code, 302)       # same IP, other email
        self.assertEqual(self.login(self.seller, PW, ip="10.0.0.9")[1].status_code, 302)  # other IP, same email

    def test_unknown_and_wrong_password_look_identical(self):
        wrong = self.fail(self.seller)
        unknown = self.fail("nobody@example.com")
        self.assertEqual(wrong.status_code, unknown.status_code)
        self.assertEqual(flash_texts(wrong), flash_texts(unknown))
        self.assertEqual(flash_texts(wrong), ["Email or password is incorrect."])
        self.assertNotIn("exist", wrong.get_data(as_text=True).lower().replace("incorrect", ""))

    def test_unknown_email_attempts_are_rate_limited_and_lockout_looks_identical(self):
        for _ in range(5):
            self.assertEqual(self.fail("nobody@example.com").status_code, 200)
        locked_unknown = self.fail("nobody@example.com")
        for _ in range(5):
            self.fail(self.seller)
        locked_real = self.fail(self.seller)
        self.assertEqual((locked_unknown.status_code, locked_real.status_code), (429, 429))
        self.assertEqual(flash_texts(locked_unknown), flash_texts(locked_real))

    def test_failures_accumulate_and_success_resets(self):
        for _ in range(4):
            self.fail()
        self.assertEqual(len(self.rl_rows("login_ip_email")), 4)
        self.assertEqual(self.login()[1].status_code, 302)
        self.assertEqual(len(self.rl_rows()), 0)          # success left nothing behind
        for _ in range(5):                                 # fresh budget after the reset
            self.assertEqual(self.fail().status_code, 200)
        self.assertEqual(self.fail().status_code, 429)

    def test_success_does_not_consume_or_touch_other_accounts(self):
        self.fail(self.admin)
        for _ in range(3):
            self.assertEqual(self.login()[1].status_code, 302)
        rows = self.rl_rows()                              # only the admin's one failure remains
        self.assertEqual(sorted(r["bucket"] for r in rows), ["login_email", "login_ip_email"])
        self.assertEqual({r["key"] for r in rows},
                         {limiter_key("login", "127.0.0.1", self.admin), limiter_key("login", self.admin)})

    def test_email_limit_applies_across_ips(self):
        for i in range(30):
            self.assertEqual(self.fail("target@example.com", ip=f"10.1.0.{i}").status_code, 200)
        self.assertEqual(self.fail("target@example.com", ip="10.2.0.1").status_code, 429)
        self.assertEqual(self.login(self.seller, PW, ip="10.2.0.1")[1].status_code, 302)

    def test_windows_expire_and_old_rows_are_pruned(self):
        for _ in range(5):
            self.fail()
        self.assertEqual(self.fail().status_code, 429)
        self.t += 901                                      # ip+email window (15 min) has passed
        self.assertEqual(self.fail().status_code, 200)
        for i in range(30):
            self.fail("slow@example.com", ip=f"10.3.0.{i}")
        self.t += 3601                                     # hourly email window has passed
        self.assertEqual(self.fail("slow@example.com", ip="10.4.0.1").status_code, 200)
        rows = self.rl_rows()                              # everything older was pruned; only this
        self.assertEqual(sorted(r["bucket"] for r in rows), ["login_email", "login_ip_email"])   # failure remains
        self.assertEqual({r["at"] for r in rows}, {self.t})

    def test_keys_do_not_store_plaintext_email_or_ip(self):
        self.fail("someone@example.com", ip="10.9.9.9")
        for row in self.rl_rows():
            self.assertNotIn("someone", row["key"])
            self.assertNotIn("10.9.9.9", row["key"])
            self.assertEqual(len(row["key"]), 64)

    def test_long_email_is_bounded(self):
        self.fail("a" * 5000 + "@example.com")
        self.assertTrue(all(len(r["key"]) == 64 for r in self.rl_rows()))

    def test_proxy_headers_ignored_by_default(self):
        self.assertFalse(self.app.config["TRUST_PROXY"])
        c = self.app.test_client()
        self.post(c, "/login", {"email": self.seller, "password": "x"},
                  headers={"X-Forwarded-For": "9.9.9.9"}, environ_base={"REMOTE_ADDR": "127.0.0.1"})
        keys = {r["key"] for r in self.rl_rows("login_ip_email")}
        self.assertEqual(keys, {limiter_key("login", "127.0.0.1", self.seller)})

    def test_proxy_headers_used_only_when_trusted(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "p.db")
        app = create_app(db_path=path, upload_dir=tmp, seed_demo=False, seed_mode="raycom", trust_proxy=True)
        c = sqlite3.connect(path)
        dbm.seed_raycom(c, password=PW)
        c.close()
        cl = app.test_client()
        with cl.session_transaction() as s:
            s["csrf"] = "tok"
        cl.post("/login", data={"email": self.seller, "password": "x", "csrf_token": "tok"},
                headers={"X-Forwarded-For": "9.9.9.9"}, environ_base={"REMOTE_ADDR": "127.0.0.1"})
        c = sqlite3.connect(path)
        keys = {r[0] for r in c.execute("SELECT key FROM rate_limit_attempt WHERE bucket = 'login_ip_email'")}
        c.close()
        self.assertEqual(keys, {limiter_key("login", "9.9.9.9", self.seller)})

    def test_trust_proxy_env_flag(self):
        with mock.patch.dict(os.environ, {"ZEDRO_TRUST_PROXY": "1"}):
            tmp = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, tmp, True)
            app = create_app(db_path=os.path.join(tmp, "e.db"), upload_dir=tmp, seed_demo=False, seed_mode="raycom")
            self.assertTrue(app.config["TRUST_PROXY"])


class PasswordChangeTests(RaycomBase):
    def change(self, client, current=PW, new=NEWPW, confirm=None):
        return self.post(client, "/account", {"current_password": current, "new_password": new,
                                              "confirm_password": new if confirm is None else confirm})

    def test_account_page_shows_email_and_role(self):
        c, _ = self.login()
        html = c.get("/account").get_data(as_text=True)
        self.assertIn(self.seller, html)
        self.assertIn("seller", html)
        self.assertEqual(self.client.get("/account").status_code, 302)

    def test_change_password_end_to_end(self):
        c, _ = self.login()
        other, _ = self.login()                       # a second device
        self.assertEqual(other.get("/seller").status_code, 200)
        r = self.change(c)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/login"))
        self.assertEqual(c.get("/seller").status_code, 302)       # this session cleared
        self.assertEqual(other.get("/seller").status_code, 302)   # other device invalidated
        self.assertEqual(self.login(password=PW)[1].status_code, 200)       # old password fails
        c2, r = self.login(password=NEWPW)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c2.get("/seller").status_code, 200)
        self.assertEqual(self.user(self.seller)["must_change_password"], 0)

    def test_stolen_old_cookie_stops_working(self):
        c, _ = self.login()
        with c.session_transaction() as s:
            old = dict(s)
        self.change(c)
        thief = self.app.test_client()
        with thief.session_transaction() as s:
            s.update(old)
        self.assertEqual(thief.get("/seller").status_code, 302)

    def test_validation_failures_change_nothing(self):
        c, _ = self.login()
        before = self.user(self.seller)["password_hash"]
        self.assertEqual(self.change(c, current="wrong-current").status_code, 400)
        self.assertEqual(self.change(c, new="short").status_code, 400)
        self.assertEqual(self.change(c, confirm="different-pass-1").status_code, 400)
        self.assertEqual(self.change(c, new=PW + "x" * 0, confirm=PW).status_code, 400)  # same as current
        self.assertEqual(self.change(c, current="").status_code, 400)
        self.assertEqual(self.user(self.seller)["password_hash"], before)
        self.assertEqual(c.get("/seller").status_code, 200)       # still signed in

    def test_works_for_admin(self):
        a, _ = self.login(self.admin, APW)
        self.assertEqual(self.change(a, current=APW).status_code, 302)
        self.assertEqual(self.login(self.admin, APW)[1].status_code, 200)
        self.assertEqual(self.login(self.admin, NEWPW)[1].status_code, 302)

    def test_account_post_needs_csrf(self):
        c, _ = self.login()
        r = c.post("/account", data={"current_password": PW, "new_password": NEWPW, "confirm_password": NEWPW})
        self.assertEqual(r.status_code, 400)

    def test_fingerprint_properties(self):
        h1 = self.user(self.seller)["password_hash"]
        h2 = self.user(self.admin)["password_hash"]
        fp = pw_fingerprint(SECRET, h1)
        self.assertEqual(fp, pw_fingerprint(SECRET, h1))
        self.assertNotEqual(fp, pw_fingerprint(SECRET, h2))
        self.assertNotEqual(fp, pw_fingerprint("another-secret", h1))
        self.assertNotIn(h1, fp)
        c, _ = self.login()
        with c.session_transaction() as s:
            self.assertEqual(s["pwfp"], fp)
            self.assertNotIn(h1, str(dict(s)))


class DisabledAccountTests(RaycomBase):
    def admin_client(self):
        return self.login(self.admin, APW)[0]

    def test_admin_can_disable_and_enable_seller(self):
        a = self.admin_client()
        sid = self.uid(self.seller)
        self.assertEqual(self.post(a, f"/admin/accounts/{sid}/disable").status_code, 302)
        self.assertEqual(self.user(self.seller)["active"], 0)
        self.assertEqual(self.login()[1].status_code, 200)          # cannot log in
        self.assertEqual(self.post(a, f"/admin/accounts/{sid}/enable").status_code, 302)
        self.assertEqual(self.user(self.seller)["active"], 1)
        self.assertEqual(self.login()[1].status_code, 302)

    def test_existing_session_rejected_on_next_request(self):
        s, _ = self.login()
        self.assertEqual(s.get("/seller").status_code, 200)
        a = self.admin_client()
        self.post(a, f"/admin/accounts/{self.uid(self.seller)}/disable")
        r = s.get("/seller")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/login"))
        with s.session_transaction() as sess:
            self.assertNotIn("uid", sess)                           # session cleared
        self.assertEqual(s.get("/seller").status_code, 302)

    def test_disabled_users_post_is_redirected_not_executed(self):
        s, _ = self.login()
        self.post(self.admin_client(), f"/admin/accounts/{self.uid(self.seller)}/disable")
        r = self.post(s, "/seller/add", {"brand": "Apple", "model": "Ghost", "price": "5000", "condition": "New"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM listing WHERE model = 'Ghost'")[0]["n"], 0)

    def test_user_deleted_while_signed_in_is_rejected(self):
        s, _ = self.login()
        self.q("DELETE FROM user WHERE email = ?", self.seller)
        self.assertEqual(s.get("/seller").status_code, 302)

    def test_admin_cannot_disable_self(self):
        a = self.admin_client()
        self.post(a, f"/admin/accounts/{self.uid(self.admin)}/disable")
        self.assertEqual(self.user(self.admin)["active"], 1)
        self.assertEqual(a.get("/admin").status_code, 200)

    def test_last_active_admin_rule(self):
        self.q("INSERT INTO user (email, password_hash, role, created_at, active) VALUES"
               " ('admin2@zedro.local', 'x', 'admin', '2026-01-01T00:00:00', 1)")
        self.q("INSERT INTO user (email, password_hash, role, created_at, active) VALUES"
               " ('admin3@zedro.local', 'x', 'admin', '2026-01-01T00:00:00', 0)")
        a = self.admin_client()
        a2, a3 = self.uid("admin2@zedro.local"), self.uid("admin3@zedro.local")
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        me = self.uid(self.admin)
        row = lambda i: c.execute("SELECT * FROM user WHERE id = ?", (i,)).fetchone()  # noqa: E731
        # two active admins: another one may be disabled
        self.assertIsNone(disable_block_reason(c, me, row(a2)))
        self.post(a, f"/admin/accounts/{a2}/disable")
        self.assertEqual(self.user("admin2@zedro.local")["active"], 0)
        # disabled admins do not count: now exactly one active admin remains
        active = lambda: self.q("SELECT COUNT(*) AS n FROM user WHERE role='admin' AND active=1")[0]["n"]  # noqa: E731
        self.assertEqual(active(), 1)
        # an actor other than the sole active admin may not disable them
        self.assertIn("last active admin", disable_block_reason(c, a2, row(me)))
        # self-disable is refused, and the count never reaches zero
        self.post(a, f"/admin/accounts/{me}/disable")
        self.assertEqual(active(), 1)
        # resetting a disabled admin does not reactivate it
        self.post(a, f"/admin/accounts/{a3}/reset-password")
        self.assertEqual(active(), 1)
        self.assertEqual(self.user("admin3@zedro.local")["active"], 0)
        # enabling can only raise the count
        self.post(a, f"/admin/accounts/{a3}/enable")
        self.assertEqual(active(), 2)
        c.close()

    def test_accounts_page_contents_and_access(self):
        a = self.admin_client()
        html = a.get("/admin/accounts").get_data(as_text=True)
        for needle in (self.seller, self.admin, "Raycom Raymond", "Active", "seller", "admin"):
            self.assertIn(needle, html)
        s, _ = self.login()
        self.assertEqual(s.get("/admin/accounts").status_code, 403)
        self.assertEqual(self.client.get("/admin/accounts").status_code, 302)

    def test_account_actions_need_csrf_and_admin(self):
        a = self.admin_client()
        sid = self.uid(self.seller)
        for action in ("disable", "enable", "reset-password"):
            self.assertEqual(a.post(f"/admin/accounts/{sid}/{action}", data={"csrf_token": "bad"}).status_code, 400)
        s, _ = self.login()
        self.assertEqual(self.post(s, f"/admin/accounts/{self.uid(self.admin)}/disable").status_code, 403)
        self.assertEqual(self.user(self.admin)["active"], 1)


class AdminResetTests(RaycomBase):
    def reset(self, email=None):
        a = self.login(self.admin, APW)[0]
        r = self.post(a, f"/admin/accounts/{self.uid(email or self.seller)}/reset-password")
        return a, r

    @staticmethod
    def temp_from(resp):
        return re.search(r'class="price"[^>]*>([^<]+)<', resp.get_data(as_text=True)).group(1).strip()

    def test_reset_shows_temp_password_once_and_stores_only_hash(self):
        before = self.user(self.seller)
        a, r = self.reset()
        self.assertEqual(r.status_code, 200)
        self.assertIn("no-store", r.headers["Cache-Control"])
        temp = self.temp_from(r)
        self.assertGreaterEqual(len(temp), 12)
        after = self.user(self.seller)
        self.assertNotEqual(after["password_hash"], before["password_hash"])
        self.assertEqual(after["must_change_password"], 1)
        self.assertTrue(check_password_hash(after["password_hash"], temp))
        for value in tuple(after) + tuple(self.user(self.admin)):
            self.assertNotIn(temp, str(value))
        self.assertNotIn(temp, str(self.q("SELECT * FROM rate_limit_attempt")))
        self.assertNotIn(temp, a.get("/admin/accounts").get_data(as_text=True))     # not shown again
        self.assertNotIn(temp, a.get("/admin").get_data(as_text=True))

    def test_temp_password_is_not_in_a_redirect_flash_or_url(self):
        a, r = self.reset()
        self.assertNotEqual(r.status_code, 302)
        self.assertNotIn("Location", r.headers)
        temp = self.temp_from(r)
        for resp in (a.get("/admin/accounts"), a.get("/admin")):
            self.assertNotIn(temp, resp.get_data(as_text=True))
        with a.session_transaction() as s:
            self.assertNotIn(temp, str(dict(s)))

    def test_forced_change_flow(self):
        s_old, _ = self.login()                                   # session from before the reset
        _, r = self.reset()
        temp = self.temp_from(r)
        self.assertEqual(s_old.get("/seller").status_code, 302)   # old session invalidated
        self.assertTrue(s_old.get("/seller").headers["Location"].endswith("/login"))
        s, lr = self.login(password=temp)
        self.assertEqual(lr.status_code, 302)
        self.assertTrue(lr.headers["Location"].endswith("/account"))
        for url in ("/seller", "/seller/add", "/", "/search", "/login"):
            resp = s.get(url)
            self.assertEqual(resp.status_code, 302, url)
            self.assertTrue(resp.headers["Location"].endswith("/account"), url)
        self.assertEqual(s.get("/account").status_code, 200)       # no redirect loop
        out = self.post(s, "/logout")                              # logout still works
        self.assertEqual(out.status_code, 302)
        s, _ = self.login(password=temp)
        r = self.post(s, "/account", {"current_password": temp, "new_password": NEWPW, "confirm_password": NEWPW})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.user(self.seller)["must_change_password"], 0)
        self.assertEqual(s.get("/seller").status_code, 302)        # logged out after change
        self.assertEqual(self.login(password=temp)[1].status_code, 200)
        s, _ = self.login(password=NEWPW)
        self.assertEqual(s.get("/seller").status_code, 200)        # normal access resumed

    def test_admin_cannot_reset_own_password_here(self):
        a, r = self.reset(self.admin)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.user(self.admin)["must_change_password"], 0)
        self.assertEqual(self.login(self.admin, APW)[1].status_code, 302)

    def test_reset_does_not_enable_a_disabled_account(self):
        a = self.login(self.admin, APW)[0]
        sid = self.uid(self.seller)
        self.post(a, f"/admin/accounts/{sid}/disable")
        r = self.post(a, f"/admin/accounts/{sid}/reset-password")
        temp = self.temp_from(r)
        self.assertIn("still disabled", r.get_data(as_text=True))
        self.assertEqual(self.user(self.seller)["active"], 0)
        self.assertEqual(self.login(password=temp)[1].status_code, 200)      # still cannot log in
        self.post(a, f"/admin/accounts/{sid}/enable")
        self.assertEqual(self.login(password=temp)[1].status_code, 302)


class ConfigTests(unittest.TestCase):
    def make(self, **kw):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        kw.setdefault("db_path", os.path.join(tmp, "c.db"))
        kw.setdefault("upload_dir", os.path.join(tmp, "up"))
        return create_app(seed_demo=False, **kw)

    def env(self, **vals):
        p = mock.patch.dict(os.environ, vals)
        p.start()
        self.addCleanup(p.stop)
        for k in ("ZEDRO_SECRET_KEY", "ZEDRO_SECRET", "ZEDRO_SESSION_SECURE", "ZEDRO_DATABASE",
                  "ZEDRO_UPLOAD_DIR", "ZEDRO_TRUST_PROXY"):
            if k not in vals:
                os.environ.pop(k, None)

    def test_raycom_refuses_to_start_without_secret(self):
        for vals in ({}, {"ZEDRO_SECRET_KEY": ""}, {"ZEDRO_SECRET_KEY": "   "}):
            self.env(**vals)
            with self.assertRaises(RuntimeError):
                self.make(seed_mode="raycom")

    def test_raycom_uses_configured_secret_and_old_name_still_works(self):
        self.env(ZEDRO_SECRET_KEY="k" * 40)
        self.assertEqual(self.make(seed_mode="raycom").secret_key, "k" * 40)
        self.env(ZEDRO_SECRET="legacy-secret-value")
        self.assertEqual(self.make(seed_mode="raycom").secret_key, "legacy-secret-value")

    def test_demo_mode_keeps_its_fallback_secret(self):
        self.env()
        self.assertTrue(self.make().secret_key)

    def test_cookie_flags(self):
        self.env(ZEDRO_SECRET_KEY="k" * 40)
        app = self.make(seed_mode="raycom")
        self.assertTrue(app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(app.config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertFalse(app.config["SESSION_COOKIE_SECURE"])
        self.env(ZEDRO_SECRET_KEY="k" * 40, ZEDRO_SESSION_SECURE="1")
        app = self.make(seed_mode="raycom")
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"])
        cookie = app.test_client().get("/login", base_url="https://localhost").headers.get("Set-Cookie", "")
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Strict-Transport-Security", app.test_client().get("/").headers)

    def test_database_and_upload_paths_from_environment(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        db, up = os.path.join(tmp, "disk", "z.db"), os.path.join(tmp, "disk", "uploads")
        self.env(ZEDRO_DATABASE=db, ZEDRO_UPLOAD_DIR=up)
        app = create_app(seed_demo=False)
        self.assertEqual((app.config["DATABASE"], app.config["UPLOAD_DIR"]), (db, up))
        self.assertTrue(os.path.exists(db))

    def test_defaults_unchanged_without_environment(self):
        self.env()
        app = create_app(seed_demo=False)
        self.assertTrue(app.config["DATABASE"].endswith(os.path.join("instance", "zedro.db")))

    def test_raycom_refuses_a_demo_database_and_leaves_it_untouched(self):
        self.env(ZEDRO_SECRET_KEY="k" * 40)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "demo.db")
        with contextlib.redirect_stdout(io.StringIO()):
            create_app(db_path=path, upload_dir=tmp)
        with self.assertRaises(RuntimeError):
            create_app(db_path=path, upload_dir=tmp, seed_mode="raycom")
        c = sqlite3.connect(path)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM store").fetchone()[0], 5)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM user").fetchone()[0], 6)
        c.close()

    def test_raycom_seed_is_idempotent_and_admin_password_is_separate(self):
        self.env(ZEDRO_SECRET_KEY="k" * 40, ZEDRO_RAYCOM_PASSWORD="seller-pass-1234",
                 ZEDRO_ADMIN_PASSWORD="admin-pass-5678")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "r.db")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            create_app(db_path=path, upload_dir=tmp, seed_mode="raycom")
            create_app(db_path=path, upload_dir=tmp, seed_mode="raycom")    # second start seeds nothing
        c = sqlite3.connect(path)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM store").fetchone()[0], 1)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM user").fetchone()[0], 2)
        h = dict(c.execute("SELECT role, password_hash FROM user").fetchall())
        c.close()
        self.assertTrue(check_password_hash(h["seller"], "seller-pass-1234"))
        self.assertTrue(check_password_hash(h["admin"], "admin-pass-5678"))
        self.assertFalse(check_password_hash(h["admin"], "seller-pass-1234"))

    def test_admin_password_falls_back_to_raycom_password(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        c = sqlite3.connect(os.path.join(tmp, "f.db"))
        dbm.init_db(c)
        dbm.seed_raycom(c, password="shared-pass-123")
        h = dict(c.execute("SELECT role, password_hash FROM user").fetchall())
        c.close()
        self.assertTrue(check_password_hash(h["admin"], "shared-pass-123"))


class MigrationTests(unittest.TestCase):
    OLD_SCHEMA = """
        CREATE TABLE store (id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT, address TEXT, phone TEXT,
            whatsapp TEXT, created_at TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE user (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('seller', 'admin')), store_id INTEGER REFERENCES store(id),
            created_at TEXT NOT NULL);
    """

    def test_existing_database_is_upgraded_in_place(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "old.db")
        c = sqlite3.connect(path)
        c.executescript(self.OLD_SCHEMA)
        c.execute("INSERT INTO store (id, name, created_at) VALUES (7, 'Old Store', '2026-01-01T00:00:00')")
        c.execute("INSERT INTO user VALUES (3, 'old@x.zz', 'scrypt:keepme', 'seller', 7, '2026-02-02T00:00:00')")
        c.commit()
        dbm.init_db(c)
        dbm.init_db(c)                                            # idempotent
        row = c.execute("SELECT id, email, password_hash, role, store_id, created_at, active,"
                        " must_change_password FROM user").fetchall()
        cols = [r[1] for r in c.execute("PRAGMA table_info(user)")]
        c.close()
        self.assertEqual(row, [(3, "old@x.zz", "scrypt:keepme", "seller", 7, "2026-02-02T00:00:00", 1, 0)])
        self.assertEqual(cols.count("active"), 1)
        self.assertEqual(cols.count("must_change_password"), 1)

    def test_upgraded_database_still_serves_logins(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "old2.db")
        c = sqlite3.connect(path)
        c.executescript(self.OLD_SCHEMA)
        c.execute("INSERT INTO store (id, name, created_at) VALUES (1, 'Old Store', '2026-01-01T00:00:00')")
        from werkzeug.security import generate_password_hash
        c.execute("INSERT INTO user VALUES (1, 'old@x.zz', ?, 'seller', 1, '2026-02-02T00:00:00')",
                  (generate_password_hash("old-password-1"),))
        c.commit()
        c.close()
        with mock.patch.dict(os.environ, {"ZEDRO_SECRET_KEY": "k" * 40}):
            app = create_app(db_path=path, upload_dir=tmp, seed_demo=False, seed_mode="raycom_legacy_x")
        cl = app.test_client()
        with cl.session_transaction() as s:
            s["csrf"] = "tok"
        r = cl.post("/login", data={"email": "old@x.zz", "password": "old-password-1", "csrf_token": "tok"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(cl.get("/seller").status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
