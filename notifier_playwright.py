import os, json, time
from typing import Dict, List
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

from playwright.sync_api import sync_playwright, TimeoutError

# ====== ENV ======
PAGE_URL = os.environ["PAGE_URL"]

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_IDS = [os.environ.get("TELEGRAM_CHAT_ID_1"), os.environ.get("TELEGRAM_CHAT_ID_2")]
CHAT_IDS = [c for c in CHAT_IDS if c]

GIST_ID = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = os.getenv("GIST_FILENAME", "keitaro_state.json")

TZ = ZoneInfo("Europe/Warsaw")
EPS = 0.0001

# ====== helpers ======
def now_day():
    return datetime.now(TZ).strftime("%Y-%m-%d")

def money(x):
    return f"${x:,.2f}"

def tg_send(msg):
    for cid in CHAT_IDS:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": cid, "text": msg, "parse_mode": "Markdown"},
            timeout=20
        )

# ====== GIST ======
def load_state():
    r = requests.get(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"Bearer {GIST_TOKEN}"},
        timeout=20
    )
    if r.status_code == 200:
        try:
            return json.loads(r.json()["files"][GIST_FILENAME]["content"])
        except:
            pass
    return {"date": now_day(), "rows": {}}

def save_state(state):
    requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"Bearer {GIST_TOKEN}"},
        json={"files": {GIST_FILENAME: {"content": json.dumps(state, indent=2)}}},
        timeout=20
    )

# ====== PARSE TABLE ======
def fetch_rows():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state="storage_state.json")
        page = ctx.new_page()

        page.goto(PAGE_URL, wait_until="networkidle")

        try:
            page.wait_for_selector("table", timeout=15000)
        except TimeoutError:
            return []

        rows = []
        for tr in page.query_selector_all("tbody tr"):
            tds = tr.query_selector_all("td")
            if len(tds) < 7:
                continue

            def t(i):
                return tds[i].inner_text().strip()

            rows.append({
                "country": t(0),
                "creative": t(1),
                "sub2": t(2),
                "conversions": float(t(5) or 0),
                "sales": float(t(6) or 0),
                "revenue": float(t(7).replace("$", "").replace(",", "") or 0),
            })

        browser.close()
        return rows

# ====== MAIN ======
def main():
    state = load_state()
    prev_rows = state["rows"]
    today = now_day()

    rows = fetch_rows()
    if not rows:
        tg_send("accs on vacation...")
        return

    if state["date"] != today:
        save_state({"date": today, "rows": {}})
        tg_send("accs on vacation...")
        return

    new_rows = {}
    alerts = []

    for r in rows:
        k = f"{r['country']}|{r['creative']}|{r['sub2']}"
        old = prev_rows.get(k, {"conversions": 0, "sales": 0, "revenue": 0})

        if r["conversions"] > old["conversions"]:
            alerts.append(
                f"🟩 *LEAD*\n"
                f"Country: {r['country']}\n"
                f"Creative: {r['creative']}\n"
                f"SubID2: {r['sub2']}"
            )

        if r["sales"] > old["sales"]:
            delta = r["revenue"] - old["revenue"]
            alerts.append(
                f"🟦 *SALE*\n"
                f"Country: {r['country']}\n"
                f"Creative: {r['creative']}\n"
                f"SubID2: {r['sub2']}\n"
                f"Revenue: {money(delta)}"
            )

        new_rows[k] = r

    if alerts:
        tg_send("\n\n".join(alerts))

    save_state({"date": today, "rows": new_rows})

if __name__ == "__main__":
    main()
