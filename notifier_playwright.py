import os
import json
import time
import requests
from playwright.sync_api import sync_playwright

# ================== ENV ==================
LOGIN_USER = os.environ["LOGIN_USER"]
LOGIN_PASS = os.environ["LOGIN_PASS"]
PAGE_URL = os.environ["PAGE_URL"]

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_IDS = [
    os.environ.get("TELEGRAM_CHAT_ID_1"),
    os.environ.get("TELEGRAM_CHAT_ID_2"),
]
CHAT_IDS = [c for c in CHAT_IDS if c]

GIST_ID = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = os.environ.get("GIST_FILENAME", "keitaro_state.json")

# ================== TELEGRAM ==================
def tg_send(text: str):
    for chat_id in CHAT_IDS:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text}
        )

# ================== GIST ==================
def load_state():
    r = requests.get(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"token {GIST_TOKEN}"}
    )
    if r.status_code != 200:
        return {}
    files = r.json().get("files", {})
    if GIST_FILENAME not in files:
        return {}
    return json.loads(files[GIST_FILENAME]["content"])

def save_state(state: dict):
    requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={
            "Authorization": f"token {GIST_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "files": {
                GIST_FILENAME: {
                    "content": json.dumps(state, indent=2)
                }
            }
        }
    )

# ================== PARSER ==================
def fetch_rows():
    collected = []

    def is_keitaro_report(payload: dict) -> bool:
        if not isinstance(payload, dict):
            return False
        rows = payload.get("rows")
        if not isinstance(rows, list) or not rows:
            return False
        sample = rows[0]
        return (
            "country" in sample and
            "creative_id" in sample and
            "sub_id_2" in sample
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        def on_response(resp):
            try:
                if resp.request.method != "POST":
                    return
                data = resp.json()
                if is_keitaro_report(data):
                    collected.extend(data["rows"])
            except Exception:
                pass

        page.on("response", on_response)

        # LOGIN
        page.goto("https://digitaltraff.click/admin/login", timeout=60000)
        page.fill("input[name=email]", LOGIN_USER)
        page.fill("input[name=password]", LOGIN_PASS)
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")

        # OPEN REPORT PAGE (HASH URL)
        page.goto(PAGE_URL, timeout=60000)
        page.wait_for_load_state("networkidle")

        time.sleep(6)  # даём Angular/XHR отработать
        browser.close()

    return collected

# ================== MAIN ==================
def main():
    prev = load_state()
    curr = {}

    rows = fetch_rows()
    if not rows:
        tg_send("⚠️ Keitaro: rows not found (check filters or auth)")
        return

    for r in rows:
        country = r.get("country", "—")
        creative = r.get("creative_id", "—")
        sub2 = r.get("sub_id_2", "—")

        conversions = int(r.get("conversions") or 0)
        sales = int(r.get("sales") or 0)
        revenue = float(r.get("revenue") or 0)

        key = f"{country}|{creative}|{sub2}"

        old = prev.get(key, {
            "conversions": 0,
            "sales": 0,
            "revenue": 0
        })

        # LEAD
        if conversions > old["conversions"]:
            tg_send(
                f"🟢 LEAD\n"
                f"Country: {country}\n"
                f"Company: {sub2}\n"
                f"Leads: {old['conversions']} → {conversions}"
            )

        # SALE
        if sales > old["sales"]:
            delta = revenue - old["revenue"]
            tg_send(
                f"🔵 SALE\n"
                f"Country: {country}\n"
                f"Creative: {creative}\n"
                f"Company: {sub2}\n"
                f"Revenue: +${delta:.2f}"
            )

        curr[key] = {
            "conversions": conversions,
            "sales": sales,
            "revenue": revenue
        }

    save_state(curr)

# ================== RUN ==================
if __name__ == "__main__":
    main()
