#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, time, re
from typing import Dict, List
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
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

GIST_ID    = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = "keitaro_state_v3.json"

KYIV_TZ = ZoneInfo("Europe/Kyiv")
EPS = 0.009

# ========= UTILS =========
def now():
    return datetime.now(KYIV_TZ)

def today():
    return now().strftime("%Y-%m-%d")

def fmt_money(x: float) -> str:
    return f"${x:,.2f}"

# ========= STATE =========
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
    payload = {
        "files": {
            GIST_FILENAME: {
                "content": json.dumps(state, ensure_ascii=False, indent=2)
            }
        }
    }
    requests.patch(
        url,
        headers={"Authorization": f"Bearer {GIST_TOKEN}"},
        json=payload,
        timeout=30
    )

# ========= TELEGRAM =========
def tg_send(text: str):
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        },
        timeout=20
    )

# ========= PARSE =========
def parse_report_from_json(payload: dict) -> List[Dict]:
    rows = []
    for r in payload.get("rows", []):
        dims = r.get("dimensions", {})
        campaign = dims.get("campaign") or r.get("campaign") or ""
        geo = dims.get("country") or dims.get("geo") or ""

        rows.append({
            "k": f"{campaign}|{geo}",
            "campaign": str(campaign),
            "geo": str(geo),
            "leads": float(r.get("leads", 0)),
            "sales": float(r.get("sales", 0)),
            "revenue": float(r.get("revenue", 0)),
        })
    return rows

def parse_report_from_html(page) -> List[Dict]:
    rows = []
    page.wait_for_selector("table", timeout=15000)

    table = page.query_selector("table")
    headers = [h.inner_text().lower() for h in table.query_selector_all("thead th")]

    def idx(name):
        for i, h in enumerate(headers):
            if name in h:
                return i
        return -1

    i_campaign = idx("кампан")
    i_geo = idx("країн")
    i_leads = idx("конв")
    i_sales = idx("продаж")
    i_rev = idx("дохід")

    for tr in table.query_selector_all("tbody tr"):
        tds = tr.query_selector_all("td")
        def g(i):
            try:
                return tds[i].inner_text().strip()
            except:
                return ""

        def f(x):
            return float(x.replace("$","").replace(",","") or 0)

        campaign = g(i_campaign)
        geo = g(i_geo)

        rows.append({
            "k": f"{campaign}|{geo}",
            "campaign": campaign,
            "geo": geo,
            "leads": f(g(i_leads)),
            "sales": f(g(i_sales)),
            "revenue": f(g(i_rev)),
        })
    return rows

# ========= FETCH =========
def fetch_rows() -> List[Dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto("https://trident.partners/admin/")
        page.fill("input[type='text']", LOGIN_USER)
        page.fill("input[type='password']", LOGIN_PASS)
        page.click("button")

        page.goto(PAGE_URL)
        time.sleep(2)

        rows = []
        page.on("response", lambda r: rows.extend(
            parse_report_from_json(r.json())
            if "report" in r.url and r.ok else []
        ))

        time.sleep(3)
        if not rows:
            rows = parse_report_from_html(page)

        browser.close()
        return rows

# ========= MAIN =========
def main():
    state = load_state()
    prev_rows = state.get("rows", {})
    prev_date = state.get("date")

    rows = fetch_rows()
    if not rows:
        return

    if prev_date != today():
        save_state({"date": today(), "rows": {r["k"]: r for r in rows}})
        return

    new_state = {}
    messages = []

    for r in rows:
        old = prev_rows.get(r["k"], {
            "leads": 0,
            "sales": 0,
            "revenue": 0
        })

        # LEAD
        if r["leads"] > old["leads"]:
            messages.append(
                "🟩 *LEAD ALERT*\n"
                f"Campaign: {r['campaign']}\n"
                f"Country: {r['geo']}\n"
                f"Leads: {int(old['leads'])} → {int(r['leads'])}"
            )

        # SALE + DELTA REVENUE
        if r["sales"] > old["sales"]:
            delta_rev = r["revenue"] - old["revenue"]
            messages.append(
                "🟦 *SALE ALERT*\n"
                f"Campaign: {r['campaign']}\n"
                f"Country: {r['geo']}\n"
                f"Sales: {int(old['sales'])} → {int(r['sales'])}\n"
                f"Revenue: {fmt_money(delta_rev)}"
            )

        new_state[r["k"]] = r

    if messages:
        tg_send("\n\n".join(messages))

    save_state({"date": today(), "rows": new_state})

if __name__ == "__main__":
    main()
