# notifier_playwright.py — стабильные алерты без дублей (Keitaro Favourite: Today CPA)

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

BASE_URL = (os.getenv("BASE_URL", "https://digitaltraff.click") or "https://digitaltraff.click").rstrip("/")

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID_1 = os.getenv("TELEGRAM_CHAT_ID_1") or os.getenv("TELEGRAM_CHAT_ID")
TG_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2")
CHAT_IDS = [cid for cid in (TG_CHAT_ID_1, TG_CHAT_ID_2) if cid]

GIST_ID    = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = os.getenv("GIST_FILENAME", "keitaro_favourite_state.json")

KYIV_TZ   = ZoneInfo(os.getenv("KYIV_TZ", "Europe/Kyiv"))
EPS = 0.009  # чувствительность для алертов


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

def as_int(v):
    try:
        return int(float(v or 0))
    except:
        return 0


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
    Favourite report Today CPA:
    grouping: campaign, country, external_id, creative_id
    metrics: clicks, campaign_unique_clicks, conversions, sales, deposit_revenue, sale_revenue, etc.
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

        # если это не наш отчёт — пропускаем
        if not (campaign or country or external or creative):
            continue

        rows.append({
            "k": f"{campaign}|{country}|{external}|{creative}",
            "campaign": campaign,
            "country": country,
            "external_id": external,
            "creative_id": creative,

            "clicks": as_int(r.get("clicks")),
            "uniq": as_int(r.get("campaign_unique_clicks")),
            "conversions": as_int(r.get("conversions")),
            "sales": as_int(r.get("sales")),

            # revenue может быть в deposit_revenue или sale_revenue
            "deposit_revenue": as_float(r.get("deposit_revenue")),
            "sale_revenue": as_float(r.get("sale_revenue")),
        })
    return rows


def parse_report_from_html(page) -> List[Dict]:
    """
    Fallback #1: если вдруг на странице есть TABLE (иногда бывает в некоторых сборках)
    Ищем заголовки: campaign/country/external/creative/conversions/sales
    """
    rows = []
    try:
        page.wait_for_selector("table", timeout=10000)
    except PWTimeout:
        return rows

    tables = page.query_selector_all("table")
    target = None
    for t in tables:
        head = t.query_selector("thead")
        head_text = (head.inner_text() if head else t.inner_text() or "").lower()
        if ("campaign" in head_text) and ("country" in head_text) and ("creative" in head_text) and ("con" in head_text):
            target = t
            break
    if not target:
        return rows

    headers = [(th.inner_text() or "").strip().lower() for th in target.query_selector_all("thead tr th")]

    def col_idx(variants: List[str]) -> int:
        for i, h in enumerate(headers):
            for v in variants:
                if v in h:
                    return i
        return -1

    idx = {
        "campaign": col_idx(["campaign", "кампан"]),
        "country": col_idx(["country", "країна"]),
        "external": col_idx(["external id", "external"]),
        "creative": col_idx(["creative id", "creative"]),
        "conv": col_idx(["conversions", "конв"]),
        "sales": col_idx(["sales", "продаж"]),
        "dep_rev": col_idx(["deposit_revenue", "дохід (депозит", "deposit revenue"]),
        "sale_rev": col_idx(["sale_revenue", "дохід (підтвер", "sale revenue"]),
        "clicks": col_idx(["clicks", "кліки"]),
        "uniq": col_idx(["unique", "унік", "campaign_unique"]),
    }

    def to_i(s: str) -> int:
        s = (s or "").replace(",", "").strip()
        try:
            return int(float(s))
        except:
            return 0

    def to_f(s: str) -> float:
        s = (s or "").replace("$", "").replace(",", "").strip()
        try:
            return float(s)
        except:
            return 0.0

    for tr in target.query_selector_all("tbody tr"):
        tds = tr.query_selector_all("td")

        def safe(i):
            try:
                return (tds[i].inner_text() or "").strip()
            except:
                return ""

        campaign = safe(idx["campaign"])
        country  = safe(idx["country"])
        external = safe(idx["external"])
        creative = safe(idx["creative"])
        if not (campaign or country or external or creative):
            continue

        rows.append({
            "k": f"{campaign}|{country}|{external}|{creative}",
            "campaign": campaign,
            "country": country,
            "external_id": external,
            "creative_id": creative,

            "clicks": to_i(safe(idx["clicks"])),
            "uniq": to_i(safe(idx["uniq"])),
            "conversions": to_i(safe(idx["conv"])),
            "sales": to_i(safe(idx["sales"])),

            "deposit_revenue": to_f(safe(idx["dep_rev"])),
            "sale_revenue": to_f(safe(idx["sale_rev"])),
        })

    return rows


