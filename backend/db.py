"""SQLite storage for the PMIS VLE web app.

Tables
  vles          one row per login - VLE heads and admin users (role 'vle' / 'admin')
  applications  one row per application (a candidate can have several)
  meta          key/value (last import time, source file name)
"""
import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("PMIS_DB", Path(__file__).resolve().parent.parent / "data" / "pmis.db"))

# Journey order agreed with the client (call of 27-10-2026). Used by backend and frontend.
STAGES = [
    "Applied",
    "Shortlisted",
    "Selected",
    "Offer Letter Received",
    "Offer Letter Accepted",
    "Joining Verified",
]
# Older / alternative labels found in exports -> agreed stage
STAGE_ALIASES = {
    "accepted": "Selected",
    "joined": "Joining Verified",
    "joining verified": "Joining Verified",
    "offer letter received": "Offer Letter Received",
    "offer letter accepted": "Offer Letter Accepted",
    "short listed": "Shortlisted",
}


def normalise_status(raw: str) -> str | None:
    s = " ".join(str(raw or "").split())
    for st in STAGES:
        if s.lower() == st.lower():
            return st
    return STAGE_ALIASES.get(s.lower())


SCHEMA = """
CREATE TABLE IF NOT EXISTS vles (
    vle_id      TEXT PRIMARY KEY,
    location    TEXT,
    district    TEXT,
    pin_salt    TEXT NOT NULL,
    pin_hash    TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'vle'
);
CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id    TEXT NOT NULL,
    candidate_name  TEXT NOT NULL,
    internship_id   TEXT NOT NULL,
    company_name    TEXT,
    status          TEXT NOT NULL,
    vle_id          TEXT NOT NULL,
    vle_location    TEXT,
    vle_district    TEXT,
    project_code    TEXT
);
CREATE INDEX IF NOT EXISTS ix_app_vle ON applications(vle_id);
CREATE INDEX IF NOT EXISTS ix_app_cand ON applications(candidate_id);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with connect() as con:
        con.executescript(SCHEMA)
        # upgrade databases created by the first version
        for table, col in (("vles", "district"), ("applications", "vle_district")):
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
            if col not in cols:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")


def hash_pin(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 120_000).hex()


def set_pin(con, vle_id: str, pin: str):
    salt = secrets.token_hex(8)
    con.execute("UPDATE vles SET pin_salt=?, pin_hash=? WHERE vle_id=?", (salt, hash_pin(pin, salt), vle_id))


def upsert_admin(con, user_id: str, pin: str):
    if not con.execute("SELECT 1 FROM vles WHERE vle_id=?", (user_id,)).fetchone():
        con.execute("INSERT INTO vles (vle_id, location, pin_salt, pin_hash, role) VALUES (?, 'All', '', '', 'admin')", (user_id,))
    con.execute("UPDATE vles SET role='admin' WHERE vle_id=?", (user_id,))
    set_pin(con, user_id, pin)


def check_pin(row, pin: str) -> bool:
    return bool(row["pin_hash"]) and secrets.compare_digest(row["pin_hash"], hash_pin(pin, row["pin_salt"]))
