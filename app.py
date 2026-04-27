import asyncio
import json
import os
import re
import sqlite3
import time
import random
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from playwright.sync_api import sync_playwright

app = Flask(__name__, static_folder='static')
CORS(app)

DB_PATH = "seen.db"
LISTINGS_PATH = "listings.json"

SEARCH_URL = "https://www.airbnb.fr/s/Aix-en-Provence--France/homes?refinement_paths%5B%5D=%2Fhomes&room_types%5B%5D=Entire%20home%2Fapt&price_min=100&ne_lat=43.536&ne_lng=5.456&sw_lat=43.524&sw_lng=5.438&zoom=15&search_type=MAP"

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS seen (listing_id TEXT PRIMARY KEY, first_seen_utc TEXT)")
    con.commit()
    con.close()

def load_seen():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT listing_id FROM seen")
    rows = cur.fetchall()
    con.close()
    return {r[0] for r in rows}

def save_seen(new_ids):
    if not new_ids:
        return
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cur.executemany("INSERT OR IGNORE INTO seen(listing_id, first_seen_utc) VALUES(?, ?)", [(lid, now) for lid in new_ids])
    con.commit()
    con.close()

def load_listings():
    if os.path.exists(LISTINGS_PATH):
        with open(LISTINGS_PATH, "r") as f:
            return json.load(f)
    return []

def save_listings(listings):
    with open(LISTINGS_PATH, "w") as f:
        json.dump(listings, f)

def extract_listing_id(url):
    try:
        path = urlparse(url).path
        m = re.search(r"/rooms/(\d+)", path)
        return m.group(1) if m else None
    except:
        return None

def has_new_badge(text):
    if not text:
        return False
    t = text.lower()
    return "nouveau" in t or "new" in t

def extract_review_count(text):
    if not text:
        return None
    t = " ".join(text.split())
    m = re.search(r"\b(\d{1,4})\s*(commentaires|avis|reviews)\b", t, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m2 = re.search(r"\((\d{1,4})\)", t)
    if m2:
        return int(m2.group(1))
    return None

def extract_image(card):
    try:
        img = card.query_selector("img")
        if img:
            src = img.get_attribute("src")
            if src:
                return src
    except:
        pass
    return None

def extract_price(text):
    if not text:
        return None
    m = re.search(r"(\d[\d\s]*)\s*€", text)
    if m:
        return m.group(0).strip()
    return None

def scrape(mode="new"):
    print(f"[scrape] Démarrage mode={mode}")
    results = []
    url = SEARCH_URL
    if mode == "few":
        url = SEARCH_URL + "&max_reviews=15"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
            viewport={"width": 1440, "height": 900},
            locale="fr-FR",
            timezone_id="Europe/Paris",
        )
        page = context.new_page()

        for attempt in range(2):
            try:
                page.goto(url, timeout=120000, wait_until="domcontentloaded")
                break
            except:
                if attempt == 1:
                    browser.close()
                    return []
                time.sleep(2)

        page.wait_for_timeout(random.randint(1500, 3000))
        page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.6)")
        page.wait_for_timeout(800)

        visited = 0
        seen_urls = set()

        while visited < 20:
            visited += 1
            try:
                page.wait_for_selector("a[href*='/rooms/']", timeout=30000)
            except:
                break

            anchors = page.locator("a[href*='/rooms/']").element_handles()
            for a in anchors:
                try:
                    href = a.get_attribute("href")
                    if not href or "/rooms/" not in href:
                        continue
                    url_clean = href.split("?")[0]
                    if url_clean in seen_urls:
                        continue
                    seen_urls.add(url_clean)

                    lid = extract_listing_id(url_clean)
                    if not lid:
                        continue

                    card = a.evaluate_handle("(el) => el.closest('article') || el.closest('div[data-testid]') || el.parentElement")
                    txt = ""
                    try:
                        txt = card.evaluate("(el) => el.innerText || ''")
                    except:
                        pass

                    is_new = has_new_badge(txt)
                    reviews = extract_review_count(txt)
                    price = extract_price(txt)

                    # Filtre selon le mode
                    if mode == "new" and not is_new:
                        continue
                    if mode == "few" and (reviews is None or reviews > 15):
                        continue

                    # Image de couverture
                    image = None
                    try:
                        img_el = card.evaluate_handle("(el) => el.querySelector('img')")
                        image = img_el.evaluate("(el) => el ? el.src : null")
                    except:
                        pass

                    results.append({
                        "id": lid,
                        "url": f"https://www.airbnb.fr/rooms/{lid}",
                        "is_new": is_new,
                        "reviews": reviews,
                        "price": price,
                        "image": image,
                        "mode": mode,
                        "detectedAt": datetime.now().strftime("%d/%m %H:%M"),
                    })
                except:
                    continue

            # Page suivante
            next_clicked = False
            for sel in ["[data-testid='pagination-right']", "button[aria-label*='Suivant']", "button[aria-label*='Next']"]:
                try:
                    btn = page.locator(sel)
                    if btn.count() > 0:
                        disabled = btn.get_attribute("disabled")
                        if disabled is not None:
                            break
                        page.wait_for_timeout(random.randint(500, 1500))
                        btn.first.click()
                        page.wait_for_timeout(2000)
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.6)")
                        page.wait_for_timeout(600)
                        next_clicked = True
                        break
                except:
                    continue
            if not next_clicked:
                break

        browser.close()

    print(f"[scrape] {len(results)} annonces trouvées mode={mode}")
    return results

def full_scan():
    print("[scan] Démarrage scan complet")
    seen = load_seen()
    all_listings = load_listings()

    for mode in ["new", "few"]:
        results = scrape(mode)
        new_ones = [r for r in results if r["id"] not in seen]
        if new_ones:
            for l in new_ones:
                seen.add(l["id"])
                all_listings.insert(0, l)
            save_seen([l["id"] for l in new_ones])
            print(f"[scan] {len(new_ones)} nouvelles annonces mode={mode}")

    save_listings(all_listings[:200])
    print("[scan] Terminé")

# Init
init_db()

# Scheduler 8h, 12h, 17h
scheduler = BackgroundScheduler()
scheduler.add_job(full_scan, "cron", hour="8,12,17", minute=0)
scheduler.start()

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/listings")
def get_listings():
    return jsonify(load_listings())

@app.route("/api/scan", methods=["POST"])
def manual_scan():
    threading.Thread(target=full_scan).start()
    return jsonify({"status": "started"})

@app.route("/api/clear", methods=["POST"])
def clear():
    save_listings([])
    return jsonify({"status": "cleared"})

@app.route("/api/status")
def status():
    listings = load_listings()
    return jsonify({
        "total": len(listings),
        "new": sum(1 for l in listings if l.get("is_new")),
        "few": sum(1 for l in listings if not l.get("is_new")),
        "lastScan": listings[0]["detectedAt"] if listings else None
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
