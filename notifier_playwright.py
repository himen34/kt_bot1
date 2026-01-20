# notifier_playwright.py — TG DEBUG VERSION

import os, json, time, re
from typing import Dict, List
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright


# ========= ENV =========
LOGIN_USER = os.environ["LOGIN_USER"]
LOGIN_PASS = os.environ["LOGIN_PASS"]
PAGE_URL   = os.environ["PAGE_URL"]

BASE_URL = "https://digitaltraff.click"

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

GIST_ID    = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = "keitaro_debug_state.json"

DEBUG = os.getenv("DEBUG_LOG", "1") == "1"

TZ = ZoneInfo("Europe/Kyiv")


# ========= LOGGING =========
LOG_BUFFER = []

def tg_send(text: str):
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        },
        timeout=20
    )

def log(msg: str):
    print(msg, flush=True)
    LOG_BUFFER.append(msg)

def debug(msg: str):
    if DEBUG:
        log(f"[DEBUG] {msg}")

def flush_logs_to_tg():
    if not LOG_BUFFER:
        return
    text = "\n".join(LOG_BUFFER[-40:])
    tg_send("🧪 *DEBUG LOG*\n```\n" + text + "\n```")


# ========= HELPERS =========
def today():
    return datetime.now(TZ).strftime("%Y-%m-%d")

def as_int(v):
    try:
        return int(float(v or 0))
    except:
        return 0


# ========= STATE =========
def load_state():
    try:
        r = requests.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={"Authorization": f"Bearer {GIST_TOKEN}"},
            timeout=20
        )
        content = r.json()["files"][GIST_FILENAME]["content"]
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


# ========= FETCH =========
def fetch_rows():
    captured = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        debug("Opening login page")
        page.goto(f"{BASE_URL}/admin/", wait_until="domcontentloaded")

        page.fill("input[name='login']", LOGIN_USER)
        page.fill("input[name='password']", LOGIN_PASS)
        page.click("button[type='submit']")
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

            rows = []
            for r in data.get("rows", []):
                d = r.get("dimensions", {})
                if not d.get("campaign"):
                    continue
                rows.append({
                    "k": f"{d.get('campaign')}|{d.get('country')}|{d.get('creative_id')}",
                    "campaign": d.get("campaign"),
                    "country": d.get("country"),
                    "creative": d.get("creative_id"),
                    "conversions": as_int(r.get("conversions")),
                    "sales": as_int(r.get("sales")),
                })

            if rows:
                debug(f"XHR rows captured: {len(rows)}")
                captured = rows

        ctx.on("response", on_response)

        debug("Opening report page")
        page.goto(PAGE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)

        browser.close()

    return captured


# ========= MAIN =========
def main():
    debug("Script started")

    state = load_state()
    prev_rows = state.get("rows", {})
    prev_date = state.get("date")

    rows = fetch_rows()

    if not rows:
        log("⚠️ Keitaro: no data")
        flush_logs_to_tg()
        return

    debug(f"Rows fetched: {len(rows)}")

    if prev_date != today():
        debug("New day → baseline saved")
        save_state({"date": today(), "rows": {r["k"]: r for r in rows}})
        flush_logs_to_tg()
        return

    messages = []
    new_map = {}

    for r in rows:
        old = prev_rows.get(r["k"], {})
        debug(f"Row {r['k']} conv={r['conversions']} sales={r['sales']}")

        if r["conversions"] > old.get("conversions", 0):
            messages.append(
                f"🟩 *CONVERSION ALERT*\n"
                f"Campaign: {r['campaign']}\n"
                f"Country: {r['country']}\n"
                f"Creative: {r['creative']}\n"
                f"{old.get('conversions',0)} → {r['conversions']}"
            )

        if r["sales"] > old.get("sales", 0):
            messages.append(
                f"🟦 *SALE ALERT*\n"
                f"Campaign: {r['campaign']}\n"
                f"Country: {r['country']}\n"
                f"Creative: {r['creative']}\n"
                f"{old.get('sales',0)} → {r['sales']}"
            )

        new_map[r["k"]] = r

    if messages:
        tg_send("\n\n".join(messages))
        debug(f"Alerts sent: {len(messages)}")
    else:
        debug("No alerts triggered")

    save_state({"date": today(), "rows": new_map})
    debug("State saved")

    flush_logs_to_tg()


if __name__ == "__main__":
    main()
