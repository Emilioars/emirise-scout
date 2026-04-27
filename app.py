import json, os, re, sqlite3, time, random, threading
from datetime import datetime, timezone
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
import requests
from bs4 import BeautifulSoup

app = Flask(__name__, static_folder='static')
CORS(app)

DB_PATH = "seen.db"
LISTINGS_PATH = "listings.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

BASE_URL = "https://www.airbnb.fr/s/Aix-en-Provence--France/homes?refinement_paths%5B%5D=%2Fhomes&room_types%5B%5D=Entire%20home%2Fapt&price_min=100&ne_lat=43.536&ne_lng=5.456&sw_lat=43.524&sw_lng=5.438&zoom=15&search_type=MAP"

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

def scrape(mode="new"):
    print(f"[scrape] mode={mode}")
    results = []
    seen_ids = set()
    
    for page in range(5):
        delay = random.uniform(6, 18)
        print(f"[scrape] page {page+1} attente {delay:.0f}s")
        time.sleep(delay)
        
        url = BASE_URL + f"&items_offset={page*18}"
        if mode == "few":
            url += "&max_reviews=15"
        
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            print(f"[scrape] status={r.status_code}")
            
            # Cherche les données JSON dans la page
            soup = BeautifulSoup(r.text, "html.parser")
            
            # Cherche le script contenant les données
            for script in soup.find_all("script"):
                txt = script.string or ""
                if '"listing"' in txt and '"id"' in txt:
                    # Extraire les IDs des annonces
                    ids = re.findall(r'"id"\s*:\s*"?(\d{10,})"?', txt)
                    images = re.findall(r'https://a0\.muscache\.com/im/pictures/[^"\']+', txt)
                    prices = re.findall(r'"amount"\s*:\s*(\d+)', txt)
                    reviews = re.findall(r'"reviewsCount"\s*:\s*(\d+)', txt)
                    
                    for i, lid in enumerate(ids):
                        if lid in seen_ids:
                            continue
                        rev = int(reviews[i]) if i < len(reviews) else None
                        
                        if mode == "new" and (rev is None or rev > 0):
                            continue
                        if mode == "few" and (rev is None or rev > 15):
                            continue
                        
                        seen_ids.add(lid)
                        results.append({
                            "id": lid,
                            "url": f"https://www.airbnb.fr/rooms/{lid}",
                            "is_new": rev == 0 or rev is None,
                            "reviews": rev,
                            "price": f"{prices[i]}€" if i < len(prices) else None,
                            "image": images[i] if i < len(images) else None,
                            "mode": mode,
                            "detectedAt": datetime.now().strftime("%d/%m %H:%M"),
                        })
                    break
                        
        except Exception as e:
            print(f"[scrape] erreur page {page}: {e}")
    
    print(f"[scrape] {len(results)} annonces mode={mode}")
    return results

def full_scan():
    print("[scan] Démarrage")
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

init_db()
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
