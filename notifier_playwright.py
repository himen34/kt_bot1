# notifier_playwright.py — стабильные алерты без дублей (Keitaro Campaigns Report)

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
GIST_FILENAME = os.getenv("GIST_FILENAME", "keitaro_campaigns_state.json")

SPEND_DIR = (os.getenv("SPEND_DIRECTION", "both") or "both").lower()
KYIV_TZ   = ZoneInfo(os.getenv("KYIV_TZ", "Europe/Kyiv"))
EPS = 0.009

# ========= utils =========
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
    if SPEND_DIR == "up":   return delta >  EPS
    if SPEND_DIR == "down": return delta < -EPS
    return abs(delta) > EPS

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

# ========= parsing (Campaigns report JSON) =========
def parse_campaigns_report(payload: dict) -> List[Dict]:
    rows = []
    for r in payload.get("rows", []):
        dims = r.get("dimensions", {}) if isinstance(r.get("dimensions"), dict) else {}
        country  = (dims.get("country") or "").strip()
        creative = (dims.get("creative_id") or "").strip()
        sub2     = (dims.get("sub_id_2") or "").strip()

        # если это не наш отчёт — пропускаем
        if not (country or creative or sub2):
            continue

        rows.append({
            "k": f"{country}|{creative}|{sub2}",
            "country": country,
            "creative": creative,
            "sub2": sub2,
            "cost":    as_float(r.get("cost")),
            "leads":   as_float(r.get("conversions")),  # ВАЖНО: leads = conversions
            "sales":   as_float(r.get("sales")),
            "revenue": as_float(r.get("revenue")),
        })
    return rows

# ========= fetch =========
def fetch_rows() -> List[Dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        )
        page = ctx.new_page()

        # ===== LOGIN (правильно под app-login) =====
        page.goto("https://digitaltraff.click/admin/", wait_until="domcontentloaded")

        # ждём, пока компонент отрисует инпуты
        try:
            page.wait_for_selector("app-login", timeout=20000)
        except PWTimeout:
            pass

        # заполняем по placeholder (как в твоём html: Username / Password)
        page.get_by_placeholder("Username").fill(LOGIN_USER)
        page.get_by_placeholder("Password").fill(LOGIN_PASS)
        page.get_by_role("button", name="Sign in").click()

        # ✅ НЕ ждём keitaro-app (его может не быть)
        # ждём либо исчезновения app-login, либо появления hash-роута
        ok = False
        try:
            page.wait_for_selector("app-login", state="detached", timeout=20000)
            ok = True
        except PWTimeout:
            pass
        if not ok:
            try:
                page.wait_for_url(re.compile(r".*/admin/#!/.*"), timeout=20000)
                ok = True
            except PWTimeout:
                pass
        if not ok:
            # если всё ещё логин-экран — значит логин не прошёл
            browser.close()
            return []

        # ===== XHR capture =====
        captured: List[Dict] = []
        best_score = -1.0

        def on_response(resp):
            nonlocal captured, best_score
            url = (resp.url or "").lower()
            if "/admin/api/reports/campaigns" not in url:
                return
            try:
                data = resp.json()
            except Exception:
                return
            rows = parse_campaigns_report(data)
            if not rows:
                return
            # выбираем самый “полный” пакет
            score = sum((r.get("cost") or 0.0) + (r.get("leads") or 0.0) + (r.get("sales") or 0.0) for r in rows)
            if score > best_score:
                best_score = score
                captured = rows

        ctx.on("response", on_response)

        # ===== open report =====
        page.goto(PAGE_URL, wait_until="domcontentloaded")

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass

        # 🔥 важно: форсим обновление отчёта, иначе XHR может не стрелять
        try:
            page.click("button[aria-label='Refresh']", timeout=4000)
        except Exception:
            try:
                page.click("button:has-text('Refresh')", timeout=4000)
            except Exception:
                pass

        time.sleep(2.5)

        browser.close()
        return captured

