# notifier_playwright.py — Keitaro campaigns report (FULL LOGIC)

import os, json, time
from typing import Dict, List
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
TG_CHAT_ID_1 = os.getenv("TELEGRAM_CHAT_ID_1")
TG_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2")
CHAT_IDS = [c for c in (TG_CHAT_ID_1, TG_CHAT_ID_2) if c]

GIST_ID    = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = "keitaro_campaign_state.json"

KYIV_TZ = ZoneInfo("Europe/Kyiv")
EPS = 0.0001

# ========= UTILS =========
def now():
    return datetime.now(KYIV_TZ)

def today():
    return now().strftime("%Y-%m-%d")

def f(v):
    try:
        return float(v)
    except:
        return 0.0

def money(x):
    return f"${x:,.2f}"

# ========= GIST =========
def load_state():
    r = requests.get(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"Bearer {GIST_TOKEN}"},
        timeout=30
    )
    if r.status_code == 200:
        files = r.json().get("files", {})
        if GIST_FILENAME in files:
            return json.loads(files[GIST_FILENAME]["content"])
    return {"date": today(), "rows": {}}

def save_state(state):
    requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"Bearer {GIST_TOKEN}"},
        json={"files": {GIST_FILENAME: {"content": json.dumps(state, indent=2)}}},
        timeout=30
    )

# ========= TELEGRAM =========
def tg_send(text):
    for cid in CHAT_IDS:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": cid, "text": text, "parse_mode": "Markdown"},
            timeout=20
        )

# ========= PARSER =========
def parse_keitaro(payload: dict) -> List[Dict]:
    rows = []
    for r in payload.get("rows", []):
        d = r.get("dimensions", {})
        rows.append({
            "k": f"{d.get('country')}|{d.get('creative_id')}|{d.get('sub_id_2')}",
            "country": d.get("country"),
            "creative": d.get("creative_id"),
            "sub2": d.get("sub_id_2"),

            "cost": f(r.get("cost")),
            "leads": f(r.get("conversions")),   # 👈 IMPORTANT
            "sales": f(r.get("sales")),
        })
    return rows

# ========= FETCH =========
def fetch_rows():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        page.goto("https://digitaltraff.click/admin/", wait_until="domcontentloaded")
        page.fill("input[name='email']", LOGIN_USER)
        page.fill("input[name='password']", LOGIN_PASS)
        page.click("button[type='submit']")

        try:
            page.wait_for_selector("text=Дашборд", timeout=15000)
        except PWTimeout:
            pass

        captured = []

        def on_resp(resp):
            nonlocal captured
            if "/admin/api/reports/campaigns" in resp.url:
                try:
                    data = resp.json()
                    rows = parse_keitaro(data)
                    if rows:
                        captured = rows
                except:
                    pass

        ctx.on("response", on_resp)
        page.goto(PAGE_URL, wait_until="domcontentloaded")
        time.sleep(5)

        browser.close()
        return captured

# ========= MAIN =========
def main():
    state = load_state()
    prev_date = state["date"]
    prev_rows = state["rows"]

    rows = fetch_rows()
    if not rows:
        tg_send("⚠️ Keitaro: no data")
        return

    if prev_date != today():
        save_state({"date": today(), "rows": {r["k"]: r for r in rows}})
        tg_send("🔄 New day — baseline reset")
        return

    spend_msgs = []
    lead_msgs = []
    sale_msgs = []

    new_map = {}

    for r in rows:
        k = r["k"]
        old = prev_rows.get(k)

        if old:
            # ---- SPEND ----
            dc = r["cost"] - old["cost"]
            if abs(dc) > EPS:
                spend_msgs.append(
                    f"🧊 *SPEND ALERT*\n"
                    f"{r['country']} | {r['creative']} | {r['sub2']}\n"
                    f"{money(old['cost'])} → {money(r['cost'])}"
                )

            # ---- LEADS ----
            dl = r["leads"] - old["leads"]
            if dl > EPS:
                lead_msgs.append(
                    f"🟩 *LEAD ALERT*\n"
                    f"{r['country']} | {r['creative']} | {r['sub2']}\n"
                    f"{int(old['leads'])} → {int(r['leads'])}"
                )

            # ---- SALES ----
            ds = r["sales"] - old["sales"]
            if ds > EPS:
                sale_msgs.append(
                    f"🟦 *SALE ALERT*\n"
                    f"{r['country']} | {r['creative']} | {r['sub2']}\n"
                    f"{int(old['sales'])} → {int(r['sales'])}"
                )

        new_map[k] = r

    msgs = spend_msgs + lead_msgs + sale_msgs
    if msgs:
        tg_send("\n\n".join(msgs))

    save_state({"date": today(), "rows": new_map})

if __name__ == "__main__":
    main()
