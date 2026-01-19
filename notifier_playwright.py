#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Keitaro notifier (Campaign Stats) — stable alerts без дублей (GitHub Actions friendly)

Что делает:
- Логинится в Keitaro по адресу: https://digitaltraff.click/admin/
- Открывает PAGE_URL (страница статистики/кампаний)
- Ловит JSON-ответы (XHR/fetch) и пытается вытащить rows со столбцами:
  campaign/company + country + leads + sales + revenue
- Сравнивает с состоянием в GitHub Gist:
  - LEAD alert: Campaign + Country + Leads delta
  - SALE alert: Campaign + Country + Sales delta + Revenue delta (сумма конкретной продажи)
- Сбрасывает baseline в полночь по Киеву
- Если данных нет — не падает, просто отправляет “accs on vacation...”

ENV:
LOGIN_USER, LOGIN_PASS, PAGE_URL
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID или TELEGRAM_CHAT_ID_1/2
GIST_ID, GIST_TOKEN, (опц.) GIST_FILENAME
(опц.) SPEND_DIRECTION (не используется в этом варианте)
"""

import os, json, time, re
from typing import Dict, List, Optional, Any
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
TG_CHAT_ID_1 = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID_1")
TG_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2")
CHAT_IDS = [cid for cid in (TG_CHAT_ID_1, TG_CHAT_ID_2) if cid]

GIST_ID    = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = os.getenv("GIST_FILENAME", "keitaro_state_v2.json")

KYIV_TZ   = ZoneInfo(os.getenv("KYIV_TZ", "Europe/Kyiv"))
EPS = 0.009  # всё, что > ~1 цент — считаем изменением

LOGIN_URL = "https://digitaltraff.click/admin/"

# ========= utils =========
def now_kyiv() -> datetime:
    return datetime.now(KYIV_TZ)

def kyiv_today_str() -> str:
    return now_kyiv().strftime("%Y-%m-%d")

def fmt_money(x: float) -> str:
    return f"${x:,.2f}"

def as_float(v: Any) -> float:
    try:
        if v is None:
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        s = s.replace("$", "").replace(",", "")
        return float(s) if s else 0.0
    except Exception:
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
                json={
                    "chat_id": cid,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True
                },
                timeout=20
            )
        except Exception:
            pass

# ========= parsing (flexible) =========
def _guess_rows(payload: Any) -> Optional[List[Dict]]:
    """
    Возвращает список dict-строк, если payload похож на отчёт.
    Поддерживает варианты:
    - {"rows":[...]}
    - {"data":[...]}
    - {"result":{"rows":[...]}}
    - [{"campaign":..., "country":...}, ...]
    """
    if payload is None:
        return None

    # list of dicts
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload

    if isinstance(payload, dict):
        if isinstance(payload.get("rows"), list):
            return payload["rows"]
        if isinstance(payload.get("data"), list):
            return payload["data"]
        res = payload.get("result")
        if isinstance(res, dict) and isinstance(res.get("rows"), list):
            return res["rows"]
        # иногда глубже
        for k in ("payload", "report", "table"):
            v = payload.get(k)
            if isinstance(v, dict):
                rr = _guess_rows(v)
                if rr:
                    return rr
    return None

def _pick(d: Dict, *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return ""

def parse_rows_generic(payload: Any) -> List[Dict]:
    """
    Универсальный парсер. Пытается собрать нужные поля:
    - campaign (название компании/кампании как в таблице)
    - geo/country
    - leads
    - sales
    - revenue
    """
    rows_raw = _guess_rows(payload)
    if not rows_raw:
        return []

    out: List[Dict] = []
    for r in rows_raw:
        if not isinstance(r, dict):
            continue

        # Иногда Keitaro кладёт dimensions отдельно
        dims = r.get("dimensions")
        if isinstance(dims, dict):
            rr = dict(dims)
            rr.update(r)
            r = rr

        campaign = _pick(
            r,
            "campaign", "campaign_name", "company", "company_name", "advertiser",
            "name", "title"
        )

        geo = _pick(
            r,
            "country", "country_code", "country_iso2", "geo", "geo_code", "location"
        )

        # Метрики
        leads = as_float(_pick(r, "leads", "conversions", "conv", "lead"))
        sales = as_float(_pick(r, "sales", "purchases", "orders", "sale"))
        revenue = as_float(_pick(r, "revenue", "income", "profit", "rev"))

        # Фильтр мусора: нам нужны строки, где есть хотя бы leads/sales/revenue и campaign
        if not str(campaign).strip():
            continue
        if (leads < EPS) and (sales < EPS) and (revenue < EPS):
            continue

        # Ключ: campaign + geo (как ты хотел для кампаний)
        k = f"{str(campaign).strip()}|{str(geo).strip()}"
        out.append({
            "k": k,
            "campaign": str(campaign).strip(),
            "geo": str(geo).strip(),
            "leads": leads,
            "sales": sales,
            "revenue": revenue,
        })

    return out

def aggregate_rows_max(rows: List[Dict]) -> List[Dict]:
    """Склейка дубликатов за запуск: берём максимум по leads/sales/revenue на один ключ."""
    acc: Dict[str, Dict] = {}
    for r in rows:
        k = r["k"]
        if k not in acc:
            acc[k] = dict(r)
        else:
            a = acc[k]
            a["leads"] = max(a["leads"], r["leads"])
            a["sales"] = max(a["sales"], r["sales"])
            a["revenue"] = max(a["revenue"], r["revenue"])
    return list(acc.values())

# ========= fetch =========
def fetch_rows() -> List[Dict]:
    """
    Основная идея: НЕ парсим HTML-таблицы.
    Ловим JSON ответы и выбираем “лучший” пакет по сумме (leads+sales+revenue).
    """
    captured: List[Dict] = []
    best_score = -1.0

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

        def on_response(resp):
            nonlocal captured, best_score
            try:
                if resp.status != 200:
                    return
                ct = (resp.headers.get("content-type") or "").lower()
                if "application/json" not in ct:
                    # иногда JSON без content-type — попробуем по URL
                    url = (resp.url or "").lower()
                    if not any(x in url for x in ("report", "stats", "summary", "campaign", "dashboard", "api")):
                        return
                data = resp.json()
            except Exception:
                return

            rows = parse_rows_generic(data)
            if not rows:
                return

            score = 0.0
            for r in rows:
                score += r.get("leads", 0.0) + r.get("sales", 0.0) + r.get("revenue", 0.0)
            if score > best_score:
                captured = rows
                best_score = score

        ctx.on("response", on_response)

        # ---- LOGIN ----
        page.goto(LOGIN_URL, wait_until="domcontentloaded")

        # максимально толерантные селекторы
        try:
            page.fill("input[name='login'], input[name='username'], input[type='text']", LOGIN_USER)
        except Exception:
            pass
        try:
            page.fill("input[name='password'], input[type='password']", LOGIN_PASS)
        except Exception:
            pass

        # кнопка логина
        try:
            page.click("button[type='submit']")
        except Exception:
            try:
                page.get_by_role("button", name=re.compile("sign in|log in|login|увійти|войти", re.I)).click()
            except Exception:
                pass

        # дождёмся ухода логин-формы/перехода
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass

        # ---- GO TO REPORT ----
        page.goto(PAGE_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PWTimeout:
            pass

        # дать SPA догрузить XHR
        time.sleep(2.0)

        browser.close()

    return aggregate_rows_max(captured)

# ========= monotonic =========
def clamp_monotonic(new_v: float, old_v: float) -> float:
    """Запрет «отката»: метрика не может стать меньше прошлой."""
    if old_v is None:
        return new_v
    return new_v if new_v >= (old_v - 1e-6) else old_v

# ========= main =========
def main():
    state = load_state()
    prev_date: str = state.get("date", kyiv_today_str())
    prev_rows: Dict[str, Dict] = state.get("rows", {})
    today = kyiv_today_str()

    rows = fetch_rows()
    if not rows:
        tg_send("accs on vacation...")
        return

    # Сброс у полуночи по Киеву
    if prev_date != today:
        baseline = {r["k"]: r for r in rows}
        save_state({"date": today, "rows": baseline})
        tg_send("accs on vacation...")
        return

    new_map: Dict[str, Dict] = {}
    lead_msgs: List[str] = []
    sale_msgs: List[str] = []

    for r in rows:
        k = r["k"]
        old = prev_rows.get(k)

        if old:
            # монотонность
            r["leads"]   = clamp_monotonic(r.get("leads", 0.0), old.get("leads", 0.0))
            r["sales"]   = clamp_monotonic(r.get("sales", 0.0), old.get("sales", 0.0))
            r["revenue"] = clamp_monotonic(r.get("revenue", 0.0), old.get("revenue", 0.0))

            # LEAD
            if r["leads"] - old.get("leads", 0.0) > EPS:
                lead_msgs.append(
                    "🟩 *LEAD ALERT*\n"
                    f"Campaign: {r['campaign']}\n"
                    f"Country: {r['geo']}\n"
                    f"Leads: {int(old.get('leads', 0))} → {int(r['leads'])}"
                )

            # SALE + Revenue delta (сумма конкретной продажи)
            if r["sales"] - old.get("sales", 0.0) > EPS:
                delta_rev = r["revenue"] - old.get("revenue", 0.0)
                sale_msgs.append(
                    "🟦 *SALE ALERT*\n"
                    f"Campaign: {r['campaign']}\n"
                    f"Country: {r['geo']}\n"
                    f"Sales: {int(old.get('sales', 0))} → {int(r['sales'])}\n"
                    f"Revenue: {fmt_money(delta_rev)}"
                )
        else:
            # новый ключ
            if r.get("leads", 0.0) > EPS:
                lead_msgs.append(
                    "🟩 *LEAD ALERT*\n"
                    f"Campaign: {r['campaign']}\n"
                    f"Country: {r['geo']}\n"
                    f"Leads: 0 → {int(r['leads'])}"
                )

            if r.get("sales", 0.0) > EPS:
                # revenue delta = весь revenue, потому что ранее было 0
                sale_msgs.append(
                    "🟦 *SALE ALERT*\n"
                    f"Campaign: {r['campaign']}\n"
                    f"Country: {r['geo']}\n"
                    f"Sales: 0 → {int(r['sales'])}\n"
                    f"Revenue: {fmt_money(r.get('revenue', 0.0))}"
                )

        new_map[k] = {
            "campaign": r.get("campaign", ""),
            "geo": r.get("geo", ""),
            "leads": float(r.get("leads", 0.0)),
            "sales": float(r.get("sales", 0.0)),
            "revenue": float(r.get("revenue", 0.0)),
        }

    blocks = lead_msgs + sale_msgs
    if blocks:
        tg_send("\n\n".join(blocks))

    save_state({"date": today, "rows": new_map})

if __name__ == "__main__":
    main()
