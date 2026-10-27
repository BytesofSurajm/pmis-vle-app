"""PMIS VLE web app - API + static frontend.

Run:  uvicorn app:app --host 0.0.0.0 --port 8000

Logins
  * VLE heads: VLE ID + PIN/password. See only their own candidates (filtered on the server).
  * Admin users (role=admin): project-wide dashboard with state/district filters, can open any
    VLE and upload a new Excel export.

Credentials come from data/pins.csv (columns: ID,PIN[,role]) and/or env vars:
  PMIS_PINS="VLEKA20261:0630,VLEMA20266:4113"   PMIS_ADMIN_PIN="5823"
Optional: PMIS_SECRET (token key; random per start if unset), PMIS_DB, PMIS_SEED_XLSX.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from collections import defaultdict
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import importer

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
DATA = ROOT / "data"
SECRET = (os.environ.get("PMIS_SECRET") or secrets.token_hex(32)).encode()
TOKEN_HOURS = 12

app = FastAPI(title="PMIS CSC Project - VLE Dashboard API")
db.init()


# ---------- start-up seeding (free hosts wipe the disk on restart) ----------
def read_credentials():
    """Returns [(id, pin, role)] from data/pins.csv and env vars."""
    creds = []
    f = DATA / "pins.csv"
    if f.exists():
        for line in f.read_text(encoding="utf-8-sig").splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) < 2 or not parts[0] or not parts[1] or parts[0].lower() in ("vle id", "id"):
                continue
            uid = parts[0].upper()
            role = (parts[2].lower() if len(parts) > 2 and parts[2] else ("admin" if uid == "ADMIN" else "vle"))
            creds.append((uid, parts[1], role))
    for pair in os.environ.get("PMIS_PINS", "").split(","):
        if ":" in pair:
            uid, pin = (x.strip() for x in pair.split(":", 1))
            creds.append((uid.upper(), pin, "vle"))
    if os.environ.get("PMIS_ADMIN_PIN", "").strip():
        creds.append(("ADMIN", os.environ["PMIS_ADMIN_PIN"].strip(), "admin"))
    return creds


def seed():
    with db.connect() as con:
        empty = con.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    if empty:
        f = Path(os.environ.get("PMIS_SEED_XLSX", DATA / "sample_PMIS.xlsx"))
        if f.exists():
            importer.load(f.read_bytes(), f.name)
    with db.connect() as con:
        for uid, pin, role in read_credentials():
            if role == "admin":
                db.upsert_admin(con, uid, pin)
            elif con.execute("SELECT 1 FROM vles WHERE vle_id=?", (uid,)).fetchone():
                db.set_pin(con, uid, pin)


seed()


# ---------- tokens ----------
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def make_token(vle_id: str, role: str) -> str:
    body = _b64(json.dumps({"v": vle_id, "r": role, "exp": int(time.time()) + TOKEN_HOURS * 3600}).encode())
    return f"{body}.{_b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())}"


def read_token(authorization: str = Header(default="")) -> dict:
    try:
        body, sig = authorization.removeprefix("Bearer ").split(".")
        if not hmac.compare_digest(sig, _b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if data["exp"] < time.time():
            raise ValueError
        return data
    except Exception:
        raise HTTPException(401, "Please log in again.")


def admin_only(user: dict = Depends(read_token)) -> dict:
    if user["r"] != "admin":
        raise HTTPException(403, "Admin only.")
    return user


# ---------- login ----------
_fails: dict[str, list[float]] = defaultdict(list)


class Login(BaseModel):
    vle_id: str
    pin: str


@app.post("/api/login")
def login(body: Login):
    uid = body.vle_id.strip().upper()
    _fails[uid] = [t for t in _fails[uid] if t > time.time() - 900]
    if len(_fails[uid]) >= 5:
        raise HTTPException(429, "Too many wrong tries. Wait 15 minutes.")
    with db.connect() as con:
        row = con.execute("SELECT * FROM vles WHERE vle_id=?", (uid,)).fetchone()
    if not row or not db.check_pin(row, body.pin.strip()):
        _fails[uid].append(time.time())
        raise HTTPException(401, "VLE ID or password is wrong.")
    _fails.pop(uid, None)
    return {"token": make_token(row["vle_id"], row["role"]), "vle_id": row["vle_id"], "role": row["role"]}


# ---------- data ----------
APP_COLS = "candidate_id, candidate_name, internship_id, company_name, status, vle_id, vle_location, vle_district, project_code"


def _meta(con):
    return dict(con.execute("SELECT k, v FROM meta").fetchall())


@app.get("/api/dashboard")
def dashboard(vle: str | None = None, user: dict = Depends(read_token)):
    target = user["v"]
    if user["r"] == "admin":
        if not vle:
            raise HTTPException(400, "Choose a VLE.")
        target = vle.upper()
    with db.connect() as con:
        v = con.execute("SELECT vle_id, location, district FROM vles WHERE vle_id=? AND role='vle'", (target,)).fetchone()
        if not v:
            raise HTTPException(404, "VLE not found.")
        rows = con.execute(f"SELECT {APP_COLS} FROM applications WHERE vle_id=? ORDER BY candidate_name", (target,)).fetchall()
        return {"vle": dict(v), "stages": db.STAGES, "applications": [dict(r) for r in rows],
                "updated": _meta(con).get("last_import")}


@app.get("/api/admin/overview")
def overview(user: dict = Depends(admin_only)):
    """All applications for the project-wide (state / district / VLE) dashboard."""
    with db.connect() as con:
        rows = con.execute(f"SELECT {APP_COLS} FROM applications").fetchall()
        return {"stages": db.STAGES, "applications": [dict(r) for r in rows], "updated": _meta(con).get("last_import")}


@app.post("/api/admin/import")
async def upload(file: UploadFile = File(...), user: dict = Depends(admin_only)):
    try:
        result = importer.load(await file.read(), file.filename or "upload.xlsx")
    except ValueError as e:
        raise HTTPException(400, str(e))
    seed()  # re-apply fixed PINs to any VLEs listed in pins.csv
    return result


@app.get("/api/health")
def health():
    with db.connect() as con:
        return {"ok": True,
                "applications": con.execute("SELECT COUNT(*) FROM applications").fetchone()[0],
                "vles": con.execute("SELECT COUNT(*) FROM vles WHERE role='vle'").fetchone()[0],
                "admins": con.execute("SELECT COUNT(*) FROM vles WHERE role='admin'").fetchone()[0]}


# ---------- frontend ----------
@app.api_route("/", methods=["GET", "HEAD"])
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
