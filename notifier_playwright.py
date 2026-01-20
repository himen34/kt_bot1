#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, time
from typing import Any, Dict, List, Tuple, Optional
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# ================= ENV =================
BASE_URL = os.environ.get("KEITARO_BASE_URL", "https://digitaltraff.click").rstrip("/")
API_KEY  = os.environ["KEITARO_API_KEY"]              # положи в GitHub Secrets
# Сюда кладём JSON конфиг отчёта campaigns.report (ровно как в preferences)
REPORT_CONFIG_JSON = os.environ["KEITARO_REPORT_CONFIG_JSON"]

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID_1 = os.getenv("TELEGRAM_CHAT_ID_1") or os.getenv("TELEGRAM_CHAT_ID")
TG_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2")
CHAT_IDS = [c for c in (TG_CHAT_ID_1, TG_CHAT_ID_2) if c]

GIST_ID    = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = os.getenv("GIST_FILENAME", "keitaro_state.json")

KYIV_TZ = ZoneInfo("Europe/Kyiv")
EPS = 0.0001

# ================= helpers =================
def today() -> str:
    return datetime.now(KYIV_TZ).strftime("%Y-%m-%d")

def as_float(v: Any) -> float:
    try:
        if v is None:
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip().replace("$", "").replace(",", "")
        return float(s) if s else 0.0
    except:
        return 0.0

def money(x: float) -> str:
    return f"${x:,.2f}"

# ================= Telegram =================
def tg_send(text: str):
    for cid in CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": cid, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True},
                timeout=20
            )
        except:
            pass

# ================= Gist =================
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
    return {"date": today(), "rows": {}}

def save_state(state: Dict):
    url = f"https://api.github.com/gists/{GIST_ID}"
    requests.patch(url, headers={
        "Authorization": f"Bearer {GIST_TOKEN}",
        "Accept": "application/vnd.github+json"
    }, json={
        "files": {GIST_FILENAME: {"content": json.dumps(state, ensure_ascii=False, indent=2)}}
    }, timeout=30).raise_for_status()

# ================= Keitaro API client =================
def keitaro_post(path: str, payload: Dict) -> Optional[Dict]:
    """
    Пробуем несколько вариантов авторизации:
    - Bearer
    - X-Api-Key
    - api-key
    """
    url = f"{BASE_URL}{path}"
    headers_variants = [
        {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"},
        {"X-Api-Key": API_KEY, "Accept": "application/json"},
        {"api-key": API_KEY, "Accept": "application/json"},
    ]
    for headers in headers_variants:
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=40)
            if r.status_code == 200:
                return r.json()
        except:
            continue
    return None

def fetch_report() -> Dict:
    """
    В разных Keitaro пути отличаются, поэтому пробуем список.
    """
    report_cfg = json.loads(REPORT_CONFIG_JSON)

    # типичные эндпоинты Keitaro 10.x (у разных сборок может отличаться)
    endpoints = [
        "/admin_api/v1/report/build",
        "/admin_api/v1/report",
        "/admin_api/v1/reports/build",
        "/admin_api/v1/reports",
    ]

    for ep in endpoints:
        data = keitaro_post(ep, report_cfg)
        if isinstance(data, dict):
            return data

    raise RuntimeError("Keitaro API: не удалось получить отчёт. Проверь KEITARO_API_KEY / endpoint / права.")

