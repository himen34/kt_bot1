# notifier_playwright.py — стабильные алерты без дублей (Keitaro TEAM)

import os, json, time, re
from typing import Dict, List, Tuple
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
TG_CHAT_ID_1 = os.getenv("TELEGRAM_CHAT_ID_1") or os.getenv("TELEGRAM_CHAT_ID")
TG_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2")
CHAT_IDS = [cid for cid in (TG_CHAT_ID_1, TG_CHAT_ID_2) if cid]

GIST_ID    = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = os.getenv("GIST_FILENAME", "keitaro_state.json")

SPEND_DIR = (os.getenv("SPEND_DIRECTION", "both") or "both").lower()
KYIV_TZ   = ZoneInfo(os.getenv("KYIV_TZ", "Europe/Kyiv"))

EPS = 0.009

# ========= TIME =========
def now_kyiv() -> datetime:
    return datetime.now(KYIV_TZ)

def kyiv_today_str() -> str:
    return now_kyiv().strftime("%Y-%m-%d")

# ========= FORMAT =========
def fmt_money(x: float) -> str:
    return f"${x:,.2f}"

def pct(delta: float, base: float) -> float:
    if abs(base) < EPS:
        return 100.0
    return abs(delta / base) * 100.0

def direction_ok(delta: float) -> bool:
    if SPEND_DIR == "up":
        return delta > EPS
    if SPEND_DIR == "down":
        return delta < -EPS
    return abs(delta) > EPS

# ========= STATE (GIST) =========
def load_state() -> Dict:
    url = f"https://api.github.com/gists/{GIST_ID}"
    r = requests.get(url, headers={
        "Authorization": f"Bearer {GIST_TOKEN}",
        "Accept": "application/vnd.github+json"
    }, timeout=30)

    if r.status_code == 200:
        files = r.json().get("files", {})
        if GIST_FILENAME in files:
            try:
                return json.loads(files[GIST_FILENAME]["content"])
            except:
                pass

    return {"date": kyiv_today_str(), "rows": {}}

def save_state(state: Dict):
    url = f"https://api.github.com/gists/{GIST_ID}"
    requests.patch(
        url,
        headers={
            "Authorization": f"Bearer {GIST_TOKEN}",
            "Accept": "application/vnd.github+json"
        },
        json={"files": {
            GIST_FILENAME: {
                "content": json.dumps(state, ensure_ascii=False, indent=2)
            }
        }},
        timeout=30
    ).raise_for_status()

# ========= TELEGRAM =========
def tg_send(text: str):
    for cid in CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={
                    "chat_id": cid,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True
                },
                timeout=20
            )
        except:
            pass

# ========= HELPERS =========
def as_float(v):
    try:
        return float(v or 0)
    except:
        return 0.0

# ========= PARSE XHR =========
def parse_report_from_json(payload: dict) -> List[Dict]:
    rows = []
    for r in payload.get("rows", []):
        dims = r.get("dimensions", {}) or {}
        g = lambda k: r.get(k) or dims.get(k) or ""

        rows.append({
            "k": f"{g('campaign_id')}|{g('creative_id')}|{g('sub_id_2')}|{g('country')}",
            "campaign": str(g("campaign_id")),
            "creative": str(g("creative_id")),
            "sub2": str(g("sub_id_2")),
            "geo": str(g("country")),
            "cost": as_float(r.get("cost")),
            "leads": as_float(r.get("conversions")),
            "sales": as_float(r.get("sales")),
        })
    return rows

# ========= FETCH =========
def fetch_rows() -> List[Dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 Chrome/124"
        )
        page = ctx.new_page()

        # ===== LOGIN (FIXED) =====
        page.goto("https://digitaltraff.click/admin/", wait_until="domcontentloaded")

        page.wait_for_selector("app-login", timeout=15000)

        page.get_by_placeholder("Username").fill(LOGIN_USER)
        page.get_by_placeholder("Password").fill(LOGIN_PASS)
        page.get_by_role("button", name="Sign in").click()

        page.wait_for_selector("app-login", state="detached", timeout=20000)

        captured = []
        best_score = -1.0

        def on_response(resp):
            nonlocal captured, best_score
            if "/admin/api/reports/" in resp.url:
                try:
                    data = resp.json()
                except:
                    return
                rows = parse_report_from_json(data)
                if not rows:
                    return
                score = sum(r["cost"] for r in rows)
                if score > best_score:
                    best_score = score
                    captured = rows

        ctx.on("response", on_response)

        page.goto(PAGE_URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=20000)
        time.sleep(1.5)

        browser.close()
        return captured

# ========= MAIN =========
def main():
    state = load_state()
    prev_date = state["date"]
    prev_rows = state["rows"]
    today = kyiv_today_str()

    rows = fetch_rows()
    if not rows:
        tg_send("⚠️ No data fetched")
        return

    if prev_date != today:
        save_state({"date": today, "rows": {r["k"]: r for r in rows}})
        tg_send("🔄 New day baseline saved")
        return

    new_map = {}
    msgs = []

    for r in rows:
        k = r["k"]
        old = prev_rows.get(k)

        if old:
            # SPEND
            delta = r["cost"] - old["cost"]
            if direction_ok(delta):
                msgs.append(
                    "🧊 *SPEND ALERT*\n"
                    f"Campaign: {r['campaign']}\n"
                    f"Creative: {r['creative']}  Sub2: {r['sub2']}  Geo: {r['geo']}\n"
                    f"Cost: {fmt_money(old['cost'])} → {fmt_money(r['cost'])} "
                    f"(Δ {fmt_money(delta)}, {pct(delta, old['cost']):.0f}%)"
                )

            # LEADS
            if r["leads"] > old["leads"]:
                msgs.append(
                    "🟩 *LEAD ALERT*\n"
                    f"Campaign: {r['campaign']}\n"
                    f"Creative: {r['creative']}  Sub2: {r['sub2']}  Geo: {r['geo']}\n"
                    f"Leads: {int(old['leads'])} → {int(r['leads'])}"
                )

            # SALES
            if r["sales"] > old["sales"]:
                msgs.append(
                    "🟦 *SALE ALERT*\n"
                    f"Campaign: {r['campaign']}\n"
                    f"Creative: {r['creative']}  Sub2: {r['sub2']}  Geo: {r['geo']}\n"
                    f"Sales: {int(old['sales'])} → {int(r['sales'])}"
                )

        new_map[k] = r

    if msgs:
        tg_send("\n\n".join(msgs))

    save_state({"date": today, "rows": new_map})

# ========= RUN =========
if __name__ == "__main__":
    main()
