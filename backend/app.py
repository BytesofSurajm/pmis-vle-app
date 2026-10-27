"""PMIS VLE web app - API + static frontend.

Run:  uvicorn app:app --host 0.0.0.0 --port 8000
Env:  PMIS_SECRET   long random string used to sign login tokens (required in production)
      PMIS_DB       path to the SQLite file (default ../data/pmis.db)

Security model (replaces Power BI row-level security):
  * A VLE logs in with VLE ID + 4-digit PIN and receives a signed token.
  * Every data query is filtered on the server by the VLE ID inside the token.
    A VLE can never ask for another VLE's rows - there is no parameter for it.
  * The ADMIN login (role=admin) may pick any VLE and upload a new Excel file.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from collections import defaultdict
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import importer

SECRET = os.environ.get("PMIS_SECRET", "dev-only-change-me").encode()
TOKEN_HOURS = 12
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="PMIS VLE Dashboard API")
db.init()


# ---------- start-up seeding (for free hosts whose disk is wiped on restart) ----------
# PMIS_SEED_XLSX  Excel file loaded when the database is empty (default data/sample_PMIS.xlsx)
# PMIS_PINS       fixed PINs, e.g. "VLEKA20261:0630,VLEMA20266:4113"
# PMIS_ADMIN_PIN  PIN for the ADMIN login
def seed():
    with db.connect() as con:
        empty = con.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    if empty:
        f = Path(os.environ.get("PMIS_SEED_XLSX", FRONTEND.parent / "data" / "sample_PMIS.xlsx"))
        if f.exists():
            importer.load(f.read_bytes(), f.name)
    with db.connect() as con:
        for pair in os.environ.get("PMIS_PINS", "").split(","):
            if ":" in pair:
                vle, pin = (x.strip() for x in pair.split(":", 1))
                if con.execute("SELECT 1 FROM vles WHERE vle_id=?", (vle.upper(),)).fetchone():
                    db.set_pin(con, vle.upper(), pin)
        admin_pin = os.environ.get("PMIS_ADMIN_PIN", "").strip()
        if admin_pin:
            if not con.execute("SELECT 1 FROM vles WHERE vle_id='ADMIN'").fetchone():
                con.execute("INSERT INTO vles (vle_id, location, pin_salt, pin_hash, role) VALUES ('ADMIN','All','','','admin')")
            db.set_pin(con, "ADMIN", admin_pin)


seed()


# ---------- tokens (HMAC-signed, no external dependency) ----------
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def make_token(vle_id: str, role: str) -> str:
    body = _b64(json.dumps({"v": vle_id, "r": role, "exp": int(time.time()) + TOKEN_HOURS * 3600}).encode())
    sig = _b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_token(authorization: str = Header(default="")) -> dict:
    try:
        body, sig = authorization.removeprefix("Bearer ").split(".")
        good = _b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if data["exp"] < time.time():
            raise ValueError
        return data
    except Exception:
        raise HTTPException(401, "Please log in again.")


# ---------- login with simple brute-force protection ----------
_fails: dict[str, list[float]] = defaultdict(list)


class Login(BaseModel):
    vle_id: str
    pin: str


@app.post("/api/login")
def login(body: Login):
    vle_id = body.vle_id.strip().upper()
    recent = [t for t in _fails[vle_id] if t > time.time() - 900]
    _fails[vle_id] = recent
    if len(recent) >= 5:
        raise HTTPException(429, "Too many wrong tries. Wait 15 minutes.")
    with db.connect() as con:
        row = con.execute("SELECT * FROM vles WHERE vle_id=?", (vle_id,)).fetchone()
    if not row or not db.check_pin(row, body.pin.strip()):
        _fails[vle_id].append(time.time())
        raise HTTPException(401, "VLE ID or PIN is wrong.")
    _fails.pop(vle_id, None)
    return {"token": make_token(row["vle_id"], row["role"]), "vle_id": row["vle_id"], "role": row["role"]}


# ---------- data ----------
def _payload(con, vle_id: str):
    vle = con.execute("SELECT vle_id, location FROM vles WHERE vle_id=?", (vle_id,)).fetchone()
    if not vle:
        raise HTTPException(404, "VLE not found.")
    rows = con.execute(
        """SELECT candidate_id, candidate_name, internship_id, company_name, status,
                  vle_id, vle_location, project_code
           FROM applications WHERE vle_id=? ORDER BY candidate_name""",
        (vle_id,),
    ).fetchall()
    meta = dict(con.execute("SELECT k, v FROM meta").fetchall())
    return {
        "vle": dict(vle),
        "stages": db.STAGES,
        "applications": [dict(r) for r in rows],
        "updated": meta.get("last_import"),
    }


@app.get("/api/dashboard")
def dashboard(vle: str | None = None, user: dict = Depends(read_token)):
    target = user["v"]
    if user["r"] == "admin":
        target = (vle or "").upper()
        if not target:
            raise HTTPException(400, "Choose a VLE.")
    with db.connect() as con:
        return _payload(con, target)


@app.get("/api/vles")
def list_vles(user: dict = Depends(read_token)):
    if user["r"] != "admin":
        raise HTTPException(403, "Admin only.")
    with db.connect() as con:
        rows = con.execute(
            """SELECT v.vle_id, v.location, COUNT(a.id) AS records,
                      SUM(a.status='Joined') AS joined
               FROM vles v LEFT JOIN applications a ON a.vle_id=v.vle_id
               WHERE v.role='vle' GROUP BY v.vle_id ORDER BY v.vle_id"""
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/admin/import")
async def upload(file: UploadFile = File(...), user: dict = Depends(read_token)):
    if user["r"] != "admin":
        raise HTTPException(403, "Admin only.")
    data = await file.read()
    try:
        return importer.load(data, file.filename or "upload.xlsx")
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------- frontend ----------
@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
