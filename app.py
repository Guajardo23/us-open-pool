"""
The Open Championship Pool 2026 — Flask backend
Deploy to Railway: https://railway.app  (files persist across restarts)

Endpoints:
  GET  /                -> index.html
  GET  /api/field       -> sorted golfer list
  GET  /api/status      -> full app state (picks, standings, results)
  POST /api/picks       -> submit / update picks  { name, picks:[5] }
  POST /api/refresh     -> force an immediate standings scrape (manual trigger)
"""

import json, os, ssl, threading, time, unicodedata, urllib.request
from collections import defaultdict
from datetime import datetime, date

from flask import Flask, request, jsonify, send_from_directory
import zoneinfo

app = Flask(__name__)

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
# Persist data files to /data (Railway Volume mount point) if it exists,
# otherwise fall back to the app directory for local development.
DATA_DIR       = "/data" if os.path.isdir("/data") else BASE_DIR
PICKS_FILE     = os.path.join(DATA_DIR, "picks.json")
STANDINGS_FILE = os.path.join(DATA_DIR, "standings.json")
EARNINGS_FILE  = os.path.join(DATA_DIR, "earnings.json")

ET = zoneinfo.ZoneInfo("America/New_York")
PT = zoneinfo.ZoneInfo("America/Los_Angeles")

# Picks lock: Thursday July 16, 2026 at 1:30 AM Eastern (≈6:30 AM BST first tee at Birkdale)
LOCK_DT = datetime(2026, 7, 16, 1, 30, 0, tzinfo=ET)

# ── Tournament info ───────────────────────────────────────────────────────────
TOURNAMENT_NAME   = "The Open Championship"
TOURNAMENT_YEAR   = "2026"
TOURNAMENT_VENUE  = "Royal Birkdale · Southport, England"
PAR               = 70   # Royal Birkdale par 70

# ── Payout table (~$17M purse, The Open Championship structure) ───────────────
PAYOUT = {
     1: 3100000,  2: 1759000,  3: 1128000,  4:  878000,  5:  707000,
     6:  613000,  7:  527000,  8:  442000,  9:  402000, 10:  361500,
    11:  328500, 12:  302000, 13:  283000, 14:  264000, 15:  250500,
    16:  237500, 17:  224500, 18:  211500, 19:  198500, 20:  188000,
    21:  179500, 22:  171000, 23:  162500, 24:  154000, 25:  145500,
    26:  137000, 27:  132000, 28:  127000, 29:  122000, 30:  117000,
    31:  112000, 32:  107000, 33:  102000, 34:   97500, 35:   93500,
    36:   90000, 37:   86500, 38:   83500, 39:   80500, 40:   77500,
    41:   74500, 42:   71500, 43:   68500, 44:   65500, 45:   62500,
    46:   59500, 47:   56500, 48:   54000, 49:   52000, 50:   50500,
}
MC_PAYOUT = 0

# ── The Open Championship 2026 field (Royal Birkdale) ────────────────────────
# Tier definitions are odds-based approximations built from the confirmed field.
# Exact ESPN name spellings must be validated once ESPN publishes the field
# (before Thursday's lock) so every pick scores.
# Tier 1 — Top ~15 favorites          → pick 2
# Tier 2 — Contenders                  → pick 2
# Tier 3 — Longshots / qualifiers      → pick 1

TIER1 = sorted([
    "Scottie Scheffler", "Rory McIlroy", "Jon Rahm", "Xander Schauffele",
    "Bryson DeChambeau", "Ludvig Åberg", "Tommy Fleetwood", "Collin Morikawa",
    "Viktor Hovland", "Tyrrell Hatton", "Justin Thomas", "Shane Lowry",
    "Robert MacIntyre", "Joaquin Niemann", "Cameron Young",
])

TIER2 = sorted([
    "Matt Fitzpatrick", "Russell Henley", "Hideki Matsuyama", "Jordan Spieth",
    "Wyndham Clark", "J.J. Spaun", "Patrick Cantlay", "Sepp Straka",
    "Sam Burns", "Keegan Bradley", "Justin Rose", "Corey Conners",
    "Ben Griffin", "Maverick McNealy", "Min Woo Lee", "Aaron Rai",
    "Sungjae Im", "Si Woo Kim", "Tom Kim", "Akshay Bhatia",
    "Jason Day", "Adam Scott", "Cameron Smith", "Brian Harman",
    "Harris English", "Nicolai Højgaard", "Rasmus Højgaard", "Thomas Detry",
    "Sahith Theegala", "Michael Kim", "Chris Gotterup", "Marco Penge",
    "Daniel Berger", "Nick Taylor", "Andrew Novak", "Alex Noren",
    "Kurt Kitayama", "Rickie Fowler", "Jake Knapp", "Patrick Reed",
    "Gary Woodland", "Matt McCarty", "Harry Hall", "Jacob Bridgeman",
    "Max Greyserman", "Max Homa", "Brooks Koepka", "Ryan Fox",
    "Billy Horschel", "Matt Wallace", "Li Haotong", "Sam Stevens",
    "Nico Echavarria", "Pierceson Coody", "Keith Mitchell", "Sami Välimäki",
])