def parse_report_from_ag_grid(page) -> List[Dict]:
    """
    Fallback #2: AG-GRID (у тебя в UI именно он).
    """
    rows = []
    try:
        # ждём, пока грид появится
        page.wait_for_selector(".ag-center-cols-container .ag-row", timeout=12000)
    except PWTimeout:
        return rows

    try:
        headers = [(h.inner_text() or "").strip().lower() for h in page.locator(".ag-header-cell-text").all()]

        def idx(name_variants: List[str]) -> int:
            for i, h in enumerate(headers):
                for v in name_variants:
                    if v in h:
                        return i
            return -1

        i_campaign = idx(["campaign", "кампан"])
        i_country  = idx(["country", "країна"])
        i_external = idx(["external id", "external"])
        i_creative = idx(["creative id", "creative"])
        i_clicks   = idx(["clicks", "кліки"])
        i_uniq     = idx(["unique", "унік", "campaign_unique"])
        i_conv     = idx(["conversions", "конв"])
        i_sales    = idx(["sales", "продаж"])
        i_dep_rev  = idx(["deposit_revenue", "deposit revenue", "дохід (депозит"])
        i_sale_rev = idx(["sale_revenue", "sale revenue", "дохід (підтвер"])

        def to_i(s: str) -> int:
            s = (s or "").replace(",", "").strip()
            try:
                return int(float(s))
            except:
                return 0

        def to_f(s: str) -> float:
            s = (s or "").replace("$", "").replace(",", "").strip()
            try:
                return float(s)
            except:
                return 0.0

        rws = page.locator(".ag-center-cols-container .ag-row")
        for row in rws.all():
            # ag-grid может рендерить значения как .ag-cell-value / .ag-cell
            cells = [(c.inner_text() or "").strip() for c in row.locator(".ag-cell").all()]

            def safe(i):
                try:
                    return cells[i]
                except:
                    return ""

            campaign = safe(i_campaign)
            country  = safe(i_country)
            external = safe(i_external)
            creative = safe(i_creative)
            if not (campaign or country or external or creative):
                continue

            rows.append({
                "k": f"{campaign}|{country}|{external}|{creative}",
                "campaign": campaign,
                "country": country,
                "external_id": external,
                "creative_id": creative,

                "clicks": to_i(safe(i_clicks)),
                "uniq": to_i(safe(i_uniq)),
                "conversions": to_i(safe(i_conv)),
                "sales": to_i(safe(i_sales)),

                "deposit_revenue": to_f(safe(i_dep_rev)),
                "sale_revenue": to_f(safe(i_sale_rev)),
            })
    except Exception:
        return []

    return rows


# ========= fetch with stabilisation (как у тебя, но под favourite) =========
def aggregate_rows_max(rows: List[Dict]) -> List[Dict]:
    """
    Склейка дубликатов за запуск: берём максимум по conversions/sales/revenue на один ключ.
    """
    acc: Dict[str, Dict] = {}
    for r in rows:
        k = r["k"]
        if k not in acc:
            acc[k] = dict(r)
        else:
            a = acc[k]
            a["clicks"] = max(a.get("clicks", 0), r.get("clicks", 0))
            a["uniq"] = max(a.get("uniq", 0), r.get("uniq", 0))
            a["conversions"] = max(a.get("conversions", 0), r.get("conversions", 0))
            a["sales"] = max(a.get("sales", 0), r.get("sales", 0))
            a["deposit_revenue"] = max(a.get("deposit_revenue", 0.0), r.get("deposit_revenue", 0.0))
            a["sale_revenue"] = max(a.get("sale_revenue", 0.0), r.get("sale_revenue", 0.0))
    return list(acc.values())


