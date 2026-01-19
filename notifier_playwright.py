#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import re
from typing import Dict, List
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

# ================= ENV =================
LOGIN_USER = os.environ["LOGIN_USER"]
LOGIN_PASS = os.environ["LOGIN_PASS"]
PAGE_URL   = os.environ["PAGE_URL"]

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID_1 = os.getenv("TELEGRAM_CHAT_ID_1")
TG_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2")
CHAT_IDS = [c for c in (TG_CHAT_ID_1, TG_CHAT_ID_2) if c]

GIST_ID    = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = os.getenv("GIST_FILENAME", "keitaro_state.json")

KYIV_TZ = ZoneInfo("Europe/Kyiv")
EPS = 0.0001

# ================= HELPERS =================
def now_kyiv():
    return datetime.now(KYIV_TZ)

def today():
    return now_kyiv().strftime("%Y-%m-%d")

def as_float(x):
    try:
        return float(x or 0)
    except:
        return 0.0

def money(x):
    return f"${x:,.2f}"

# ================= TELEGRAM =================
def tg_send(text: str):
    for cid in CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={
                    "chat_id": cid,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True
                },
                timeout=15
            )
        except:
            pass

# ================= GIST STATE =================
def load_state() -> Dict:
    url = f"https://api.github.com/gists/{GIST_ID}"
    r = requests.get(url, headers={
        "Authorization": f"Bearer {GIST_TOKEN}",
        "Accept": "application/vnd.github+json"
    }, timeout=30)

    if r.status_code == 200:
        files = r.json().get("files", {})
        if GIST_FILENAME in files:
            try:
                return json.loads(files[GIST_FILENAME]["content"])
            except:
                pass

    return {"date": today(), "rows": {}}

def save_state(state: Dict):
    url = f"https://api.github.com/gists/{GIST_ID}"
    requests.patch(
        url,
        headers={
            "Authorization": f"Bearer {GIST_TOKEN}",
            "Accept": "application/vnd.github+json"
        },
        json={
            "files": {
                GIST_FILENAME: {
                    "content": json.dumps(state, indent=2, ensure_ascii=False)
                }
            }
        },
        timeout=30
    )

# ================= PARSER =================
def parse_report_from_json(payload: dict) -> List[Dict]:
    rows = []
    raw = payload.get("rows")
    if not isinstance(raw, list):
        return rows

    for r in raw:
        dims = r.get("dimensions", {})
        if isinstance(dims, dict):
            rr = dict(dims)
            rr.update(r)
            r = rr

        country = r.get("country") or r.get("geo") or ""
        creative = r.get("creative_id") or ""
        sub2 = r.get("sub_id_2") or ""

        leads = as_float(r.get("conversions"))
        sales = as_float(r.get("sales"))
        revenue = as_float(r.get("revenue"))

        if not (country or creative or sub2):
            continue

        rows.append({
            "k": f"{country}|{creative}|{sub2}",
            "country": country,
            "creative": creative,
            "sub2": sub2,
            "leads": leads,
            "sales": sales,
            "revenue": revenue,
        })

    return rows

# ================= FETCH =================
def fetch_rows() -> List[Dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        )
        page = ctx.new_page()

        # -------- LOGIN --------
        page.goto("https://digitaltraff.click/admin/", wait_until="domcontentloaded")

        page.fill("input[name='login'], input[type='text']", LOGIN_USER)
        page.fill("input[name='password'], input[type='password']", LOGIN_PASS)

        try:
            page.get_by_role("button", name=re.compile("sign in|log in|увійти|войти", re.I)).click()
        except:
            page.keyboard.press("Enter")

        try:
            page.wait_for_selector("app-login", state="detached", timeout=15000)
        except PWTimeout:
            pass

        captured = []
        best_score = -1.0

        def on_response(resp):
            nonlocal captured, best_score
            try:
                data = resp.json()
            except:
                return

            rows = parse_report_from_json(data)
            if not rows:
                return

            score = sum(r["leads"] + r["sales"] + r["revenue"] for r in rows)
            if score > best_score:
                captured = rows
                best_score = score

        ctx.on("response", on_response)

        page.goto(PAGE_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass

        time.sleep(1.5)
        browser.close()

        return captured

# ================= MAIN =================
def main():
    state = load_state()
    prev_rows = state.get("rows", {})
    prev_date = state.get("date")

    rows = fetch_rows()
    if not rows:
        return

    if prev_date != today():
        save_state({"date": today(), "rows": {r["k"]: r for r in rows}})
        return

    new_rows = {}
    lead_msgs = []
    sale_msgs = []

    for r in rows:
        k = r["k"]
        old = prev_rows.get(k, {"leads": 0, "sales": 0, "revenue": 0})

        if r["leads"] - old["leads"] > EPS:
            lead_msgs.append(
                "🟩 *LEAD ALERT*\n"
                f"Country: {r['country']}\n"
                f"Creative ID: {r['creative']}\n"
                f"Sub ID 2: {r['sub2']}\n"
                f"Leads: {int(old['leads'])} → {int(r['leads'])}"
            )

        if r["sales"] - old["sales"] > EPS:
            delta_rev = r["revenue"] - old["revenue"]
            sale_msgs.append(
                "🟦 *SALE ALERT*\n"
                f"Country: {r['country']}\n"
                f"Creative ID: {r['creative']}\n"
                f"Sub ID 2: {r['sub2']}\n"
                f"Sales: {int(old['sales'])} → {int(r['sales'])}\n"
                f"Revenue: {money(delta_rev)}"
            )

        new_rows[k] = r

    if lead_msgs or sale_msgs:
        tg_send("\n\n".join(lead_msgs + sale_msgs))

    save_state({"date": today(), "rows": new_rows})

if __name__ == "__main__":
    main()