TIER3 = sorted([
    "Padraig Harrington", "Stewart Cink", "Darren Clarke", "David Duval",
    "Francesco Molinari", "Louis Oosthuizen", "Henrik Stenson", "Ryo Hisatsune",
    "Eric Cole", "David Puig", "Dan Brown", "Laurie Canter",
    "Rasmus Neergaard-Petersen", "Keita Nakajima", "Daniel Hillier", "Ángel Ayora",
    "Joakim Lagergren", "Jordan Smith", "Dan Bradbury", "Hennie du Plessis",
    "Andy Sullivan", "Bernd Wiesberger", "Kazuki Higa", "Casey Jarvis",
    "Kota Kaneko", "Travis Smyth", "Scott Vincent", "Martin Couvra",
    "Joe Dean", "John Parry", "Adrien Saddier", "Jayden Schaper",
    "Kristoffer Reitan", "Mason Howell", "Jackson Koivun", "Fifa Laopakdee",
    "Mateo Pulcini", "Jack Buchanan", "Stuart Grehan", "Tim Wiedemeyer",
    "Lev Grinberg",
])

FIELD = sorted(TIER1 + TIER2 + TIER3)
TIERS = {g: 1 for g in TIER1} | {g: 2 for g in TIER2} | {g: 3 for g in TIER3}

TIER_LABELS = {
    1: "Tier 1 — Favorites",
    2: "Tier 2 — Contenders",
    3: "Tier 3 — Longshots",
}
# picks must be submitted in slot order: [T1, T1, T2, T2, T3]
PICK_TIER_SLOTS = [1, 1, 2, 2, 3]

# normalized lookup for forgiving name matching (handles accent differences)
def _norm(s):
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    ).lower().strip()

FIELD_NORM = {_norm(g): g for g in FIELD}

# ── File helpers ──────────────────────────────────────────────────────────────
def load_json(path, default=None):
    if default is None:
        default = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ── Lock ──────────────────────────────────────────────────────────────────────
def is_locked():
    return datetime.now(ET) >= LOCK_DT

# ── Earnings / leaderboard calculation ───────────────────────────────────────
def parse_position(pos_str):
    if not pos_str:
        return None
    up = pos_str.strip().upper()
    if up in ("MC", "CUT", "WD", "DQ"):
        return None
    try:
        return int(up.replace("T", ""))
    except ValueError:
        return None

def compute_projected_earnings(golfers):
    pos_groups = defaultdict(list)
    mc_golfers, no_pos = [], []
    for name, gdata in golfers.items():
        pos_str = gdata.get("position", "").strip().upper()
        if not pos_str:
            no_pos.append(name)
        elif pos_str in ("MC", "CUT", "WD", "DQ"):
            mc_golfers.append(name)
        else:
            pos_num = parse_position(pos_str)
            if pos_num:
                pos_groups[pos_num].append(name)
            else:
                no_pos.append(name)

    earnings = {}
    for pos_num, names in pos_groups.items():
        count = len(names)
        total = sum(PAYOUT.get(pos_num + i, PAYOUT.get(50, 56700)) for i in range(count))
        per = total // count
        for name in names:
            earnings[name] = per
    for name in mc_golfers:
        earnings[name] = MC_PAYOUT
    for name in no_pos:
        earnings[name] = 0
    return earnings

def build_results(picks, earnings_map, golfers):
    results = []
    for name, pick_list in picks.items():
        total = 0
        pick_details = []
        for golfer in pick_list:
            amt = earnings_map.get(golfer, 0)
            gdata = golfers.get(golfer, {})
            pick_details.append({
                "golfer":   golfer,
                "amount":   amt,
                "position": gdata.get("position", ""),
                "score":    gdata.get("score", ""),
                "today":    gdata.get("today", ""),
            })
            total += amt
        results.append({"name": name, "picks": pick_details, "total": total})

    results.sort(key=lambda x: (-x["total"], x["name"].lower()))
    for i, r in enumerate(results):
        if i == 0:
            r["rank"] = 1
        elif r["total"] == results[i - 1]["total"]:
            r["rank"] = results[i - 1]["rank"]
        else:
            r["rank"] = i + 1
    return results

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("templates", "index.html")

