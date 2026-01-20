# notifier_playwright.py — DEBUG VERSION (логируем ВСЁ)

import os, json, time, re
from typing import Dict, List
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ========= ENV =========
LOGIN_USER = os.environ["LOGIN_USER"]
LOGIN_PASS = os.environ["LOGIN_PASS"]
PAGE_URL   = os.environ["PAGE_URL"]

BASE_URL = (os.getenv("BASE_URL", "https://digitaltraff.click")).rstrip("/")

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GIST_ID    = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = "keitaro_debug_state.json"

DEBUG = os.getenv("DEBUG_LOG", "0") == "1"

KYIV_TZ = ZoneInfo("Europe/Kyiv")
EPS = 0.0001


# ========= helpers =========
def log(msg: str):
    print(msg, flush=True)

def debug(msg: str):
    if DEBUG:
        log(f"[DEBUG] {msg}")

def tg_send(text: str):
    if not TG_CHAT_ID:
        return
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": text},
        timeout=20
    )


def today():
    return datetime.now(KYIV_TZ).strftime("%Y-%m-%d")

def as_int(v):
    try:
        return int(float(v or 0))
    except:
        return 0

def as_float(v):
    try:
        return float(v or 0)
    except:
        return 0.0


# ========= state =========
def load_state():
    try:
        r = requests.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={"Authorization": f"Bearer {GIST_TOKEN}"},
            timeout=20
        )
        data = r.json()
        content = data["files"][GIST_FILENAME]["content"]
        return json.loads(content)
    except Exception:
        return {"date": today(), "rows": {}}

def save_state(state):
    requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"Bearer {GIST_TOKEN}"},
        json={"files": {GIST_FILENAME: {"content": json.dumps(state)}}},
        timeout=20
    )


# ========= parsing =========
def parse_rows(payload):
    rows = []
    for r in payload.get("rows", []):
        dims = r.get("dimensions", {})
        campaign = dims.get("campaign", "")
        country  = dims.get("country", "")
        external = dims.get("external_id", "")
        creative = dims.get("creative_id", "")

        if not campaign:
            continue

        rows.append({
            "k": f"{campaign}|{country}|{external}|{creative}",
            "campaign": campaign,
            "country": country,
            "external": external,
            "creative": creative,
            "conversions": as_int(r.get("conversions")),
            "sales": as_int(r.get("sales")),
            "revenue": as_float(r.get("deposit_revenue") or r.get("sale_revenue")),
        })
    return rows


# ========= fetch =========
def fetch_rows():
    captured = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        page.goto(f"{BASE_URL}/admin/", wait_until="domcontentloaded")

        page.fill("input[name='login'], input[type='text']", LOGIN_USER)
        page.fill("input[name='password'], input[type='password']", LOGIN_PASS)
        page.get_by_role("button").click()

        page.wait_for_timeout(3000)
        debug("Logged in")

        def on_response(resp):
            nonlocal captured
            try:
                data = resp.json()
            except:
                return
            if not isinstance(data, dict):
                return
            if "rows" not in data:
                return

            rows = parse_rows(data)
            if rows:
                debug(f"XHR captured rows={len(rows)}")
                captured = rows

        ctx.on("response", on_response)

        page.goto(PAGE_URL, wait_until="domcontentloaded")
        debug("Report page loaded")

        page.wait_for_timeout(5000)
        browser.close()

    return captured


# ========= main =========
def main():
    debug("Script started")

    state = load_state()
    prev_rows = state.get("rows", {})
    prev_date = state.get("date")

    rows = fetch_rows()

    if not rows:
        log("NO DATA FROM KEITARO")
        return

    debug(f"Rows fetched: {len(rows)}")

    if prev_date != today():
        debug("New day, saving baseline")
        save_state({"date": today(), "rows": {r["k"]: r for r in rows}})
        return

    messages = []
    new_map = {}

    for r in rows:
        old = prev_rows.get(r["k"], {})
        debug(f"Row {r['k']} conv={r['conversions']} sales={r['sales']}")

        if r["conversions"] > old.get("conversions", 0):
            messages.append(
                f"🟩 CONVERSION\n{r['campaign']} | {r['country']} | {r['creative']}\n"
                f"{old.get('conversions',0)} → {r['conversions']}"
            )
            debug("Conversion alert triggered")

        if r["sales"] > old.get("sales", 0):
            messages.append(
                f"🟦 SALE\n{r['campaign']} | {r['country']} | {r['creative']}\n"
                f"{old.get('sales',0)} → {r['sales']}"
            )
            debug("Sale alert triggered")

        new_map[r["k"]] = r

    if messages:
        debug(f"Sending {len(messages)} alerts")
        tg_send("\n\n".join(messages))
    else:
        debug("No alerts triggered")

    save_state({"date": today(), "rows": new_map})
    debug("State saved")


if __name__ == "__main__":
    main()
