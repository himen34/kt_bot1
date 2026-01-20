import os, json, time, re
from typing import Dict, List, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ================== CONFIG ==================
DEBUG = False

# Keitaro base domain (ВАЖНО: меняется под разные Keitaro)
BASE_URL = os.getenv("BASE_URL", "https://digitaltraff.click").rstrip("/")

LOGIN_USER = os.environ["LOGIN_USER"]
LOGIN_PASS = os.environ["LOGIN_PASS"]
PAGE_URL   = os.environ["PAGE_URL"]

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID_1 = os.getenv("TELEGRAM_CHAT_ID_1") or os.getenv("TELEGRAM_CHAT_ID")
TG_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2")
CHAT_IDS = [cid for cid in (TG_CHAT_ID_1, TG_CHAT_ID_2) if cid]

GIST_ID    = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = os.getenv("GIST_FILENAME", "keitaro_today_cpa_state.json")

TZ = ZoneInfo(os.getenv("KYIV_TZ", "Europe/Kyiv"))
EPS = 0.009

# хочешь “как раньше” со spend? включи True
ENABLE_SPEND = (os.getenv("ENABLE_SPEND", "false").lower() == "true")
SPEND_DIR = (os.getenv("SPEND_DIRECTION", "both") or "both").lower()  # up|down|both


# ================== TIME ==================
def today_key() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


# ================== HELPERS ==================
def as_float(v):
    try:
        return float(v or 0)
    except:
        return 0.0

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


