import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as dbm  # noqa: E402
from app import create_app, freshness  # noqa: E402

PW = "test-pass-123"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dbfile = os.path.join(self.tmp, "t.db")
        self.app = create_app(db_path=self.dbfile, upload_dir=os.path.join(self.tmp, "up"))
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
