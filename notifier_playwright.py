import os, json, time
from typing import Dict, List
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright


# ================= ENV =================
LOGIN_USER = os.environ["LOGIN_USER"]
LOGIN_PASS = os.environ["LOGIN_PASS"]
PAGE_URL   = os.environ["PAGE_URL"]

TG_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

GIST_ID    = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = os.getenv("GIST_FILENAME", "keitaro_favourite_state.json")

TZ = ZoneInfo("Europe/Kyiv")
EPS = 0.0001


# ================= TIME =================
def today_str():
    return datetime.now(TZ).strftime("%Y-%m-%d")


# ================= TELEGRAM =================
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


# ================= GIST STATE =================
def load_state() -> Dict:
    r = requests.get(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"Bearer {GIST_TOKEN}"},
        timeout=30
    )
    if r.status_code == 200:
        files = r.json().get("files", {})
        if GIST_FILENAME in files:
            try:
                return json.loads(files[GIST_FILENAME]["content"])
            except:
                pass
    return {"date": today_str(), "rows": {}}


def save_state(state: Dict):
    requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"Bearer {GIST_TOKEN}"},
        json={
            "files": {
                GIST_FILENAME: {
                    "content": json.dumps(state, indent=2)
                }
            }
        },
        timeout=30
    )


# ================= HELPERS =================
def as_float(v):
    try:
        return float(v or 0)
    except:
        return 0.0


# ================= FETCH FROM KEITARO =================
def fetch_rows() -> List[Dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        # LOGIN
        page.goto("https://digitaltraff.click/admin/", wait_until="domcontentloaded")
        page.fill("input[name='login']", LOGIN_USER)
        page.fill("input[name='password']", LOGIN_PASS)
        page.click("button[type='submit']")
        page.wait_for_timeout(3000)

        # Favourite report page
        page.goto(PAGE_URL, wait_until="networkidle")
        page.wait_for_timeout(2000)

        # DIRECT API CALL (no XHR hooks)
        data = page.evaluate("""
        async () => {
            const res = await fetch('/admin/api/reports/favourite/1', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({})
            });
            return await res.json();
        }
        """)

        browser.close()

    rows = []
    for r in data.get("rows", []):
        dims = r.get("dimensions", {}) or {}

        def g(k):
            return r.get(k) or dims.get(k) or ""

        rows.append({
            "k": f"{g('campaign')}|{g('country')}|{g('creative_id')}",
            "campaign": str(g("campaign")),
            "country": str(g("country")),
            "creative": str(g("creative_id")),
            "conversions": as_float(r.get("conversions")),
            "sales": as_float(r.get("sales")),
        })

    return rows


# ================= MAIN =================
def main():
    state = load_state()
    today = today_str()

    rows = fetch_rows()

    # 🔧 FIX: Keitaro may temporarily return empty rows
    if not rows:
        if state.get("rows"):
            # silently ignore temporary empty response
            return
        else:
            tg_send("⚠️ Keitaro: no data")
            return

    # Daily reset
    if state["date"] != today:
        save_state({"date": today, "rows": {r["k"]: r for r in rows}})
        return

    alerts = []
    new_map = {}

    for r in rows:
        old = state["rows"].get(
            r["k"],
            {"conversions": 0, "sales": 0}
        )

        # CONVERSIONS
        if r["conversions"] > old["conversions"]:
            alerts.append(
                "🟩 *CONVERSION ALERT*\n"
                f"Campaign: {r['campaign']}\n"
                f"Country: {r['country']}\n"
                f"Creative: {r['creative']}\n"
                f"Conversions: {int(old['conversions'])} → {int(r['conversions'])}"
            )

        # SALES
        if r["sales"] > old["sales"]:
            alerts.append(
                "🟦 *SALE ALERT*\n"
                f"Campaign: {r['campaign']}\n"
                f"Country: {r['country']}\n"
                f"Creative: {r['creative']}\n"
                f"Sales: {int(old['sales'])} → {int(r['sales'])}"
            )

        new_map[r["k"]] = r

    if alerts:
        tg_send("\n\n".join(alerts))

    save_state({"date": today, "rows": new_map})


if __name__ == "__main__":
    main()
