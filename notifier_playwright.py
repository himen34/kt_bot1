import os, json, time, re
from typing import Dict, List
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

KYIV_TZ = ZoneInfo("Europe/Kyiv")
EPS = 0.0001
DEBUG = os.getenv("DEBUG_LOG", "0") == "1"

pu = urlparse(PAGE_URL)
BASE_URL = f"{pu.scheme}://{pu.netloc}".rstrip("/")


# ================= LOG =================
LOG_BUF: List[str] = []

def _ts():
    return datetime.now(KYIV_TZ).strftime("%H:%M:%S")

def log(msg):
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    if DEBUG:
        LOG_BUF.append(line)


# ================= TG =================
def tg_send(text: str, markdown: bool = True):
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
        except:
            pass


# ================= UTILS =================
def kyiv_today_str():
    return datetime.now(KYIV_TZ).strftime("%Y-%m-%d")

def as_int(v):
    try:
        return int(float(v or 0))
    except:
        return 0

def as_float(v):
    try:
        return float(v or 0)
    except:
        return 0.0

def fmt_money(v: float) -> str:
    try:
        return f"${float(v):.2f}"
    except:
        return "$0.00"


# ================= STATE =================
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
    requests.patch(url, headers={
        "Authorization": f"Bearer {GIST_TOKEN}",
        "Accept": "application/vnd.github+json"
    }, json={
        "files": {
            GIST_FILENAME: {
                "content": json.dumps(state, ensure_ascii=False, indent=2)
            }
        }
    }, timeout=30)


# ================= PARSING =================
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
            # 🔴 ВАЖНО: confirmed revenue
            "revenue": as_float(
                r.get("revenue_confirmed")
                or r.get("confirmed_revenue")
                or r.get("sale_revenue_confirmed")
                or r.get("sale_revenue")
                or r.get("deposit_revenue")
                or r.get("revenue")
            ),
        })

    return rows


# ================= FETCH =================
def fetch_rows() -> List[Dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        captured: List[Dict] = []
        best_score = -1

        def on_response(resp):
            nonlocal captured, best_score
            try:
                data = resp.json()
            except:
                return
            if not isinstance(data, dict) or not data.get("rows"):
                return

            rows = parse_rows_from_payload(data)
            if not rows:
                return

            score = len(rows)
            if score > best_score:
                best_score = score
                captured = rows
                log(f"XHR captured: rows={len(rows)}")

        ctx.on("response", on_response)

        page.goto(f"{BASE_URL}/admin/", wait_until="domcontentloaded")
        try:
            page.fill("input[type='text']", LOGIN_USER)
            page.fill("input[type='password']", LOGIN_PASS)
            page.click("button")
        except:
            pass

        try:
            page.wait_for_selector("app-login", state="detached", timeout=15000)
        except PWTimeout:
            pass

        page.goto(PAGE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        browser.close()

        return captured


# ================= MAIN =================
def main():
    log("Script started")

    state = load_state()
    prev_date = state.get("date")
    prev_rows = state.get("rows", {})
    today = kyiv_today_str()

    rows = fetch_rows()
    if not rows:
        log("No data fetched")
        return

    if prev_date != today:
        save_state({"date": today, "rows": {r["k"]: r for r in rows}})
        log("New day baseline saved")
        return

    new_map = {}
    conv_msgs = []
    sale_msgs = []

    for r in rows:
        k = r["k"]
        old = prev_rows.get(k, {})

        header = (
            f"Campaign: {r['campaign']}\n"
            f"Country: {r['country']}\n"
            f"External: {r['external_id']}\n"
            f"Creative: {r['creative_id']}"
        )

        old_conv = as_int(old.get("conversions"))
        old_sales = as_int(old.get("sales"))
        old_rev = as_float(old.get("revenue"))

        if r["conversions"] > old_conv:
            conv_msgs.append(
                "🟩 *CONVERSION ALERT*\n"
                f"{header}\n"
                f"{old_conv} → {r['conversions']}"
            )

        if r["sales"] > old_sales:
            delta_rev = r["revenue"] - old_rev
            rev_line = f"Confirmed Revenue Δ: {fmt_money(delta_rev)}"
            if abs(delta_rev) < EPS:
                rev_line = f"Confirmed Revenue: {fmt_money(r['revenue'])}"

            sale_msgs.append(
                "🟦 *SALE ALERT*\n"
                f"{header}\n"
                f"Sales: {old_sales} → {r['sales']}\n"
                f"{rev_line}"
            )

        new_map[k] = r

    if conv_msgs or sale_msgs:
        tg_send("\n\n".join(conv_msgs + sale_msgs))

    save_state({"date": today, "rows": new_map})
    log("State saved")


if __name__ == "__main__":
    main()
