r"""Bulk-upload every PDF in a folder to a running COI tracker through its normal upload form (same as picking
all the files in the dashboard's file picker). For local testing.

  .venv\Scripts\python.exe tools\upload_folder.py samples\bulk20 --email you@example.com --password "..."
"""
import argparse
import os
import re

import httpx

ap = argparse.ArgumentParser()
ap.add_argument("folder")
ap.add_argument("--base", default="http://localhost:8010")
ap.add_argument("--email", required=True)
ap.add_argument("--password", required=True)
a = ap.parse_args()
with httpx.Client(base_url=a.base, follow_redirects=True, timeout=120) as c:
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"email": a.email, "password": a.password, "csrf": tok})
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/dashboard").text).group(1)
    names = sorted(n for n in os.listdir(a.folder) if n.lower().endswith((".pdf", ".jpg", ".jpeg", ".png")))
    files = [("files", (n, open(os.path.join(a.folder, n), "rb").read(), "application/pdf")) for n in names]
    r = c.post("/upload", data={"csrf": tok}, files=files)
    m = re.search(r'class="banner flash">([^<]+)<', r.text)
    print(len(names), "files ->", m.group(1) if m else r.status_code)
