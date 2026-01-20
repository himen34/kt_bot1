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
TG_CHAT_ID_1 = os.getenv("TELEGRAM_CHAT_ID_1") or os.getenv("TELEGRAM_CHAT_ID")
TG_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2")
CHAT_IDS = [cid for cid in (TG_CHAT_ID_1, TG_CHAT_ID_2) if cid]

GIST_ID    = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = os.getenv("GIST_FILENAME", "keitaro_campaigns_state.json")

SPEND_DIR = (os.getenv("SPEND_DIRECTION", "both") or "both").lower()
KYIV_TZ   = ZoneInfo(os.getenv("KYIV_TZ", "Europe/Kyiv"))
EPS = 0.009

# ================= utils =================
def now_kyiv() -> datetime:
    return datetime.now(KYIV_TZ)

def kyiv_today_str() -> str:
    return now_kyiv().strftime("%Y-%m-%d")

def fmt_money(x: float) -> str:
    return f"${x:,.2f}"

def pct(delta: float, base: float) -> float:
    if abs(base) < EPS:
        return 100.0 if abs(delta) >= EPS else 0.0
    return abs(delta / base) * 100.0

def direction_ok(delta: float) -> bool:
    if SPEND_DIR == "up":
        return delta > EPS
    if SPEND_DIR == "down":
        return delta < -EPS
    return abs(delta) > EPS

def as_float(v):
    try:
        return float(v or 0)
    except:
        return 0.0

# ================= Gist state =================
def load_state() -> Dict:
    url = f"https://api.github.com/gists/{GIST_ID}"
    r = requests.get(
        url,
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
            except:
                pass
    return {"date": kyiv_today_str(), "rows": {}}

def save_state(state: Dict):
    url = f"https://api.github.com/gists/{GIST_ID}"
    files = {GIST_FILENAME: {"content": json.dumps(state, ensure_ascii=False, indent=2)}}
    r = requests.patch(
        url,
        headers={
            "Authorization": f"Bearer {GIST_TOKEN}",
            "Accept": "application/vnd.github+json"
        },
        json={"files": files},
        timeout=30
    )
    r.raise_for_status()

# ================= Telegram =================
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

# ================= Keitaro JSON parsing =================
def parse_campaigns_report(payload: dict) -> List[Dict]:
    rows = []
    for r in payload.get("rows", []):
        dims = r.get("dimensions", {}) or {}

        country  = (dims.get("country") or "").strip()
        creative = (dims.get("creative_id") or "").strip()
        sub2     = (dims.get("sub_id_2") or "").strip()

        if not (country or creative or sub2):
            continue

        rows.append({
            "k": f"{country}|{creative}|{sub2}",
            "country": country,
            "creative": creative,
            "sub2": sub2,
            "cost":    as_float(r.get("cost")),
            "leads":   as_float(r.get("conversions")),
            "sales":   as_float(r.get("sales")),
            "revenue": as_float(r.get("revenue")),
        })
    return rows

# ================= Fetch rows (CORRECT) =================
def fetch_rows() -> List[Dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 Chrome/124 Safari/537.36"
        )
        page = ctx.new_page()

        # login
        page.goto("https://digitaltraff.click/admin/", wait_until="domcontentloaded")
        page.get_by_placeholder("Username").fill(LOGIN_USER)
        page.get_by_placeholder("Password").fill(LOGIN_PASS)
        page.get_by_role("button", name="Sign in").click()

        page.wait_for_url(re.compile(r".*/admin/#!/.*"), timeout=20000)

        # 🔥 ловимо XHR ДО переходу
        with page.expect_response(
            lambda r: "/admin/api/reports/campaigns" in r.url and r.status == 200,
            timeout=30000
        ) as resp_info:
            page.goto(PAGE_URL, wait_until="domcontentloaded")

        resp = resp_info.value
        data = resp.json()

        browser.close()
        return parse_campaigns_report(data)

# ================= monotonic =================
def clamp_monotonic(new_v: float, old_v: float) -> float:
    if old_v is None:
        return new_v
    return new_v if new_v >= (old_v - 1e-6) else old_v

# ================= MAIN =================
def main():
    state = load_state()
    prev_date = state.get("date", kyiv_today_str())
    prev_rows = state.get("rows", {})
    today = kyiv_today_str()

    rows = fetch_rows()
    if not rows:
        tg_send("⚠️ Keitaro: no data fetched")
        return

    if prev_date != today:
        save_state({"date": today, "rows": {r["k"]: r for r in rows}})
        return

    new_map = {}
    spend_msgs, lead_msgs, sale_msgs = [], [], []

    for r in rows:
        k = r["k"]
        old = prev_rows.get(k)

        if old:
            r["cost"]    = clamp_monotonic(r["cost"], old["cost"])
            r["leads"]   = clamp_monotonic(r["leads"], old["leads"])
            r["sales"]   = clamp_monotonic(r["sales"], old["sales"])
            r["revenue"] = clamp_monotonic(r["revenue"], old["revenue"])

            header = f"{r['country']} | {r['creative']} | {r['sub2']}"

            delta_cost = r["cost"] - old["cost"]
            if direction_ok(delta_cost):
                p = pct(delta_cost, old["cost"])
                arrow = "🔺" if delta_cost > 0 else "🔻"
                spend_msgs.append(
                    "🧊 *SPEND ALERT*\n"
                    f"{header}\n"
                    f"Cost: {fmt_money(old['cost'])} → {fmt_money(r['cost'])} "
                    f"(Δ {fmt_money(delta_cost)}, ~{p:.0f}%) {arrow}"
                )

            if r["leads"] - old["leads"] > EPS:
                lead_msgs.append(
                    "🟩 *LEAD ALERT*\n"
                    f"{header}\n"
                    f"Leads: {int(old['leads'])} → {int(r['leads'])}"
                )

            if r["sales"] - old["sales"] > EPS:
                sale_msgs.append(
                    "🟦 *SALE ALERT*\n"
                    f"{header}\n"
                    f"Sales: {int(old['sales'])} → {int(r['sales'])}\n"
                    f"Revenue: {fmt_money(r['revenue'] - old['revenue'])}"
                )
        else:
            if r["cost"] > EPS:
                spend_msgs.append(
                    "🧊 *SPEND ALERT*\n"
                    f"{r['country']} | {r['creative']} | {r['sub2']}\n"
                    f"Cost: {fmt_money(0)} → {fmt_money(r['cost'])} 🔺"
                )

        new_map[k] = r

    blocks = spend_msgs + lead_msgs + sale_msgs
    if blocks:
        tg_send("\n\n".join(blocks))

    save_state({"date": today, "rows": new_map})

if __name__ == "__main__":
    main()
