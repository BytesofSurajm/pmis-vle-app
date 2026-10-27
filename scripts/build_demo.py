"""Build a single-file demo page (no server) from frontend/index.html + an Excel file.

    python scripts/build_demo.py PMIS_export.xlsx demo.html [--fragment]

--fragment drops the <html>/<head>/<body> wrapper (for hosts that add their own).
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
import importer  # noqa: E402

src, out = Path(sys.argv[1]), Path(sys.argv[2])
rows, _ = importer.read_rows(src.read_bytes())
html = (ROOT / "frontend" / "index.html").read_text()
data = json.dumps({"applications": rows, "updated": f"{date.today().isoformat()} (sample)"}, ensure_ascii=False)
inject = f"<script>window.PMIS_DEMO={data};</script>\n"
html = html.replace("<script>\n(() => {", inject + "<script>\n(() => {", 1)
if "--fragment" in sys.argv:
    head = re.search(r"<head>(.*?)</head>", html, re.S).group(1)
    head = re.sub(r'<meta charset[^>]*>\s*|<meta name="viewport"[^>]*>\s*', "", head)
    body = re.search(r"<body>(.*)</body>", html, re.S).group(1)
    html = head.strip() + "\n" + body.strip() + "\n"
out.write_text(html)
print(f"wrote {out} with {len(rows)} rows")
