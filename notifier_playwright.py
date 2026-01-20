import os, json, time, re
from typing import Dict, List
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ================= CONFIG =================
BASE_URL = "https://digitaltraff.click"   # твой Keitaro
DEBUG = False

LOGIN_USER = os.environ["LOGIN_USER"]
LOGIN_PASS = os.environ["LOGIN_PASS"]
PAGE_URL   = os.environ["PAGE_URL"]

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

GIST_ID    = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILE  = "keitaro_today_cpa.json"

TZ = ZoneInfo("Europe/Kyiv")
EPS = 0.001


# ================= TIME =================
def today_key() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


# ================= HELPERS =================
def as_float(v):
    try:
        return float(v or 0)
    except:
        return 0.0


# ================= TELEGRAM =================
def tg_send(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
    except:
        pass


# ================= GIST =================
def load_state() -> Dict:
    r = requests.get(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"Bearer {GIST_TOKEN}"},
        timeout=20,
    )
    if r.status_code == 200:
        files = r.json().get("files", {})
        if GIST_FILE in files:
            try:
                return json.loads(files[GIST_FILE]["content"])
            except:
                pass
    return {"date": today_key(), "rows": {}}


def save_state(state: Dict):
    requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"Bearer {GIST_TOKEN}"},
        json={
            "files": {
                GIST_FILE: {
                    "content": json.dumps(state, ensure_ascii=False, indent=2)
                }
            }
        },
        timeout=20,
    ).raise_for_status()


# ================= PARSE JSON =================
def parse_today_cpa(payload: dict) -> List[Dict]:
    rows = []
    for r in payload.get("rows", []):
        dims = r.get("dimensions", {}) or {}

        def g(k):
            return r.get(k) or dims.get(k) or ""

        row = {
            "k": f"{g('campaign')}|{g('country')}|{g('external_id')}|{g('creative_id')}",
            "campaign": str(g("campaign")),
            "country": str(g("country")),
            "external_id": str(g("external_id")),
            "creative_id": str(g("creative_id")),
            "conversions": as_float(r.get("conversions")),
            "sales": as_float(r.get("sales")),
            "revenue": as_float(r.get("deposit_revenue") or r.get("sale_revenue")),
            "cpa": as_float(r.get("cpa")),
        }
        rows.append(row)
    return rows


def aggregate_max(rows: List[Dict]) -> List[Dict]:
    acc = {}
    for r in rows:
        k = r["k"]
        if k not in acc:
            acc[k] = dict(r)
        else:
            a = acc[k]
            a["conversions"] = max(a["conversions"], r["conversions"])
            a["sales"] = max(a["sales"], r["sales"])
            a["revenue"] = max(a["revenue"], r["revenue"])
            a["cpa"] = max(a["cpa"], r["cpa"])
    return list(acc.values())


# ================= FETCH =================
def fetch_rows() -> List[Dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        # LOGIN
        page.goto(f"{BASE_URL}/admin/", wait_until="domcontentloaded")
        page.fill("input[name='login'], input[type='text']", LOGIN_USER)
        page.fill("input[name='password'], input[type='password']", LOGIN_PASS)
        page.click("button[type='submit']")

        try:
            page.wait_for_selector("app-login", state="detached", timeout=15000)
        except PWTimeout:
            pass

        captured = []
        best_len = 0

        def on_response(resp):
            nonlocal captured, best_len
            url = resp.url.lower()

            if DEBUG:
                print("XHR:", url)

            # 🔥 КЛЮЧЕВОЙ ФИЛЬТР
            if "/admin/api/reports/favourite" not in url:
                return

            try:
                data = resp.json()
            except:
                return

            rows = parse_today_cpa(data)
            if rows and len(rows) > best_len:
                captured = rows
                best_len = len(rows)

        ctx.on("response", on_response)

        # ОТКРЫВАЕМ ОТЧЁТ
        page.goto(PAGE_URL, wait_until="domcontentloaded")
        time.sleep(3)

        browser.close()
        return aggregate_max(captured)


# ================= MAIN =================
def main():
    state = load_state()
    today = today_key()

    rows = fetch_rows()
    if not rows:
        tg_send("⚠️ Keitaro: no data")
        return

    if state["date"] != today:
        save_state({"date": today, "rows": {r["k"]: r for r in rows}})
        return

    prev = state["rows"]
    new_map = {}

    alerts = []

    for r in rows:
        k = r["k"]
        old = prev.get(k)

        header = (
            f"{r['campaign']} | {r['country']} | "
            f"{r['external_id']} | {r['creative_id']}"
        )

        if old:
            if r["conversions"] - old.get("conversions", 0) > EPS:
                alerts.append(
                    "🟩 *LEAD ALERT*\n"
                    f"{header}\n"
                    f"Conv: {int(old['conversions'])} → {int(r['conversions'])} • CPA: ${r['cpa']:.2f}"
                )

            if r["sales"] - old.get("sales", 0) > EPS:
                alerts.append(
                    "🟦 *SALE ALERT*\n"
                    f"{header}\n"
                    f"Sales: {int(old['sales'])} → {int(r['sales'])}\n"
                    f"Revenue: ${r['revenue']:.2f}"
                )
        else:
            if r["conversions"] > 0:
                alerts.append(
                    "🟩 *LEAD ALERT*\n"
                    f"{header}\n"
                    f"Conv: 0 → {int(r['conversions'])} • CPA: ${r['cpa']:.2f}"
                )

        new_map[k] = r

    if alerts:
        tg_send("\n\n".join(alerts))

    save_state({"date": today, "rows": new_map})


if __name__ == "__main__":
    main()
