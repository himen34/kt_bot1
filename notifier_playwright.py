import os, json, time, re
from typing import Dict, List
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ================= ENV =================
LOGIN_USER = os.environ["LOGIN_USER"]
LOGIN_PASS = os.environ["LOGIN_PASS"]
PAGE_URL   = os.environ["PAGE_URL"]

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

GIST_ID    = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = os.getenv("GIST_FILENAME", "keitaro_favourite_state.json")

TZ = ZoneInfo("Europe/Kyiv")
EPS = 0.0001


# ================= TIME =================
def today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


# ================= TELEGRAM =================
def tg_send(text: str):
    try:
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
    except Exception:
        pass


# ================= STATE (GIST) =================
def load_state() -> Dict:
    try:
        r = requests.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={"Authorization": f"Bearer {GIST_TOKEN}"},
            timeout=20
        )
        if r.status_code == 200:
            files = r.json().get("files", {})
            if GIST_FILENAME in files:
                return json.loads(files[GIST_FILENAME]["content"])
    except Exception:
        pass

    return {"date": today_str(), "rows": {}}


def save_state(state: Dict):
    requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"Bearer {GIST_TOKEN}"},
        json={
            "files": {
                GIST_FILENAME: {
                    "content": json.dumps(state, ensure_ascii=False, indent=2)
                }
            }
        },
        timeout=20
    )


# ================= HELPERS =================
def as_float(v) -> float:
    try:
        return float(v)
    except:
        return 0.0


# ================= PARSE FAVOURITE JSON =================
def parse_favourite_json(payload: dict) -> List[Dict]:
    rows = []
    for r in payload.get("rows", []):
        d = r.get("dimensions", {}) or {}

        row = {
            "k": f"{d.get('campaign')}|{d.get('country')}|{d.get('creative_id')}",
            "campaign": d.get("campaign"),
            "country": d.get("country"),
            "creative_id": d.get("creative_id"),
            "conversions": as_float(r.get("conversions")),
            "sales": as_float(r.get("sales")),
            "revenue": as_float(r.get("sale_revenue")),
        }
        rows.append(row)
    return rows


# ================= FETCH =================
def fetch_rows() -> List[Dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        # LOGIN
        page.goto("https://digitaltraff.click/admin/", wait_until="domcontentloaded")
        page.get_by_placeholder("Username").fill(LOGIN_USER)
        page.get_by_placeholder("Password").fill(LOGIN_PASS)
        page.get_by_role("button", name=re.compile("sign in", re.I)).click()

        try:
            page.wait_for_selector("app-login", state="detached", timeout=15000)
        except PWTimeout:
            pass

        captured: List[Dict] = []

        def on_response(resp):
            nonlocal captured
            url = resp.url.lower()
            if "/admin/api/reports/favourite" not in url:
                return
            try:
                data = resp.json()
            except Exception:
                return
            rows = parse_favourite_json(data)
            if rows:
                captured = rows

        ctx.on("response", on_response)

        page.goto(PAGE_URL, wait_until="domcontentloaded")
        time.sleep(2.5)

        browser.close()
        return captured


# ================= MAIN =================
def main():
    state = load_state()
    today = today_str()

    rows = fetch_rows()
    if not rows:
        tg_send("⚠️ Keitaro: no data")
        return

    # reset every day
    if state["date"] != today:
        save_state({
            "date": today,
            "rows": {r["k"]: r for r in rows}
        })
        return

    prev = state["rows"]
    new_state = {}
    alerts = []

    for r in rows:
        k = r["k"]
        old = prev.get(k)

        header = (
            f"Campaign: {r['campaign']}\n"
            f"Country: {r['country']}\n"
            f"Creative: {r['creative_id']}"
        )

        if old:
            # CONVERSIONS
            if r["conversions"] > old.get("conversions", 0) + EPS:
                alerts.append(
                    "🟩 *CONVERSION ALERT*\n"
                    f"{header}\n"
                    f"Conversions: {int(old['conversions'])} → {int(r['conversions'])}"
                )

            # SALES
            if r["sales"] > old.get("sales", 0) + EPS:
                alerts.append(
                    "🟦 *SALE ALERT*\n"
                    f"{header}\n"
                    f"Sales: {int(old['sales'])} → {int(r['sales'])}\n"
                    f"Revenue: ${r['revenue']:.2f}"
                )

        new_state[k] = r

    if alerts:
        tg_send("\n\n".join(alerts))

    save_state({
        "date": today,
        "rows": new_state
    })


if __name__ == "__main__":
    main()