# ================== GIST STATE ==================
def load_state() -> Dict:
    r = requests.get(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={
            "Authorization": f"Bearer {GIST_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    if r.status_code == 200:
        files = r.json().get("files", {})
        if GIST_FILENAME in files and "content" in files[GIST_FILENAME]:
            try:
                return json.loads(files[GIST_FILENAME]["content"])
            except:
                pass
    return {"date": today_key(), "rows": {}}

def save_state(state: Dict):
    r = requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={
            "Authorization": f"Bearer {GIST_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        json={
            "files": {
                GIST_FILENAME: {
                    "content": json.dumps(state, ensure_ascii=False, indent=2)
                }
            }
        },
        timeout=30,
    )
    r.raise_for_status()


# ================== TELEGRAM ==================
def tg_send(text: str):
    if not CHAT_IDS:
        return
    for cid in CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={
                    "chat_id": cid,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )
        except:
            pass


# ================== PARSE TODAY CPA REPORT ==================
def parse_today_cpa_json(payload: dict) -> List[Dict]:
    """
    Ожидаем Keitaro report JSON: payload["rows"] где у каждой строки есть:
      - dimensions: campaign, country, external_id, creative_id (или похожие)
      - metrics: conversions, sales, revenue, cost ...
    """
    out: List[Dict] = []
    for r in payload.get("rows", []):
        dims = r.get("dimensions", {}) or {}

        def g(*keys):
            for k in keys:
                v = r.get(k)
                if v not in (None, "", 0):
                    return v
                v = dims.get(k)
                if v not in (None, "", 0):
                    return v
            return ""

        campaign = str(g("campaign", "campaign_name", "campaign_id")).strip()
        country  = str(g("country", "country_name", "geo", "country_code")).strip()
        external = str(g("external_id", "external", "externalId")).strip()
        creative = str(g("creative_id", "creative", "creativeId")).strip()

        # ключ как в таблице: Кампания + Країна + External ID + Creative ID
        k = f"{campaign}|{country}|{external}|{creative}"

        out.append({
            "k": k,
            "campaign": campaign,
            "country": country,
            "external_id": external,
            "creative_id": creative,

            # метрики
            "clicks": as_float(r.get("clicks")),
            "uniq": as_float(r.get("campaign_unique_clicks")),
            "conversions": as_float(r.get("conversions")),
            "sales": as_float(r.get("sales")),
            "revenue": as_float(r.get("revenue") or r.get("deposit_revenue")),
            "cost": as_float(r.get("cost")),
            "cpa": as_float(r.get("cpa")),
        })
    return out


def aggregate_rows_max(rows: List[Dict]) -> List[Dict]:
    """Склейка дубликатов за запуск: берём максимум по основным метрикам на один ключ."""
    acc: Dict[str, Dict] = {}
    for r in rows:
        k = r["k"]
        if k not in acc:
            acc[k] = dict(r)
        else:
            a = acc[k]
            for m in ("clicks", "uniq", "conversions", "sales", "revenue", "cost", "cpa"):
                a[m] = max(as_float(a.get(m)), as_float(r.get(m)))
    return list(acc.values())


# ================== FETCH ==================
def fetch_rows() -> List[Dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        # ===== LOGIN =====
        page.goto(f"{BASE_URL}/admin/", wait_until="domcontentloaded")

        # универсально: placeholder или обычные input
        try:
            # вариант 1 (часто)
            if page.locator("input[placeholder='Username']").count() > 0:
                page.get_by_placeholder("Username").fill(LOGIN_USER)
                page.get_by_placeholder("Password").fill(LOGIN_PASS)
                page.get_by_role("button", name=re.compile("sign in|войти|увійти", re.I)).click()
            else:
                # вариант 2 (как у тебя в старом коде)
                page.fill("input[name='login'], input[type='text']", LOGIN_USER)
                page.fill("input[name='password'], input[type='password']", LOGIN_PASS)
                page.get_by_role("button", name=re.compile("sign in|войти|увійти", re.I)).click()
        except Exception:
            pass

        try:
            page.wait_for_selector("app-login", state="detached", timeout=15000)
        except PWTimeout:
            pass

        # ===== CAPTURE XHR =====
        captured: List[Dict] = []
        best_len = 0

        def on_response(resp):
            nonlocal captured, best_len
            url = (resp.url or "").lower()

            if DEBUG:
                print("XHR:", url)

            # Keitaro reports
            if "/admin/api/reports" not in url:
                return

            try:
                data = resp.json()
            except Exception:
                return

            rows = parse_today_cpa_json(data)
            if not rows:
                return

            if DEBUG:
                print("  rows:", len(rows))

            # берём самый полный пакет
            if len(rows) > best_len:
                captured = rows
                best_len = len(rows)

        ctx.on("response", on_response)

        # ===== OPEN REPORT =====
        page.goto(PAGE_URL, wait_until="domcontentloaded")
        time.sleep(3.0)

        browser.close()
        return aggregate_rows_max(captured)


# ================== MAIN ==================
def main():
    state = load_state()
    prev_date = state.get("date", today_key())
    today = today_key()

    rows = fetch_rows()
    if not rows:
        tg_send("⚠️ Keitaro: no data")
        return

    # daily reset
    if prev_date != today:
        save_state({"date": today, "rows": {r["k"]: r for r in rows}})
        return

    prev_rows: Dict[str, Dict] = state.get("rows", {})
    new_map: Dict[str, Dict] = {}

    spend_msgs: List[str] = []
    lead_msgs: List[str] = []
    sale_msgs: List[str] = []

    for r in rows:
        k = r["k"]
        old = prev_rows.get(k)

        header = (
            f"{r['campaign']} | {r['country']} | {r['external_id']} | {r['creative_id']}"
        )

        if old:
            # SPEND (optional)
            if ENABLE_SPEND:
                delta_cost = r["cost"] - as_float(old.get("cost"))
                if direction_ok(delta_cost):
                    p = pct(delta_cost, as_float(old.get("cost")))
                    arrow = "🔺" if delta_cost > 0 else "🔻"
                    spend_msgs.append(
                        "🧊 *SPEND ALERT*\n"
                        f"{header}\n"
                        f"Cost: {fmt_money(as_float(old.get('cost')))} → {fmt_money(r['cost'])} "
                        f"(Δ {fmt_money(delta_cost)}, ~{p:.0f}%) {arrow}"
                    )

            # LEADS (conversions)
            if r["conversions"] - as_float(old.get("conversions")) > EPS:
                lead_msgs.append(
                    "🟩 *LEAD ALERT*\n"
                    f"{header}\n"
                    f"Conv: {int(as_float(old.get('conversions')))} → {int(r['conversions'])}  • CPA: {fmt_money(r['cpa'])}"
                )

            # SALES
            if r["sales"] - as_float(old.get("sales")) > EPS:
                delta_rev = r["revenue"] - as_float(old.get("revenue"))
                sale_msgs.append(
                    "🟦 *SALE ALERT*\n"
                    f"{header}\n"
                    f"Sales: {int(as_float(old.get('sales')))} → {int(r['sales'])}\n"
                    f"Revenue Δ: {fmt_money(delta_rev)}"
                )
        else:
            # new row key
            if ENABLE_SPEND and r["cost"] > EPS:
                spend_msgs.append(
                    "🧊 *SPEND ALERT*\n"
                    f"{header}\n"
                    f"Cost: {fmt_money(0)} → {fmt_money(r['cost'])} 🔺"
                )
            if r["conversions"] > EPS:
                lead_msgs.append(
                    "🟩 *LEAD ALERT*\n"
                    f"{header}\n"
                    f"Conv: 0 → {int(r['conversions'])}  • CPA: {fmt_money(r['cpa'])}"
                )
            if r["sales"] > EPS:
                sale_msgs.append(
                    "🟦 *SALE ALERT*\n"
                    f"{header}\n"
                    f"Sales: 0 → {int(r['sales'])}\n"
                    f"Revenue: {fmt_money(r['revenue'])}"
                )

        new_map[k] = r

    blocks = spend_msgs + lead_msgs + sale_msgs
    if blocks:
        tg_send("\n\n".join(blocks))

    save_state({"date": today, "rows": new_map})


if __name__ == "__main__":
    main()
