import os, json, time, re
from typing import Dict, List, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ========= ENV =========
LOGIN_USER = os.environ["LOGIN_USER"]
LOGIN_PASS = os.environ["LOGIN_PASS"]
PAGE_URL   = os.environ["PAGE_URL"]

# ВАЖНО: чтобы не хардкодить домен, можно задать BASE_URL
# пример: BASE_URL=https://digitaltraff.click
BASE_URL = (os.getenv("BASE_URL", "https://digitaltraff.click") or "https://digitaltraff.click").rstrip("/")

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID_1 = os.getenv("TELEGRAM_CHAT_ID_1") or os.getenv("TELEGRAM_CHAT_ID")
TG_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2")
CHAT_IDS = [cid for cid in (TG_CHAT_ID_1, TG_CHAT_ID_2) if cid]

GIST_ID    = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = os.getenv("GIST_FILENAME", "keitaro_favourite_state.json")

# Таймзона для reset
KYIV_TZ = ZoneInfo(os.getenv("KYIV_TZ", "Europe/Kyiv"))

EPS = 0.0001
DEBUG = (os.getenv("DEBUG", "false").lower() == "true")


# ========= utils =========
def now_kyiv() -> datetime:
    return datetime.now(KYIV_TZ)

def kyiv_today_str() -> str:
    return now_kyiv().strftime("%Y-%m-%d")

def fmt_money(x: float) -> str:
    return f"${x:,.2f}"

def as_float(v) -> float:
    try:
        return float(v or 0)
    except:
        return 0.0


# ========= state (Gist) =========
def load_state() -> Dict:
    url = f"https://api.github.com/gists/{GIST_ID}"
    r = requests.get(url, headers={
        "Authorization": f"Bearer {GIST_TOKEN}",
        "Accept": "application/vnd.github+json"
    }, timeout=30)

    if r.status_code == 200:
        files = r.json().get("files", {})
        if GIST_FILENAME in files and "content" in files[GIST_FILENAME]:
            try:
                return json.loads(files[GIST_FILENAME]["content"])
            except Exception:
                pass

    return {"date": kyiv_today_str(), "rows": {}}

def save_state(state: Dict):
    url = f"https://api.github.com/gists/{GIST_ID}"
    files = {GIST_FILENAME: {"content": json.dumps(state, ensure_ascii=False, indent=2)}}
    r = requests.patch(url, headers={
        "Authorization": f"Bearer {GIST_TOKEN}",
        "Accept": "application/vnd.github+json"
    }, json={"files": files}, timeout=30)
    r.raise_for_status()


# ========= Telegram =========
def tg_send(text: str):
    if not CHAT_IDS:
        return
    for cid in CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": cid, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True},
                timeout=20
            )
        except Exception:
            pass


# ========= parsing (Favourite report JSON) =========
def parse_favourite_report_json(payload: dict) -> List[Dict]:
    """
    Favourite report (grouping: campaign,country,external_id,creative_id)
    metrics обычно: clicks, campaign_unique_clicks, conversions, sales, roi_confirmed, sale_revenue (и т.п.)
    """
    rows = []
    for r in payload.get("rows", []):
        dims = r.get("dimensions", {}) if isinstance(r.get("dimensions"), dict) else {}

        def g(k: str) -> str:
            return str((dims.get(k) or r.get(k) or "")).strip()

        campaign = g("campaign")
        country  = g("country")
        external = g("external_id")
        creative = g("creative_id")

        # если это не наш отчёт — пропускаем пустые строки
        if not (campaign or country or external or creative):
            continue

        rows.append({
            "k": f"{campaign}|{country}|{external}|{creative}",
            "campaign": campaign,
            "country": country,
            "external_id": external,
            "creative_id": creative,

            # метрики
            "conversions": as_float(r.get("conversions")),
            "sales": as_float(r.get("sales")),
            # revenue в favourite чаще = sale_revenue, иногда deposit_revenue тоже есть
            "revenue": as_float(r.get("sale_revenue") or r.get("deposit_revenue") or r.get("revenue")),
            "roi_confirmed": as_float(r.get("roi_confirmed")),
        })
    return rows