# ========= monotonic =========
def clamp_monotonic(new_v: float, old_v: float) -> float:
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
        tg_send("⚠️ No data fetched")
        return

    # reset on new day
    if prev_date != today:
        baseline = {r["k"]: r for r in rows}
        save_state({"date": today, "rows": baseline})
        return

    new_map: Dict[str, Dict] = {}
    best_spend_msg: Dict[str, Tuple[float, str]] = {}
    lead_msgs: List[str] = []
    sale_msgs: List[str] = []

    for r in rows:
        k = r["k"]
        old = prev_rows.get(k)

        if old:
            # монотонность
            r["cost"]  = clamp_monotonic(r["cost"],  old.get("cost", 0.0))
            r["leads"] = clamp_monotonic(r["leads"], old.get("leads", 0.0))
            r["sales"] = clamp_monotonic(r["sales"], old.get("sales", 0.0))
            r["revenue"] = clamp_monotonic(r["revenue"], old.get("revenue", 0.0))

            header = f"{r['country']} | {r['creative']} | {r['sub2']}"

            # SPEND
            delta_cost = r["cost"] - old.get("cost", 0.0)
            if direction_ok(delta_cost):
                p = pct(delta_cost, old.get("cost", 0.0))
                arrow = "🔺" if delta_cost > 0 else "🔻"
                msg = (
                    "🧊 *SPEND ALERT*\n"
                    f"{header}\n"
                    f"Cost: {fmt_money(old.get('cost', 0.0))} → {fmt_money(r['cost'])} "
                    f"(Δ {fmt_money(delta_cost)}, ~{p:.0f}%) {arrow}"
                )
                score = abs(delta_cost)
                prev_best = best_spend_msg.get(k)
                if (prev_best is None) or (score > prev_best[0] + 1e-9):
                    best_spend_msg[k] = (score, msg)

            # LEADS
            if r["leads"] - old.get("leads", 0.0) > EPS:
                lead_msgs.append(
                    "🟩 *LEAD ALERT*\n"
                    f"{header}\n"
                    f"Leads: {int(old.get('leads', 0))} → {int(r['leads'])}"
                )

            # SALES + revenue delta (как ты просил ранее)
            if r["sales"] - old.get("sales", 0.0) > EPS:
                delta_rev = r["revenue"] - old.get("revenue", 0.0)
                sale_msgs.append(
                    "🟦 *SALE ALERT*\n"
                    f"{header}\n"
                    f"Sales: {int(old.get('sales', 0))} → {int(r['sales'])}\n"
                    f"Revenue: {fmt_money(delta_rev)}"
                )

        else:
            # новый ключ
            header = f"{r['country']} | {r['creative']} | {r['sub2']}"

            if r["cost"] > EPS:
                msg = (
                    "🧊 *SPEND ALERT*\n"
                    f"{header}\n"
                    f"Cost: {fmt_money(0)} → {fmt_money(r['cost'])} (Δ {fmt_money(r['cost'])}) 🔺"
                )
                best_spend_msg[k] = (r["cost"], msg)

            if r["leads"] > EPS:
                lead_msgs.append(
                    "🟩 *LEAD ALERT*\n"
                    f"{header}\n"
                    f"Leads: 0 → {int(r['leads'])}"
                )

            if r["sales"] > EPS:
                sale_msgs.append(
                    "🟦 *SALE ALERT*\n"
                    f"{header}\n"
                    f"Sales: 0 → {int(r['sales'])}\n"
                    f"Revenue: {fmt_money(r['revenue'])}"
                )

        new_map[k] = r

    spend_msgs = [v[1] for v in best_spend_msg.values()]
    blocks = spend_msgs + lead_msgs + sale_msgs

    if blocks:
        tg_send("\n\n".join(blocks))

    save_state({"date": today, "rows": new_map})

if __name__ == "__main__":
    main()