@app.route("/api/field")
def get_field():
    return jsonify({"tier1": TIER1, "tier2": TIER2, "tier3": TIER3})

@app.route("/api/status")
def get_status():
    picks         = load_json(PICKS_FILE)
    standings_data = load_json(STANDINGS_FILE)
    final_earnings = load_json(EARNINGS_FILE)

    golfers      = standings_data.get("golfers", {})
    round_info   = standings_data.get("round", "")
    round_status = standings_data.get("status", "")

    use_final     = bool(final_earnings)
    use_projected = bool(golfers) and not use_final

    if use_final:
        earnings_map = final_earnings
    elif use_projected:
        earnings_map = compute_projected_earnings(golfers)
    else:
        earnings_map = {}

    results = build_results(picks, earnings_map, golfers) if picks else []
    updated = datetime.now(PT).strftime("%B %d, %Y at %I:%M %p PT")

    return jsonify({
        "locked":        is_locked(),
        "lock_iso":      LOCK_DT.isoformat(),
        "round":         round_info,
        "round_status":  round_status,
        "updated":       updated,
        "use_final":     use_final,
        "use_projected": use_projected,
        "picks":         picks,
        "golfers":       golfers,
        "results":       results,
        "tournament":    f"{TOURNAMENT_YEAR} {TOURNAMENT_NAME}",
        "venue":         TOURNAMENT_VENUE,
        "tiers":         TIERS,
        "tier_labels":   TIER_LABELS,
        "field":         {"tier1": TIER1, "tier2": TIER2, "tier3": TIER3},
    })

