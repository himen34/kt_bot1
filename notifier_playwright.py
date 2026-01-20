import os, json, time, re
from typing import Dict, List
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

# ================= ENV =================
LOGIN_USER = os.environ["LOGIN_USER"]
LOGIN_PASS = os.environ["LOGIN_PASS"]
PAGE_URL   = os.environ["PAGE_URL"]

BASE_URL = "https://digitaltraff.click"

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

GIST_ID    = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = "keitaro_favourite_state.json"

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


# ================= GIST =================
def load_state() -> Dict:
    r = requests.get(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"Bearer {GIST_TOKEN}"},
        timeout=20
    )
    if r.status_code == 200:
        files = r.json().get("files", {})
        if GIST_FILENAME in files:
            return json.loads(files[GIST_FILENAME]["content"])
    return {"date": today_str(), "rows": {}}


def save_state(state: Dict):
    requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"Bearer {GIST_TOKEN}"},
        json={"files": {GIST_FILENAME: {"content": json.dumps(state, indent=2)}}},
        timeout=20
    )


# ================= PARSE =================
def as_float(v):
    try:
        return float(v)
    except:
        return 0.0


# ================= FETCH FROM PAGE =================
def fetch_rows_from_page() -> List[Dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        # LOGIN
        page.goto(f"{BASE_URL}/admin/", wait_until="domcontentloaded")
        page.get_by_placeholder("Username").fill(LOGIN_USER)
        page.get_by_placeholder("Password").fill(LOGIN_PASS)
        page.get_by_role("button", name=re.compile("sign in", re.I)).click()

        page.wait_for_timeout(2000)

        # OPEN REPORT
        page.goto(PAGE_URL, wait_until="domcontentloaded")

        # ⏳ ждём пока Angular реально загрузит данные
        page.wait_for_timeout(5000)

        # 🔥 ЧИТАЕМ ДАННЫЕ ПРЯМО ИЗ ПРИЛОЖЕНИЯ
        rows = page.evaluate("""
        () => {
            try {
                const app = window.ng || window.__ngContext__ || null;
                const services = Object.values(window)
                  .filter(v => v && typeof v === 'object' && v.constructor?.name?.includes('Report'));

                for (const s of services) {
                    if (s.rows && Array.isArray(s.rows)) {
                        return s.rows;
                    }
                }
            } catch(e) {}
            return [];
        }
        """)

        browser.close()
        return rows


# ================= MAIN =================
def main():
    state = load_state()
    today = today_str()

    raw_rows = fetch_rows_from_page()
    if not raw_rows:
        tg_send("⚠️ Keitaro: no data")
        return

    rows = []
    for r in raw_rows:
        d = r.get("dimensions", {})
        rows.append({
            "k": f"{d.get('campaign')}|{d.get('country')}|{d.get('external_id')}|{d.get('creative_id')}",
            "campaign": d.get("campaign"),
            "country": d.get("country"),
            "external_id": d.get("external_id"),
            "creative_id": d.get("creative_id"),
            "conversions": as_float(r.get("conversions")),
            "sales": as_float(r.get("sales")),
            "revenue": as_float(r.get("sale_revenue") or r.get("deposit_revenue")),
        })

    if state["date"] != today:
        save_state({"date": today, "rows": {r["k"]: r for r in rows}})
        return

    prev = state["rows"]
    new_state = {}
    alerts = []

    for r in rows:
        old = prev.get(r["k"], {})
        header = f"{r['campaign']} | {r['country']} | {r['creative_id']}"

        if r["conversions"] > old.get("conversions", 0) + EPS:
            alerts.append(
                "🟩 *CONVERSION ALERT*\n"
                f"{header}\n"
                f"{int(old.get('conversions',0))} → {int(r['conversions'])}"
            )

        if r["sales"] > old.get("sales", 0) + EPS:
            alerts.append(
                "🟦 *SALE ALERT*\n"
                f"{header}\n"
                f"{int(old.get('sales',0))} → {int(r['sales'])}\n"
                f"Revenue: ${r['revenue']:.2f}"
            )

        new_state[r["k"]] = r

    if alerts:
        tg_send("\\n\\n".join(alerts))

    save_state({"date": today, "rows": new_state})


if __name__ == "__main__":
    main()
