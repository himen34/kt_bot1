import os, json, time, re
from typing import Dict, List, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

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
GIST_FILENAME = os.getenv("GIST_FILENAME", "keitaro_favourite_state.json")

# Киев для reset
KYIV_TZ = ZoneInfo(os.getenv("KYIV_TZ", "Europe/Kyiv"))
EPS = 0.0001

DEBUG = (os.getenv("DEBUG_LOG", "0") == "1")

# ВАЖНО: домен берём из PAGE_URL, чтобы не было треша с куками/сессией
pu = urlparse(PAGE_URL)
BASE_URL = f"{pu.scheme}://{pu.netloc}".rstrip("/")


# ================= TG + LOGGER =================
LOG_BUF: List[str] = []

def _ts() -> str:
    return datetime.now(KYIV_TZ).strftime("%H:%M:%S")

def log(msg: str):
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    if DEBUG:
        LOG_BUF.append(line)

def tg_send(text: str, markdown: bool = True):
    if not CHAT_IDS:
        return
    for cid in CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={
                    "chat_id": cid,
                    "text": text,
                    "parse_mode": "Markdown" if markdown else None,
                    "disable_web_page_preview": True
                },
                timeout=20
            )
        except Exception:
            pass

def flush_debug_to_tg():
    if not DEBUG or not LOG_BUF:
        return
    # последние 45 строк
    chunk = "\n".join(LOG_BUF[-45:])
    tg_send("🧪 *DEBUG LOG*\n```\n" + chunk + "\n```", markdown=True)


# ================= utils =================
def kyiv_today_str() -> str:
    return datetime.now(KYIV_TZ).strftime("%Y-%m-%d")

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


# ================= state (Gist) =================
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


# ================= parsing (Favourite schema) =================
def parse_rows_from_payload(payload: dict) -> List[Dict]:
    rows: List[Dict] = []
    for r in payload.get("rows", []):
        dims = r.get("dimensions", {}) if isinstance(r.get("dimensions"), dict) else {}

        def g(k):
            return r.get(k) or dims.get(k) or ""

        campaign = str(g("campaign")).strip()
        country  = str(g("country")).strip()
        external = str(g("external_id")).strip()
        creative = str(g("creative_id")).strip()

        # пропускаем мусорные строки
        if not (campaign or country or external or creative):
            continue

        rows.append({
            "k": f"{campaign}|{country}|{external}|{creative}",
            "campaign": campaign,
            "country": country,
            "external_id": external,
            "creative_id": creative,
            "conversions": as_int(r.get("conversions")),
            "sales": as_int(r.get("sales")),
            "revenue": as_float(r.get("sale_revenue") or r.get("deposit_revenue") or r.get("revenue")),
        })
    return rows

def aggregate_rows_max(rows: List[Dict]) -> List[Dict]:
    acc: Dict[str, Dict] = {}
    for r in rows:
        k = r["k"]
        if k not in acc:
            acc[k] = dict(r)
        else:
            a = acc[k]
            a["conversions"] = max(a.get("conversions", 0), r.get("conversions", 0))
            a["sales"] = max(a.get("sales", 0), r.get("sales", 0))
            a["revenue"] = max(a.get("revenue", 0.0), r.get("revenue", 0.0))
    return list(acc.values())


