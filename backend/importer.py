"""Load the PMIS Excel export into SQLite.

Usage:
    python importer.py path/to/PMIS_export.xlsx [--admin-pin 9999]

* Reads the sheet named "Applications" (or the first sheet that has a
  "Candidate Id" header). Columns are matched by header name, so column
  order does not matter and helper/formula columns are ignored.
* All IDs are kept as text.
* Application rows are fully replaced on every import (snapshot load).
* Every new VLE gets a random 4-digit PIN. New PINs are written to
  data/new_vle_pins.csv so the admin can hand them out - share that file
  privately and delete it afterwards. Existing VLE PINs are never changed.
"""
import argparse
import csv
import io
import secrets
import sys
from datetime import datetime
from pathlib import Path

import openpyxl

import db

COLUMNS = {
    "candidate id": "candidate_id",
    "candidate name": "candidate_name",
    "internship id": "internship_id",
    "company name": "company_name",
    "application status": "status",
    "vle id": "vle_id",
    "vle location": "vle_location",
    "project code": "project_code",
}
REQUIRED = {"candidate_id", "candidate_name", "internship_id", "status", "vle_id"}


def _clean(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def read_rows(xlsx_bytes: bytes):
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    sheets = [wb["Applications"]] if "Applications" in wb.sheetnames else wb.worksheets
    for ws in sheets:
        it = ws.iter_rows(values_only=True)
        for header in it:
            names = [_clean(h).lower() for h in header]
            if "candidate id" in names:
                idx = {COLUMNS[n]: i for i, n in enumerate(names) if n in COLUMNS}
                missing = REQUIRED - idx.keys()
                if missing:
                    raise ValueError(f"Missing columns: {', '.join(sorted(missing))}")
                rows, problems = [], []
                for n, r in enumerate(it, start=2):
                    rec = {k: _clean(r[i]) if i < len(r) else "" for k, i in idx.items()}
                    if not rec["candidate_id"]:
                        continue
                    if rec["status"] not in db.STAGES:
                        problems.append(f"row {n}: unknown status '{rec['status']}'")
                    rows.append(rec)
                return rows, problems
    raise ValueError("No sheet with a 'Candidate Id' header was found.")


def load(xlsx_bytes: bytes, source_name: str = "upload.xlsx", admin_pin: str | None = None):
    rows, problems = read_rows(xlsx_bytes)
    db.init()
    new_pins = []
    with db.connect() as con:
        con.execute("DELETE FROM applications")
        con.executemany(
            """INSERT INTO applications (candidate_id, candidate_name, internship_id, company_name,
               status, vle_id, vle_location, project_code)
               VALUES (:candidate_id, :candidate_name, :internship_id, :company_name,
               :status, :vle_id, :vle_location, :project_code)""",
            [{k: r.get(k, "") for k in COLUMNS.values()} for r in rows],
        )
        locs = {}
        for r in rows:
            locs.setdefault(r["vle_id"], r.get("vle_location", ""))
        for vle_id, loc in locs.items():
            hit = con.execute("SELECT 1 FROM vles WHERE vle_id=?", (vle_id,)).fetchone()
            if hit:
                con.execute("UPDATE vles SET location=? WHERE vle_id=?", (loc, vle_id))
            else:
                pin = f"{secrets.randbelow(10000):04d}"
                con.execute("INSERT INTO vles (vle_id, location, pin_salt, pin_hash) VALUES (?,?,'','')", (vle_id, loc))
                db.set_pin(con, vle_id, pin)
                new_pins.append((vle_id, loc, pin))
        if admin_pin:
            if not con.execute("SELECT 1 FROM vles WHERE vle_id='ADMIN'").fetchone():
                con.execute("INSERT INTO vles (vle_id, location, pin_salt, pin_hash, role) VALUES ('ADMIN','All','','','admin')")
            db.set_pin(con, "ADMIN", admin_pin)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('last_import', ?)", (datetime.now().isoformat(timespec="minutes"),))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('source', ?)", (source_name,))

    if new_pins:
        out = db.DB_PATH.parent / "new_vle_pins.csv"
        write_header = not out.exists()
        with out.open("a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(["VLE ID", "VLE Location", "PIN"])
            w.writerows(new_pins)
    return {"rows": len(rows), "vles": len({r["vle_id"] for r in rows}), "new_vles": len(new_pins), "warnings": problems}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx")
    ap.add_argument("--admin-pin", help="create/reset the ADMIN login with this PIN")
    a = ap.parse_args()
    p = Path(a.xlsx)
    result = load(p.read_bytes(), p.name, a.admin_pin)
    print(result)
    if result["new_vles"]:
        print(f"New PINs written to {db.DB_PATH.parent / 'new_vle_pins.csv'}")
    sys.exit(0)
