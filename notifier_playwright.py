# notifier_playwright.py — stable alerts without duplicates (Keitaro Favourite)

import os, json, time
from typing import Dict, List, Tuple
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
        headers={
            "Authorization": f"Bearer {GIST_TOKEN}",
            "Accept": "application/vnd.github+json"
        },
        timeout=30
    )
    if r.status_code == 200:
        files = r.json().get("files", {})
        if GIST_FILENAME in files:
            try:
                return json.loads(files[GIST_FILENAME]["content"])
            except Exception:
                pass
    return {"date": today_str(), "rows": {}}


def save_state(state: Dict):
    requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={
            "Authorization": f"Bearer {GIST_TOKEN}",
            "Accept": "application/vnd.github+json"
        },
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


# ================= PARSER =================
def parse_report_from_json(payload: dict) -> List[Dict]:
    rows = []
    for r in payload.get("rows", []):
        dims = r.get("dimensions", {}) or {}

        def g(k):
            return r.get(k) or dims.get(k) or ""

        rows.append({
            "k": f"{g('campaign')}|{g('country')}|{g('external_id')}|{g('creative_id')}",
            "campaign": str(g("campaign")),
            "country": str(g("country")),
            "external_id": str(g("external_id")),
            "creative_id": str(g("creative_id")),
            "conversions": as_float(r.get("conversions")),
            "sales": as_float(r.get("sales")),
        })
    return rows


def aggregate_rows_max(rows: List[Dict]) -> List[Dict]:
    acc = {}
    for r in rows:
        k = r["k"]
        if k not in acc:
            acc[k] = dict(r)
        else:
            acc[k]["conversions"] = max(acc[k]["conversions"], r["conversions"])
            acc[k]["sales"] = max(acc[k]["sales"], r["sales"])
    return list(acc.values())


# ================= FETCH =================
def fetch_rows() -> List[Dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 Chrome/124"
        )
        page = ctx.new_page()

        # LOGIN
        page.goto("https://digitaltraff.click/admin/", wait_until="domcontentloaded")
        page.fill("input[name='login']", LOGIN_USER)
        page.fill("input[name='password']", LOGIN_PASS)
        page.click("button[type='submit']")

        try:
            page.wait_for_selector("app-login", state="detached", timeout=15000)
        except PWTimeout:
            pass

        captured = []
        best_score = -1

        def on_response(resp):
            nonlocal captured, best_score
            try:
                data = resp.json()
            except:
                return

            if not isinstance(data, dict):
                return
            if "rows" not in data or not isinstance(data["rows"], list):
                return

            rows = parse_report_from_json(data)
            if not rows:
                return

            score = len(rows)
            if score > best_score:
                captured = rows
                best_score = score

        ctx.on("response", on_response)

        page.goto(PAGE_URL, wait_until="networkidle")
        time.sleep(2)

        browser.close()
        return aggregate_rows_max(captured)


# ================= MAIN =================
def main():
    state = load_state()
    today = today_str()

    rows = fetch_rows()
    if not rows:
        tg_send("⚠️ Keitaro: no data")
        return

    if state["date"] != today:
        save_state({"date": today, "rows": {r["k"]: r for r in rows}})
        return

    alerts = []
    new_map = {}

    for r in rows:
        old = state["rows"].get(r["k"], {"conversions": 0, "sales": 0})

        if r["conversions"] > old["conversions"]:
            alerts.append(
                "🟩 *CONVERSION ALERT*\n"
                f"Campaign: {r['campaign']}\n"
                f"Country: {r['country']}\n"
                f"Creative: {r['creative_id']}\n"
                f"Conversions: {int(old['conversions'])} → {int(r['conversions'])}"
            )

        if r["sales"] > old["sales"]:
            alerts.append(
                "🟦 *SALE ALERT*\n"
                f"Campaign: {r['campaign']}\n"
                f"Country: {r['country']}\n"
                f"Creative: {r['creative_id']}\n"
                f"Sales: {int(old['sales'])} → {int(r['sales'])}"
            )

        new_map[r["k"]] = r

    if alerts:
        tg_send("\n\n".join(alerts))

    save_state({"date": today, "rows": new_map})


if __name__ == "__main__":
    main()
