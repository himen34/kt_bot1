import os, json, time, re
from typing import Dict, List
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ================= ENV =================
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

KYIV_TZ = ZoneInfo("Europe/Kyiv")
DEBUG = os.getenv("DEBUG_LOG", "0") == "1"

pu = urlparse(PAGE_URL)
BASE_URL = f"{pu.scheme}://{pu.netloc}".rstrip("/")


# ================= LOG =================
LOG_BUF: List[str] = []

def ts():
    return datetime.now(KYIV_TZ).strftime("%H:%M:%S")

def log(msg):
    line = f"[{ts()}] {msg}"
    print(line, flush=True)
    if DEBUG:
        LOG_BUF.append(line)

def tg_send(text, markdown=True):
    for cid in CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={
                    "chat_id": cid,
                    "text": text,
                    "parse_mode": "Markdown" if markdown else None,
                    "disable_web_page_preview": True
                },
                timeout=15
            )
        except Exception:
            pass


# ================= UTILS =================
def today():
    return datetime.now(KYIV_TZ).strftime("%Y-%m-%d")

def as_int(v):
    try: return int(float(v or 0))
    except: return 0

def as_float(v):
    try: return float(v or 0)
    except: return 0.0


# ================= STATE =================
def load_state():
    r = requests.get(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"Bearer {GIST_TOKEN}"},
        timeout=20
    )
    if r.status_code == 200:
        files = r.json().get("files", {})
        if GIST_FILENAME in files:
            try:
                return json.loads(files[GIST_FILENAME]["content"])
            except:
                pass
    return {"date": today(), "rows": {}}

def save_state(state):
    requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"Bearer {GIST_TOKEN}"},
        json={"files": {GIST_FILENAME: {"content": json.dumps(state, indent=2)}}},
        timeout=20
    )


# ================= PARSER =================
def parse_rows(payload: dict) -> Dict[str, Dict]:
    result = {}

    for r in payload.get("rows", []):
        dims = r.get("dimensions", {})

        campaign = str(dims.get("campaign", "")).strip()
        country  = str(dims.get("country", "")).strip()
        creative = str(dims.get("creative_id", "")).strip()
        external = str(dims.get("external_id", "")).strip()

        if not (campaign or country or creative or external):
            continue

        key = f"{campaign}|{country}|{external}|{creative}"

        result[key] = {
            "campaign": campaign,
            "country": country,
            "external_id": external,
            "creative_id": creative,
            "conversions": as_int(r.get("conversions")),
            "sales": as_int(r.get("sales")),
            "revenue": as_float(
                r.get("revenue_confirmed")
                or r.get("confirmed_revenue")
                or 0
            )
        }

    return result


# ================= FETCH =================
def fetch_rows() -> Dict[str, Dict]:
    captured = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        def on_response(resp):
            nonlocal captured
            try:
                if not re.search(r"/report|/stats|/favourite", resp.url):
                    return
                data = resp.json()
            except:
                return

            if not isinstance(data, dict) or not data.get("rows"):
                return

            rows = parse_rows(data)
            if rows:
                captured = rows
                log(f"Captured rows: {len(rows)}")

        ctx.on("response", on_response)

        log("Login")
        page.goto(f"{BASE_URL}/admin/", wait_until="domcontentloaded")

        try:
            page.fill("input[type='text']", LOGIN_USER)
            page.fill("input[type='password']", LOGIN_PASS)
            page.click("button")
        except:
            pass

        try:
            page.wait_for_selector("app-login", state="detached", timeout=10000)
        except PWTimeout:
            pass

        log("Open report")
        page.goto(PAGE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        browser.close()

    return captured


# ================= MAIN =================
def main():
    log("START")

    state = load_state()
    prev_date = state["date"]
    prev_rows = state["rows"]

    rows = fetch_rows()
    if not rows:
        log("No data")
        return

    if prev_date != today():
        log("New day baseline")
        save_state({"date": today(), "rows": rows})
        return

    alerts = []

    for k, r in rows.items():
        old = prev_rows.get(k, {})

        if r["conversions"] > old.get("conversions", 0):
            alerts.append(
                f"🟩 *LEAD*\n"
                f"{r['campaign']} | {r['country']}\n"
                f"Creative: {r['creative_id']}\n"
                f"{old.get('conversions',0)} → {r['conversions']}"
            )

        if r["sales"] > old.get("sales", 0):
            delta_rev = r["revenue"] - old.get("revenue", 0)
            alerts.append(
                f"🟦 *SALE*\n"
                f"{r['campaign']} | {r['country']}\n"
                f"Creative: {r['creative_id']}\n"
                f"Sales: {old.get('sales',0)} → {r['sales']}\n"
                f"Revenue +${delta_rev:.2f}"
            )

    if alerts:
        tg_send("\n\n".join(alerts))

    save_state({"date": today(), "rows": rows})
    log("DONE")


if __name__ == "__main__":
    main()