def fetch_rows() -> List[Dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        )
        page = ctx.new_page()

        # login (как у тебя)
        page.goto(f"{BASE_URL}/admin/", wait_until="domcontentloaded")
        try:
            page.fill("input[name='login'], input[type='text']", LOGIN_USER)
            page.fill("input[name='password'], input[type='password']", LOGIN_PASS)
            page.get_by_role("button", name=re.compile("sign in|увійти|войти", re.I)).click()
        except Exception:
            pass

        try:
            page.wait_for_selector("app-login", state="detached", timeout=15000)
        except PWTimeout:
            pass

        # ========== XHR capture: КАК В СТАРОМ КОДЕ ==========
        # НИЧЕГО НЕ ФИЛЬТРУЕМ по URL, ловим любой JSON с rows
        captured: List[Dict] = []
        best_score = -1.0

        def on_response(resp):
            nonlocal captured, best_score
            try:
                data = resp.json()
            except Exception:
                return

            if not isinstance(data, dict):
                return
            if "rows" not in data or not isinstance(data.get("rows"), list):
                return

            rows = parse_report_from_json(data)
            if not rows:
                return

            # "лучший пакет" — как раньше, по наполненности
            score = len(rows) + sum(r.get("conversions", 0) + r.get("sales", 0) for r in rows) * 0.01
            if score > best_score:
                captured = rows
                best_score = score

        ctx.on("response", on_response)

        # Открываем PAGE_URL (favorite report)
        # НЕ networkidle — оно в Keitaro часто не наступает
        page.goto(PAGE_URL, wait_until="domcontentloaded")
        time.sleep(2.5)

        # если XHR не словили — пробуем "пинок" (перезагрузка)
        if not captured:
            try:
                page.reload(wait_until="domcontentloaded")
            except Exception:
                pass
            time.sleep(2.5)

        rows: List[Dict] = captured if captured else []

        # ===== fallback 1: HTML table =====
        if not rows:
            try:
                rows = parse_report_from_html(page)
            except Exception:
                rows = []

        # ===== fallback 2: AG-GRID =====
        if not rows:
            try:
                rows = parse_report_from_ag_grid(page)
            except Exception:
                rows = []

        browser.close()
        return aggregate_rows_max(rows)


# ========= main logic =========
def main():
    state = load_state()
    prev_date: str = state.get("date", kyiv_today_str())
    prev_rows: Dict[str, Dict] = state.get("rows", {})
    today = kyiv_today_str()

    rows = fetch_rows()

    # ✅ FIX: если Keitaro временно отдал пусто — НЕ спамим, если уже были данные
    if not rows:
        if prev_rows:
            return
        tg_send("⚠️ Keitaro: no data")
        return

    # reset on new day
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
            old_conv = as_int(old.get("conversions"))
            old_sales = as_int(old.get("sales"))

            if r["conversions"] - old_conv > 0:
                conv_msgs.append(
                    "🟩 *CONVERSION ALERT*\n"
                    f"{header}\n"
                    f"Conversions: {old_conv} → {r['conversions']}"
                )

            if r["sales"] - old_sales > 0:
                # revenue (берём то, что есть)
                old_dep = as_float(old.get("deposit_revenue"))
                old_sale = as_float(old.get("sale_revenue"))
                old_rev = max(old_dep, old_sale)
                new_rev = max(as_float(r.get("deposit_revenue")), as_float(r.get("sale_revenue")))
                sale_msgs.append(
                    "🟦 *SALE ALERT*\n"
                    f"{header}\n"
                    f"Sales: {old_sales} → {r['sales']}\n"
                    f"Revenue Δ: {fmt_money(new_rev - old_rev)}"
                )
        else:
            if r["conversions"] > 0:
                conv_msgs.append(
                    "🟩 *CONVERSION ALERT*\n"
                    f"{header}\n"
                    f"Conversions: 0 → {r['conversions']}"
                )
            if r["sales"] > 0:
                new_rev = max(as_float(r.get("deposit_revenue")), as_float(r.get("sale_revenue")))
                sale_msgs.append(
                    "🟦 *SALE ALERT*\n"
                    f"{header}\n"
                    f"Sales: 0 → {r['sales']}\n"
                    f"Revenue: {fmt_money(new_rev)}"
                )

        new_map[k] = r

    blocks = conv_msgs + sale_msgs
    if blocks:
        tg_send("\n\n".join(blocks))

    save_state({"date": today, "rows": new_map})


if __name__ == "__main__":
    main()
