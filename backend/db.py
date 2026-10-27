"""SQLite storage for the PMIS VLE web app.

Tables
  vles          one row per VLE (the "head") - login id, location, PIN hash, role
  applications  one row per application record from the PMIS export
  meta          key/value (last import time, source file name)
"""
import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("PMIS_DB", Path(__file__).resolve().parent.parent / "data" / "pmis.db"))

# Journey order used everywhere (backend + frontend). Change here if the
# business confirms a different order, e.g. merging "Accepted" and
# "Offer Letter Accepted".
STAGES = [
    "Applied",
    "Shortlisted",
    "Offer Letter Received",
    "Offer Letter Accepted",
    "Accepted",
    "Joined",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS vles (
    vle_id      TEXT PRIMARY KEY,
    location    TEXT,
    pin_salt    TEXT NOT NULL,
    pin_hash    TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'vle'      -- 'vle' or 'admin'
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
    project_code    TEXT
);
CREATE INDEX IF NOT EXISTS ix_app_vle ON applications(vle_id);
CREATE INDEX IF NOT EXISTS ix_app_cand ON applications(candidate_id);
CREATE INDEX IF NOT EXISTS ix_app_intern ON applications(internship_id);
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


def hash_pin(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 120_000).hex()


def set_pin(con, vle_id: str, pin: str):
    salt = secrets.token_hex(8)
    con.execute("UPDATE vles SET pin_salt=?, pin_hash=? WHERE vle_id=?", (salt, hash_pin(pin, salt), vle_id))


def check_pin(row, pin: str) -> bool:
    return secrets.compare_digest(row["pin_hash"], hash_pin(pin, row["pin_salt"]))
