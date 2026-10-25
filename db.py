"""ZEDRO database schema and demo data (stdlib sqlite3)."""
import random
import secrets
from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

FMT = "%Y-%m-%dT%H:%M:%S"

SCHEMA = """
CREATE TABLE IF NOT EXISTS store (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    address TEXT,
    phone TEXT,
    whatsapp TEXT,
    created_at TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS user (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('seller', 'admin')),
    store_id INTEGER REFERENCES store(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listing (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES store(id),
    brand TEXT NOT NULL,
    model TEXT NOT NULL,
    storage TEXT,
    condition TEXT NOT NULL,
    price INTEGER NOT NULL,
    battery_health INTEGER,
    repair_history TEXT,
    description TEXT,
    image TEXT,
    status TEXT NOT NULL DEFAULT 'available' CHECK (status IN ('available', 'sold')),
    approved INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listing_store ON listing(store_id);
CREATE INDEX IF NOT EXISTS idx_listing_updated ON listing(updated_at);
"""


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def ts(dt=None):
    return (dt or now_utc()).strftime(FMT)


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()


# Clearly fictional demo stores. Swap names here if a dealer agrees to be shown.
STORES = [
    ("Demo Mobile Point", "Demonstration store. Not a real business.", "08000000001", "2348000000001"),
    ("Demo Gadget Hub", "Demonstration store. Not a real business.", "08000000002", "2348000000002"),
    ("Demo Phone Depot", "Demonstration store. Not a real business.", "08000000003", "2348000000003"),
    ("Demo Tech Corner", "Demonstration store. Not a real business.", "08000000004", "2348000000004"),
    ("Demo Phone Plaza", "Demonstration store. Not a real business.", "08000000005", "2348000000005"),
]

N, F, U = "New", "Fairly Used", "Used"
# (brand, model, storage, condition, price NGN, battery %, repair note, store index, age in minutes, sold?)
LISTINGS = [
    ("Apple", "iPhone 13", "128GB", F, 480000, 88, None, 0, 24, 0),
    ("Apple", "iPhone 13", "128GB", F, 465000, 85, "Screen replaced", 1, 300, 0),
    ("Apple", "iPhone 13", "256GB", F, 540000, 90, None, 2, 1500, 0),
    ("Apple", "iPhone 13", "128GB", N, 720000, 100, None, 3, 90, 0),
    ("Apple", "iPhone 13 Pro Max", "256GB", F, 760000, 84, None, 4, 4000, 0),
    ("Apple", "iPhone 13 mini", "128GB", U, 360000, 80, "Battery replaced", 1, 6000, 0),
    ("Apple", "iPhone 13", "128GB", F, 470000, 87, None, 3, 2000, 1),
    ("Apple", "iPhone 12", "64GB", F, 330000, 82, None, 0, 130, 0),
    ("Apple", "iPhone 12", "128GB", F, 365000, 86, None, 2, 2000, 0),
    ("Apple", "iPhone 12 Pro", "128GB", U, 400000, 79, "Back glass replaced", 3, 9000, 0),
    ("Apple", "iPhone 11", "64GB", U, 235000, 78, None, 4, 45, 0),
    ("Apple", "iPhone 11", "128GB", F, 270000, 83, None, 0, 700, 0),
    ("Apple", "iPhone 14", "128GB", F, 610000, 91, None, 1, 55, 0),
    ("Apple", "iPhone 14 Pro", "256GB", F, 900000, 89, None, 2, 8000, 0),
    ("Apple", "iPhone 15", "128GB", N, 1050000, 100, None, 3, 200, 0),
    ("Apple", "iPhone 15 Pro Max", "256GB", N, 1650000, 100, None, 4, 3000, 0),
    ("Samsung", "Galaxy S23", "256GB", F, 620000, None, None, 0, 35, 0),
    ("Samsung", "Galaxy S23 Ultra", "256GB", F, 890000, None, None, 1, 1800, 0),
    ("Samsung", "Galaxy S23", "128GB", N, 780000, None, None, 2, 400, 0),
    ("Samsung", "Galaxy S24", "256GB", N, 1050000, None, None, 3, 60, 0),
    ("Samsung", "Galaxy A55", "128GB", N, 520000, None, None, 4, 150, 0),
    ("Samsung", "Galaxy A55", "256GB", N, 575000, None, None, 0, 5000, 0),
    ("Samsung", "Galaxy A35", "128GB", N, 405000, None, None, 1, 2500, 0),
    ("Samsung", "Galaxy A15", "128GB", N, 195000, None, None, 2, 20, 0),
    ("Samsung", "Galaxy A14", "64GB", F, 110000, None, None, 3, 1000, 0),
    ("Samsung", "Galaxy A54", "128GB", F, 310000, None, None, 4, 12000, 0),
    ("Tecno", "Camon 30", "256GB", N, 305000, None, None, 0, 75, 0),
    ("Tecno", "Camon 20", "128GB", N, 215000, None, None, 1, 500, 0),
    ("Tecno", "Camon 19", "128GB", F, 120000, None, None, 2, 2800, 0),
    ("Tecno", "Spark 20 Pro", "256GB", N, 165000, None, None, 3, 15, 0),
    ("Tecno", "Spark 10", "128GB", U, 85000, None, None, 4, 7000, 0),
    ("Tecno", "Phantom X2", "256GB", F, 330000, None, None, 0, 3500, 0),
    ("Infinix", "Note 40", "256GB", N, 235000, None, None, 1, 110, 0),
    ("Infinix", "Hot 40 Pro", "128GB", N, 155000, None, None, 2, 250, 0),
    ("Infinix", "Zero 30", "256GB", F, 195000, None, None, 3, 4500, 0),
    ("Xiaomi", "Redmi Note 13", "128GB", N, 265000, None, None, 4, 40, 0),
    ("Xiaomi", "Redmi Note 13 Pro", "256GB", N, 340000, None, None, 0, 600, 0),
    ("Xiaomi", "Redmi Note 12", "128GB", F, 150000, None, None, 1, 2200, 0),
    ("Xiaomi", "Redmi 13C", "128GB", N, 128000, None, None, 2, 90, 0),
    ("Xiaomi", "Redmi Note 11", "64GB", U, 90000, None, None, 3, 20000, 0),
    ("Xiaomi", "Redmi Note 13", "128GB", N, 260000, None, None, 2, 800, 1),
    ("Google", "Pixel 7", "128GB", F, 330000, None, None, 4, 1600, 0),
]


