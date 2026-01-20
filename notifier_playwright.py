# notifier_playwright.py — стабильные алерты без дублей (Keitaro Campaigns Report)

import os, json, time, re
from typing import Dict, List, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

# ========= ENV =========
LOGIN_USER = os.environ["LOGIN_USER"]
LOGIN_PASS = os.environ["LOGIN_PASS"]
PAGE_URL   = os.environ["PAGE_URL"]

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID_1 = os.getenv("TELEGRAM_CHAT_ID_1") or os.getenv("TELEGRAM_CHAT_ID")
TG_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2")
CHAT_IDS = [cid for cid in (TG_CHAT_ID_1, TG_CHAT_ID_2) if cid]

GIST_ID    = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = os.getenv("GIST_FILENAME", "keitaro_state.json")

SPEND_DIR = (os.getenv("SPEND_DIRECTION", "both") or "both").lower()
KYIV_TZ   = ZoneInfo(os.getenv("KYIV_TZ", "Europe/Kyiv"))

EPS = 0.009

# ========= utils =========
def now_kyiv():
    return datetime.now(KYIV_TZ)

def kyiv_today_str():
    return now_kyiv().strftime("%Y-%m-%d")

def fmt_money(x: float):
    return f"${x:,.2f}"

def direction_ok(delta: float):
    if SPEND_DIR == "up":
        return delta > EPS
    if SPEND_DIR == "down":
        return delta < -EPS
    return abs(delta) > EPS

# ========= GIST =========
def load_state():
    url = f"https://api.github.com/gists/{GIST_ID}"
    r = requests.get(url, headers={
        "Authorization": f"Bearer {GIST_TOKEN}",
        "Accept": "application/vnd.github+json"
    })
    if r.status_code == 200:
        files = r.json().get("files", {})
        if GIST_FILENAME in files:
            try:
                return json.loads(files[GIST_FILENAME]["content"])
            except:
                pass
    return {"date": kyiv_today_str(), "rows": {}}

def save_state(state):
    url = f"https://api.github.com/gists/{GIST_ID}"
    files = {GIST_FILENAME: {"content": json.dumps(state, indent=2)}}
    requests.patch(url, headers={
        "Authorization": f"Bearer {GIST_TOKEN}",
        "Accept": "application/vnd.github+json"
    }, json={"files": files})

# ========= Telegram =========
def tg_send(text: str):
    for cid in CHAT_IDS:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": cid, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )

# ========= parsing =========
def as_float(v):
    try:
        return float(v or 0)
    except:
        return 0.0

def parse_report_from_json(payload: dict):
    rows = []
    for r in payload.get("rows", []):
        dims = r.get("dimensions", {})
        country = dims.get("country", "")
        creative = dims.get("creative_id", "")
        sub2 = dims.get("sub_id_2", "")

        rows.append({
            "k": f"{country}|{creative}|{sub2}",
            "country": country,
            "creative": creative,
            "sub2": sub2,
            "leads": as_float(r.get("conversions")),
            "sales": as_float(r.get("sales")),
            "revenue": as_float(r.get("revenue")),
            "cost": as_float(r.get("cost")),
        })
    return rows

# ========= fetch =========
def fetch_rows():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        # ---- LOGIN ----
        page.goto("https://digitaltraff.click/admin/", wait_until="domcontentloaded")

        page.wait_for_selector("app-login", timeout=20000)

        page.fill("input[placeholder='Username'], input[name='login']", LOGIN_USER)
        page.fill("input[placeholder='Password'], input[name='password']", LOGIN_PASS)
        page.click("button:has-text('Sign in')")

        page.wait_for_selector("keitaro-app", timeout=20000)

        # ---- XHR CAPTURE ----
        captured = []

        def on_response(resp):
            if "/admin/api/reports/campaigns" in resp.url:
                try:
                    data = resp.json()
                    rows = parse_report_from_json(data)
                    if rows:
                        captured.extend(rows)
                except:
                    pass

        ctx.on("response", on_response)

        # ---- OPEN REPORT ----
        page.goto(PAGE_URL, wait_until="domcontentloaded")

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except:
            pass

        # 🔴 CRITICAL: force refresh to trigger XHR
        try:
            page.click("button[aria-label='Refresh'], button:has-text('Refresh')", timeout=5000)
        except:
            pass

        time.sleep(3)

        browser.close()
        return captured

# ========= main =========
def main():
    state = load_state()
    prev = state.get("rows", {})
    today = kyiv_today_str()

    rows = fetch_rows()
    if not rows:
        tg_send("⚠️ No data fetched")
        return

    new_rows = {}
    lead_msgs = []
    sale_msgs = []

    for r in rows:
        k = r["k"]
        old = prev.get(k, {"leads": 0, "sales": 0, "revenue": 0})

        # LEADS
        if r["leads"] > old["leads"]:
            lead_msgs.append(
                f"🟩 *LEAD*\n"
                f"{r['country']} | {r['creative']} | {r['sub2']}\n"
                f"{int(old['leads'])} → {int(r['leads'])}"
            )

        # SALES
        if r["sales"] > old["sales"]:
            delta_rev = r["revenue"] - old.get("revenue", 0)
            sale_msgs.append(
                f"🟦 *SALE*\n"
                f"{r['country']} | {r['creative']} | {r['sub2']}\n"
                f"Sales: {int(old['sales'])} → {int(r['sales'])}\n"
                f"Revenue +{fmt_money(delta_rev)}"
            )

        new_rows[k] = r

    msgs = lead_msgs + sale_msgs
    if msgs:
        tg_send("\n\n".join(msgs))

    save_state({"date": today, "rows": new_rows})

if __name__ == "__main__":
    main()