# ================= Parsing rows (универсально) =================
def rows_from_response(resp: Dict) -> Tuple[List[Dict], List[str]]:
    """
    Возвращает: (rows_as_dicts, columns)
    Поддержка форматов:
      1) {"rows":[{...},{...}]}
      2) {"rows":[{"dimensions":{...}, "metrics":{...}}]}
      3) {"rows":[[...],[...]], "columns":[...]}
      4) {"data":[...], "columns":[...]} и т.п.
    """
    if not isinstance(resp, dict):
        return [], []

    rows = resp.get("rows") or resp.get("data") or []
    cols = resp.get("columns") or resp.get("cols") or []

    if not isinstance(rows, list) or not rows:
        return [], cols if isinstance(cols, list) else []

    # case 1: list of dicts
    if isinstance(rows[0], dict):
        out = []
        for r in rows:
            d = dict(r)
            # case 2: dimensions + metrics
            dims = r.get("dimensions")
            mets = r.get("metrics")
            if isinstance(dims, dict):
                d.update(dims)
            if isinstance(mets, dict):
                d.update(mets)
            out.append(d)
        return out, cols if isinstance(cols, list) else []

    # case 3: list of lists + columns
    if isinstance(rows[0], list) and isinstance(cols, list) and cols:
        out = []
        for arr in rows:
            d = {}
            for i, name in enumerate(cols):
                if i < len(arr):
                    d[str(name)] = arr[i]
            out.append(d)
        return out, cols

    return [], cols if isinstance(cols, list) else []

def normalize_rows(raw_rows: List[Dict]) -> List[Dict]:
    """
    Нормализуем строго под нужные поля:
    country, creative_id, sub_id_2, conversions, sales, revenue
    """
    out = []
    for r in raw_rows:
        country = r.get("country") or r.get("geo") or r.get("country_flag") or ""
        creative_id = r.get("creative_id") or ""
        sub2 = r.get("sub_id_2") or ""

        conversions = as_float(r.get("conversions") or r.get("conv") or r.get("leads"))
        sales = as_float(r.get("sales"))
        revenue = as_float(r.get("revenue"))

        if not (country or creative_id or sub2):
            continue

        out.append({
            "k": f"{country}|{creative_id}|{sub2}",
            "country": str(country),
            "creative_id": str(creative_id),
            "sub_id_2": str(sub2),
            "conversions": conversions,
            "sales": sales,
            "revenue": revenue,
        })
    return out

# ================= Main =================
def main():
    state = load_state()
    prev_date = state.get("date", today())
    prev_rows = state.get("rows", {})
    today_str = today()

    resp = fetch_report()
    raw_rows, _ = rows_from_response(resp)
    rows = normalize_rows(raw_rows)

    if not rows:
        tg_send("accs on vacation...")
        return

    # reset baseline на новый день
    if prev_date != today_str:
        save_state({"date": today_str, "rows": {r["k"]: r for r in rows}})
        tg_send("accs on vacation...")
        return

    lead_msgs = []
    sale_msgs = []
    new_map = {}

    for r in rows:
        k = r["k"]
        old = prev_rows.get(k, {"conversions": 0, "sales": 0, "revenue": 0})

        # LEAD
        if r["conversions"] - as_float(old.get("conversions")) > EPS:
            lead_msgs.append(
                "🟩 *LEAD ALERT*\n"
                f"Country: {r['country']}\n"
                f"Creative ID: {r['creative_id']}\n"
                f"Sub ID 2: {r['sub_id_2']}\n"
                f"Leads: {int(as_float(old.get('conversions')))} → {int(r['conversions'])}"
            )

        # SALE + revenue delta (сумма продажи)
        if r["sales"] - as_float(old.get("sales")) > EPS:
            delta_rev = r["revenue"] - as_float(old.get("revenue"))
            sale_msgs.append(
                "🟦 *SALE ALERT*\n"
                f"Country: {r['country']}\n"
                f"Creative ID: {r['creative_id']}\n"
                f"Sub ID 2: {r['sub_id_2']}\n"
                f"Sales: {int(as_float(old.get('sales')))} → {int(r['sales'])}\n"
                f"Revenue: {money(delta_rev)}"
            )

        new_map[k] = r

    if lead_msgs or sale_msgs:
        tg_send("\n\n".join(lead_msgs + sale_msgs))

    save_state({"date": today_str, "rows": new_map})

if __name__ == "__main__":
    main()