def seed(conn, password=None, rng_seed=13):
    """Insert demo stores, listings and logins. Returns [(email, role, password)].

    Does nothing (returns []) if stores already exist.
    """
    if conn.execute("SELECT COUNT(*) FROM store").fetchone()[0]:
        return []
    random.Random(rng_seed)  # data is fixed; kept for future jitter
    now = now_utc()
    creds = []
    store_ids = []
    for i, (name, desc, phone, wa) in enumerate(STORES, start=1):
        cur = conn.execute(
            "INSERT INTO store (name, description, address, phone, whatsapp, created_at, approved)"
            " VALUES (?, ?, ?, ?, ?, ?, 1)",
            (name, desc, "Ughelli, Delta State (demo address)", phone, wa, ts(now)),
        )
        store_ids.append(cur.lastrowid)
        pw = password or secrets.token_urlsafe(9)
        email = f"seller{i}@demo.zedro"
        conn.execute(
            "INSERT INTO user (email, password_hash, role, store_id, created_at) VALUES (?, ?, 'seller', ?, ?)",
            (email, generate_password_hash(pw), cur.lastrowid, ts(now)),
        )
        creds.append((email, "seller", pw))
    pw = password or secrets.token_urlsafe(9)
    conn.execute(
        "INSERT INTO user (email, password_hash, role, store_id, created_at) VALUES (?, ?, 'admin', NULL, ?)",
        ("admin@demo.zedro", generate_password_hash(pw), ts(now)),
    )
    creds.append(("admin@demo.zedro", "admin", pw))
    for brand, model, storage, cond, price, batt, repair, si, age, sold in LISTINGS:
        stamp = ts(now - timedelta(minutes=age))
        conn.execute(
            "INSERT INTO listing (store_id, brand, model, storage, condition, price, battery_health,"
            " repair_history, description, status, approved, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (store_ids[si], brand, model, storage, cond, price, batt, repair,
             "Demonstration listing. Not confirmed by the store.",
             "sold" if sold else "available", stamp, stamp),
        )
    conn.commit()
    return creds


if __name__ == "__main__":
    import os
    import sqlite3
    import sys

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "zedro.db")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if "--reset" in sys.argv and os.path.exists(path):
        os.remove(path)
    c = sqlite3.connect(path)
    init_db(c)
    out = seed(c)
    if not out:
        print("Database already has data. Use --reset to start over.")
    for email, role, pw in out:
        print(f"{role:7} {email}  {pw}")