# ========= fetch (XHR capture) =========
def fetch_rows() -> List[Dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        )
        page = ctx.new_page()

        # ===== LOGIN =====
        page.goto(f"{BASE_URL}/admin/", wait_until="domcontentloaded")

        # универсально: placeholder или name=login
        try:
            if page.locator("input[placeholder='Username']").count() > 0:
                page.get_by_placeholder("Username").fill(LOGIN_USER)
                page.get_by_placeholder("Password").fill(LOGIN_PASS)
                page.get_by_role("button", name=re.compile("sign in|войти|увійти", re.I)).click()
            else:
                page.fill("input[name='login'], input[type='text']", LOGIN_USER)
                page.fill("input[name='password'], input[type='password']", LOGIN_PASS)
                page.get_by_role("button", name=re.compile("sign in|войти|увійти", re.I)).click()
        except Exception:
            pass

        try:
            page.wait_for_selector("app-login", state="detached", timeout=15000)
        except PWTimeout:
            pass

        # ===== XHR capture (как "в старину") =====
        captured: List[Dict] = []
        best_len = 0

        def on_response(resp):
            nonlocal captured, best_len
            url = (resp.url or "").lower()

            if DEBUG:
                print("XHR:", url)

            # 🔥 ВАЖНО: favourite отчёты не через "/report", а через API:
            # /admin/api/reports/favourite  или /admin/api/favouriteReports (в зависимости от сборки)
            if ("/admin/api/reports/favourite" not in url) and ("/admin/api/favouritereports" not in url):
                return

            try:
                data = resp.json()
            except Exception:
                return

            rows = parse_favourite_report_json(data)
            if not rows:
                return

            # берём самый полный пакет
            if len(rows) > best_len:
                captured = rows
                best_len = len(rows)

        ctx.on("response", on_response)

        # ===== OPEN REPORT =====
        page.goto(PAGE_URL, wait_until="domcontentloaded")

        # Keitaro SPA: НЕ networkidle, просто ждём немного
        time.sleep(3.0)

        browser.close()
        return captured


# ========= main =========
def main():
    state = load_state()
    prev_date: str = state.get("date", kyiv_today_str())
    prev_rows: Dict[str, Dict] = state.get("rows", {})
    today = kyiv_today_str()

    rows = fetch_rows()
    if not rows:
        tg_send("⚠️ Keitaro: no data")
        return

    # reset on new day (Kyiv)
    if prev_date != today:
        save_state({"date": today, "rows": {r["k"]: r for r in rows}})
        return

    new_map: Dict[str, Dict] = {}
    conv_msgs: List[str] = []
    sale_msgs: List[str] = []

    for r in rows:
        k = r["k"]
        old = prev_rows.get(k)

        header = (
            f"{r['campaign']} | {r['country']} | {r['external_id']} | {r['creative_id']}"
        )

        if old:
            old_conv = as_float(old.get("conversions"))
            old_sales = as_float(old.get("sales"))
            old_rev = as_float(old.get("revenue"))

            # CONVERSIONS
            if r["conversions"] - old_conv > EPS:
                conv_msgs.append(
                    "🟩 *CONVERSION ALERT*\n"
                    f"{header}\n"
                    f"Conversions: {int(old_conv)} → {int(r['conversions'])}"
                )

            # SALES + revenue delta
            if r["sales"] - old_sales > EPS:
                delta_rev = r["revenue"] - old_rev
                sale_msgs.append(
                    "🟦 *SALE ALERT*\n"
                    f"{header}\n"
                    f"Sales: {int(old_sales)} → {int(r['sales'])}\n"
                    f"Revenue Δ: {fmt_money(delta_rev)}"
                )

        else:
            # новый ключ
            if r["conversions"] > EPS:
                conv_msgs.append(
                    "🟩 *CONVERSION ALERT*\n"
                    f"{header}\n"
                    f"Conversions: 0 → {int(r['conversions'])}"
                )

            if r["sales"] > EPS:
                sale_msgs.append(
                    "🟦 *SALE ALERT*\n"
                    f"{header}\n"
                    f"Sales: 0 → {int(r['sales'])}\n"
                    f"Revenue: {fmt_money(r['revenue'])}"
                )

        new_map[k] = r

    blocks = conv_msgs + sale_msgs
    if blocks:
        tg_send("\n\n".join(blocks))

    save_state({"date": today, "rows": new_map})


if __name__ == "__main__":
    main()