@app.route("/api/picks", methods=["POST"])
def submit_picks():
    if is_locked():
        return jsonify({"error": "Picks are locked — the tournament has started."}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request body."}), 400

    name  = (data.get("name") or "").strip()
    picks = data.get("picks") or []

    if not name:
        return jsonify({"error": "Please enter your name."}), 400
    if len(picks) != 5:
        return jsonify({"error": "Select exactly 5 picks: 2 from Tier 1, 2 from Tier 2, 1 from Tier 3."}), 400

    if len(set(picks)) != 5:
        return jsonify({"error": "All 5 picks must be different golfers."}), 400

    # Normalize and validate each pick against its required tier
    # Slot order: [T1-pick1, T1-pick2, T2-pick1, T2-pick2, T3-pick1]
    resolved = []
    for i, pick in enumerate(picks):
        canonical = FIELD_NORM.get(_norm(pick))
        if not canonical:
            return jsonify({"error": f"Golfer not in field: \"{pick}\". Check spelling."}), 400
        expected_tier = PICK_TIER_SLOTS[i]
        actual_tier   = TIERS.get(canonical)
        if actual_tier != expected_tier:
            return jsonify({"error": f"{canonical} is in {TIER_LABELS[actual_tier]}, not {TIER_LABELS[expected_tier]}."}), 400
        resolved.append(canonical)

    all_picks = load_json(PICKS_FILE)
    all_picks[name] = resolved
    save_json(PICKS_FILE, all_picks)

    return jsonify({"ok": True, "name": name, "picks": resolved})

@app.route("/api/refresh", methods=["POST"])
def force_refresh():
    """Manually trigger a standings scrape."""
    threading.Thread(target=scrape_and_save, daemon=True).start()
    return jsonify({"ok": True, "msg": "Scrape triggered."})

# ── ESPN standings scraper ────────────────────────────────────────────────────
# ESPN's dedicated major endpoints (uso/open) typically 400, so we rely on the
# PGA scoreboard, which lists The Open during its week. Because an opposite-field
# event (e.g. Corales Puntacana) runs the same week, we select the event whose
# name matches The Open rather than blindly taking the first one.
ESPN_URL     = "https://site.api.espn.com/apis/site/v2/sports/golf/open/scoreboard"
ESPN_URL_PGA = "https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard"

def scrape_and_save():
    ctx = ssl.create_default_context()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    data = None
    for url in (ESPN_URL, ESPN_URL_PGA):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                candidate = json.loads(resp.read().decode("utf-8", errors="replace"))
            if candidate.get("events"):
                data = candidate
                print(f"[scraper] Using {url}")
                break
        except Exception as e:
            print(f"[scraper] {url} failed: {e}")
    if data is None:
        print("[scraper] Both ESPN endpoints failed or returned no events.")
        return

    events = data.get("events", [])
    if not events:
        print("[scraper] No events returned.")
        return

    # Pick The Open specifically (an opposite-field event may share the week).
    event = None
    for e in events:
        nm = (e.get("name", "") + " " + e.get("shortName", "")).lower()
        if "the open" in nm or "open championship" in nm:
            event = e
            break
    if event is None:
        event = events[0]

    competitions = event.get("competitions", [])
    if not competitions:
        return
    comp        = competitions[0]
    competitors = comp.get("competitors", [])

    event_status = event.get("status", {})
    state        = event_status.get("type", {}).get("state", "pre")
    period       = event_status.get("period", 0)

    if period == 0 and state == "in":
        for c in competitors[:5]:
            ls = c.get("linescores", [])
            if ls:
                period = max(l.get("period", 0) for l in ls)
                if period > 0:
                    break
        if period == 0:
            period = 1

    if state == "pre":
        round_info, status = "Not Started", "Not Started"
    elif state == "in":
        round_info, status = f"Round {period}", "In Progress"
    else:
        round_info, status = "Final", "Complete"

    raw_list = []
    for c in competitors:
        athlete = c.get("athlete", {})
        name = athlete.get("displayName", "") or athlete.get("fullName", "")
        if not name:
            continue

        score  = c.get("score", "")
        order  = c.get("order", 999)
        c_stat = c.get("status", {})
        c_type = c_stat.get("type", {})
        c_sn   = c_type.get("name", "")

        special = ""
        if   c_sn == "STATUS_CUT": special = "MC"
        elif c_sn == "STATUS_WD":  special = "WD"
        elif c_sn == "STATUS_DQ":  special = "DQ"

        linescores = c.get("linescores", [])
        if not special and period >= 3 and len(linescores) < period:
            special = "MC"

        pos_data   = c_stat.get("position", {})
        status_pos = pos_data.get("displayName", "") if pos_data else ""

        thru = c_stat.get("thru", "")
        if isinstance(thru, int):
            thru = "F" if thru == 18 else ("" if thru == 0 and state != "pre" else str(thru))
        else:
            thru = str(thru) if thru else ""

        today = ""
        if period > 0 and linescores:
            for ls in linescores:
                if ls.get("period") == period and "value" in ls:
                    val = ls["value"]
                    if thru == "F":
                        n = val - PAR
                        today = ("E" if n == 0 else (f"+{n}" if n > 0 else str(n)))
                    break

        raw_list.append({
            "name": name, "score": score, "order": order,
            "special": special, "status_pos": status_pos,
            "today": today, "thru": thru,
        })

    raw_list.sort(key=lambda x: x["order"])

    score_counts = defaultdict(int)
    for p in raw_list:
        if not p["special"]:
            score_counts[p["score"]] += 1

    score_to_rank, rank = {}, 1
    for p in raw_list:
        if p["special"]:
            continue
        sc = p["score"]
        if sc not in score_to_rank:
            score_to_rank[sc] = rank
        rank += 1

    golfers_out = {}
    for p in raw_list:
        if p["special"]:
            position = p["special"]
        elif p["status_pos"]:
            position = p["status_pos"]
        elif state != "pre" and p["score"]:
            r   = score_to_rank.get(p["score"], p["order"])
            cnt = score_counts.get(p["score"], 1)
            position = f"T{r}" if cnt > 1 else str(r)
        else:
            position = ""

        golfers_out[p["name"]] = {
            "position": position,
            "score":    p["score"],
            "today":    p["today"],
            "thru":     p["thru"],
        }

    save_json(STANDINGS_FILE, {
        "tournament": TOURNAMENT_NAME,
        "round":      round_info,
        "status":     status,
        "golfers":    golfers_out,
    })
    print(f"[scraper] {round_info} — {status} ({len(golfers_out)} golfers)")

# ── Background updater ────────────────────────────────────────────────────────
def _background_updater():
    while True:
        now = datetime.now(ET)
        # Only scrape during tournament dates: Jul 16-19 (Birkdale plays on UK time,
        # so scrape around the clock on those dates rather than an ET hour window).
        if date(2026, 7, 16) <= now.date() <= date(2026, 7, 19):
            scrape_and_save()
        time.sleep(600)  # every 10 minutes

_updater_started = False

def start_updater():
    global _updater_started
    if _updater_started:
        return
    _updater_started = True
    # Only kick off an immediate scrape once tournament week starts
    if datetime.now(ET).date() >= date(2026, 7, 16):
        threading.Thread(target=scrape_and_save, daemon=True).start()
    # Periodic updater
    threading.Thread(target=_background_updater, daemon=True).start()
    print("[app] Background standings updater started.")

start_updater()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