# ================= FETCH (старый стиль: ловим любые JSON rows) =================
def fetch_rows() -> List[Dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        )
        page = ctx.new_page()

        log(f"BASE_URL = {BASE_URL}")
        log("Open login page")
        page.goto(f"{BASE_URL}/admin/", wait_until="domcontentloaded")

        # логин (универсально)
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

        # ждём, что логин-форма исчезнет / роут сменится
        try:
            page.wait_for_selector("app-login", state="detached", timeout=15000)
            log("Logged in (app-login detached)")
        except PWTimeout:
            # бывает, что app-login не отцепляется, но сессия есть — продолжаем
            log("Login wait timeout (continue)")

        captured: List[Dict] = []
        best_score = -1.0

        def on_response(resp):
            nonlocal captured, best_score
            # как в старом рабочем стиле: НЕ фильтруем URL, берём любой JSON с rows
            try:
                data = resp.json()
            except Exception:
                return
            if not isinstance(data, dict):
                return
            rr = data.get("rows")
            if not isinstance(rr, list) or not rr:
                return

            rows = parse_rows_from_payload(data)
            if not rows:
                return

            # "лучший пакет" — по наполненности
            score = len(rows) + 0.01 * sum((x.get("conversions", 0) + x.get("sales", 0)) for x in rows)
            if score > best_score:
                best_score = score
                captured = rows
                log(f"XHR captured: rows={len(rows)} score={best_score:.2f}")

        ctx.on("response", on_response)

        # открываем отчёт
        log("Open report page")
        page.goto(PAGE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        # 🔥 ФОРСИМ загрузку отчёта (иначе XHR может не уйти)
        log("Try force Refresh/Apply")
        forced = False
        # Refresh варианты
        for sel in [
            "button[aria-label='Refresh']",
            "button:has-text('Refresh')",
            "[title='Refresh']",
            "button:has-text('Оновити')",
            "button:has-text('Обновить')",
        ]:
            try:
                page.click(sel, timeout=2500)
                log(f"Clicked: {sel}")
                forced = True
                break
            except Exception:
                pass

        # Apply варианты (на некоторых сборках отчёт грузится после Apply)
        if not forced:
            for sel in [
                "button:has-text('Apply')",
                "button:has-text('Застосувати')",
                "button:has-text('Применить')",
            ]:
                try:
                    page.click(sel, timeout=2500)
                    log(f"Clicked: {sel}")
                    forced = True
                    break
                except Exception:
                    pass

        # ждём ответы
        t0 = time.time()
        while (time.time() - t0) < 12.0 and not captured:
            page.wait_for_timeout(600)

        # если не поймали — пробуем reload (как в старых хаках)
        if not captured:
            log("No XHR yet -> reload")
            try:
                page.reload(wait_until="domcontentloaded")
            except Exception:
                pass
            page.wait_for_timeout(2500)

            # ещё ждём
            t1 = time.time()
            while (time.time() - t1) < 10.0 and not captured:
                page.wait_for_timeout(600)

        browser.close()

        if not captured:
            log("Result: captured=0")
            return []

        log(f"Result: captured={len(captured)}")
        return aggregate_rows_max(captured)


# ================= MAIN =================
def main():
    log("Script started")

    state = load_state()
    prev_date = state.get("date", kyiv_today_str())
    prev_rows: Dict[str, Dict] = state.get("rows", {})
    today = kyiv_today_str()

    rows = fetch_rows()

    # если Keitaro временно отдал пусто — НЕ спамим, если сегодня уже были данные
    if not rows:
        if prev_rows:
            log("No data fetched (temporary). Skip alert.")
            flush_debug_to_tg()
            return
        tg_send("⚠️ Keitaro: no data", markdown=False)
        flush_debug_to_tg()
        return

    # reset daily (Kyiv)
    if prev_date != today:
        log("New day -> baseline saved")
        save_state({"date": today, "rows": {r["k"]: r for r in rows}})
        flush_debug_to_tg()
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
            old_rev = as_float(old.get("revenue"))

            if r["conversions"] - old_conv > 0:
                conv_msgs.append(
                    "🟩 *CONVERSION ALERT*\n"
                    f"{header}\n"
                    f"Conversions: {old_conv} → {r['conversions']}"
                )
                log(f"Alert: conversions up for {k}")

            if r["sales"] - old_sales > 0:
                delta_rev = r["revenue"] - old_rev
                sale_msgs.append(
                    "🟦 *SALE ALERT*\n"
                    f"{header}\n"
                    f"Sales: {old_sales} → {r['sales']}\n"
                    f"Revenue Δ: {fmt_money(delta_rev)}"
                )
                log(f"Alert: sales up for {k}")
        else:
            if r["conversions"] > 0:
                conv_msgs.append(
                    "🟩 *CONVERSION ALERT*\n"
                    f"{header}\n"
                    f"Conversions: 0 → {r['conversions']}"
                )
                log(f"Alert: new key conversions for {k}")

            if r["sales"] > 0:
                sale_msgs.append(
                    "🟦 *SALE ALERT*\n"
                    f"{header}\n"
                    f"Sales: 0 → {r['sales']}\n"
                    f"Revenue: {fmt_money(r['revenue'])}"
                )
                log(f"Alert: new key sales for {k}")

        new_map[k] = r

    blocks = conv_msgs + sale_msgs
    if blocks:
        tg_send("\n\n".join(blocks), markdown=True)
        log(f"Sent alerts: {len(blocks)}")
    else:
        log("No alerts (no deltas)")

    save_state({"date": today, "rows": new_map})
    log("State saved")
    flush_debug_to_tg()


if __name__ == "__main__":
    main()
