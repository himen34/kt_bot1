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

BASE_URL = (os.getenv("BASE_URL", "https://digitaltraff.click") or "https://digitaltraff.click").rstrip("/")

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID_1 = os.getenv("TELEGRAM_CHAT_ID_1") or os.getenv("TELEGRAM_CHAT_ID")
TG_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2")
CHAT_IDS = [cid for cid in (TG_CHAT_ID_1, TG_CHAT_ID_2) if cid]

GIST_ID    = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = os.getenv("GIST_FILENAME", "keitaro_favourite_state.json")

KYIV_TZ   = ZoneInfo(os.getenv("KYIV_TZ", "Europe/Kyiv"))
EPS = 0.0001

# Favourite report id (в твоём URL: /reports/favourite/1/ ...)
FAV_ID = os.getenv("FAV_ID", "1")

# timezone отчёта (как в твоём URL: Europe/Warsaw)
REPORT_TZ = os.getenv("REPORT_TZ", "Europe/Warsaw")


# ========= utils =========
def now_kyiv() -> datetime:
    return datetime.now(KYIV_TZ)

def kyiv_today_str() -> str:
    return now_kyiv().strftime("%Y-%m-%d")

def fmt_money(x: float) -> str:
    return f"${x:,.2f}"

def as_float(v):
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


# ========= parsing helpers =========
def parse_report_from_json(payload: dict) -> List[Dict]:
    """
    Favourite Today CPA: grouping = campaign,country,external_id,creative_id
    metrics = conversions, sales, sale_revenue/deposit_revenue, etc.
    """
    rows = []
    for r in payload.get("rows", []):
        dims = r.get("dimensions", {}) if isinstance(r.get("dimensions"), dict) else {}

        def g(k):
            return r.get(k) or dims.get(k) or ""

        campaign = str(g("campaign")).strip()
        country  = str(g("country")).strip()
        external = str(g("external_id")).strip()
        creative = str(g("creative_id")).strip()

        if not (campaign or country or external or creative):
            continue

        rows.append({
            "k": f"{campaign}|{country}|{external}|{creative}",
            "campaign": campaign,
            "country": country,
            "external_id": external,
            "creative_id": creative,
            "conversions": as_float(r.get("conversions")),
            "sales": as_float(r.get("sales")),
            "revenue": as_float(r.get("sale_revenue") or r.get("deposit_revenue") or r.get("revenue")),
        })
    return rows


def aggregate_rows_max(rows: List[Dict]) -> List[Dict]:
    """Склейка дубликатов за запуск: берём максимум по conversions/sales/revenue на один ключ."""
    acc: Dict[str, Dict] = {}
    for r in rows:
        k = r["k"]
        if k not in acc:
            acc[k] = dict(r)
        else:
            a = acc[k]
            a["conversions"] = max(a.get("conversions", 0.0), r.get("conversions", 0.0))
            a["sales"]       = max(a.get("sales", 0.0),       r.get("sales", 0.0))
            a["revenue"]     = max(a.get("revenue", 0.0),     r.get("revenue", 0.0))
    return list(acc.values())


# ========= fetch (как в старом коде, но стабильнее) =========
def fetch_rows() -> List[Dict]:
    """
    1) логинимся
    2) открываем PAGE_URL (чтобы сессия/куки точно активны)
    3) ДЕЛАЕМ прямой API запрос favourite отчёта
       и безопасно парсим ответ (если вернулся HTML — игнорируем)
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        )
        page = ctx.new_page()

        # login
        page.goto(f"{BASE_URL}/admin/", wait_until="domcontentloaded")
        try:
            # поддержка обеих форм
            if page.locator("input[placeholder='Username']").count() > 0:
                page.get_by_placeholder("Username").fill(LOGIN_USER)
                page.get_by_placeholder("Password").fill(LOGIN_PASS)
                page.get_by_role("button", name=re.compile("sign in|увійти|войти", re.I)).click()
            else:
                page.fill("input[name='login'], input[type='text']", LOGIN_USER)
                page.fill("input[name='password'], input[type='password']", LOGIN_PASS)
                page.get_by_role("button", name=re.compile("sign in|увійти|войти", re.I)).click()
        except Exception:
            pass

        try:
            page.wait_for_selector("app-login", state="detached", timeout=15000)
        except PWTimeout:
            pass

        # открываем отчёт (не обязательно, но стабилизирует сессию)
        page.goto(PAGE_URL, wait_until="domcontentloaded")
        time.sleep(1.5)

        # ✅ прямой запрос к favourite report (без ловли XHR)
        raw = page.evaluate(
            """async ({favId, tz}) => {
                const res = await fetch(`/admin/api/reports/favourite/${favId}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ range: { interval: 'today', timezone: tz } })
                });
                return await res.text();
            }""",
            {"favId": FAV_ID, "tz": REPORT_TZ}
        )

        browser.close()

    # HTML вместо JSON -> пусто
    raw = (raw or "").strip()
    if not raw.startswith("{"):
        return []

    try:
        data = json.loads(raw)
    except Exception:
        return []

    rows = parse_report_from_json(data)
    return aggregate_rows_max(rows)


# ========= main logic =========
def main():
    state = load_state()
    prev_date: str = state.get("date", kyiv_today_str())
    prev_rows: Dict[str, Dict] = state.get("rows", {})
    today = kyiv_today_str()

    rows = fetch_rows()

    # ✅ FIX: не спамим "no data", если раньше данные уже были
    if not rows:
        if prev_rows:
            return
        tg_send("⚠️ Keitaro: no data")
        return

    # daily reset
    if prev_date != today:
        baseline = {r["k"]: r for r in rows}
        save_state({"date": today, "rows": baseline})
        return

    new_map: Dict[str, Dict] = {}
    conv_msgs: List[str] = []
    sale_msgs: List[str] = []

    for r in rows:
        k = r["k"]
        old = prev_rows.get(k)

        header = (
            f"Campaign: {r['campaign']}\n"
            f"Country: {r['country']}\n"
            f"External: {r['external_id']}\n"
            f"Creative: {r['creative_id']}"
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

            # SALES
            if r["sales"] - old_sales > EPS:
                delta_rev = r["revenue"] - old_rev
                sale_msgs.append(
                    "🟦 *SALE ALERT*\n"
                    f"{header}\n"
                    f"Sales: {int(old_sales)} → {int(r['sales'])}\n"
                    f"Revenue Δ: {fmt_money(delta_rev)}"
                )
        else:
            # new key
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
