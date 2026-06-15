"""
US Open Pool 2026 — Flask backend
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
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory
import zoneinfo

app = Flask(__name__)

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
PICKS_FILE     = os.path.join(BASE_DIR, "picks.json")
STANDINGS_FILE = os.path.join(BASE_DIR, "standings.json")
EARNINGS_FILE  = os.path.join(BASE_DIR, "earnings.json")

ET = zoneinfo.ZoneInfo("America/New_York")
PT = zoneinfo.ZoneInfo("America/Los_Angeles")

# Picks lock: Thursday June 18, 2026 at 6:30 AM Eastern (before first tee time)
LOCK_DT = datetime(2026, 6, 18, 6, 30, 0, tzinfo=ET)

# ── Tournament info ───────────────────────────────────────────────────────────
TOURNAMENT_NAME   = "US Open"
TOURNAMENT_YEAR   = "2026"
TOURNAMENT_VENUE  = "Shinnecock Hills Golf Club · Southampton, N.Y."
PAR               = 70   # Shinnecock Hills par 70

# ── Payout table (~$22.5M purse, scaled from standard major structure) ────────
PAYOUT = {
     1: 4050000,  2: 2430000,  3: 1530000,  4: 1080000,  5:  900000,
     6:  810000,  7:  754000,  8:  697000,  9:  652000, 10:  607000,
    11:  562000, 12:  517000, 13:  472000, 14:  427000, 15:  405000,
    16:  382000, 17:  360000, 18:  337000, 19:  315000, 20:  292000,
    21:  270000, 22:  252000, 23:  234000, 24:  217000, 25:  199000,
    26:  180000, 27:  173000, 28:  166500, 29:  159700, 30:  153000,
    31:  146000, 32:  139500, 33:  133000, 34:  127000, 35:  121500,
    36:  115900, 37:  110000, 38:  105700, 39:  101200, 40:   96700,
    41:   92200, 42:   87700, 43:   83200, 44:   78700, 45:   74200,
    46:   69700, 47:   65200, 48:   61600, 49:   58500, 50:   56700,
}
MC_PAYOUT = 0

# ── US Open 2026 official field (156 players, confirmed from usopen.com) ─────
# ── Tier definitions (based on pre-tournament DraftKings/ESPN BET odds) ──────
# Tier 1 — Top 15 favorites   (+400 to +3500)  → pick 2
# Tier 2 — Contenders, 16–80  (+4000 to ~+15000) → pick 2
# Tier 3 — Longshots, 81+     (+20000+)          → pick 1

TIER1 = sorted([
    "Scottie Scheffler", "Rory McIlroy", "Bryson DeChambeau", "Jon Rahm",
    "Ludvig Åberg", "Xander Schauffele", "Tommy Fleetwood", "Collin Morikawa",
    "Cameron Young", "Matt Fitzpatrick", "Viktor Hovland", "Brooks Koepka",
    "Justin Thomas", "Justin Rose", "Tyrrell Hatton",
])

TIER2 = sorted([
    "Hideki Matsuyama", "Wyndham Clark", "J.J. Spaun", "Jordan Spieth",
    "Patrick Cantlay", "Sahith Theegala", "Shane Lowry", "Sam Burns",
    "Robert MacIntyre", "Min Woo Lee", "Sungjae Im", "Keegan Bradley",
    "Adam Scott", "Sepp Straka", "Harris English", "Brian Harman",
    "Daniel Berger", "Aaron Rai", "Max Greyserman", "Jason Day",
    "Akshay Bhatia", "Kurt Kitayama", "Rickie Fowler", "Alex Fitzpatrick",
    "Nick Taylor", "Russell Henley", "Tom Kim", "Corey Conners",
    "Maverick McNealy", "Ben Griffin", "Matt McCarty", "Jacob Bridgeman",
    "Chris Gotterup", "Emiliano Grillo", "Dustin Johnson", "Si Woo Kim",
    "Joaquin Niemann", "Ryan Fox", "Bud Cauley", "J.T. Poston",
    "Andrew Novak", "Andrew Putnam", "Taylor Montgomery", "Davis Thompson",
    "Patrick Reed", "Alejandro Tosti", "Nick Hardy", "Keith Mitchell",
    "Pierceson Coody", "Max McGreevy", "Alex Noren", "Lucas Herbert",
    "Patrick Rodgers", "Harry Hall", "Cole Hammer", "Ben James",
    "Michael Kim", "Brandon Wu", "Gary Woodland", "Carlos Ortiz",
    "Kristoffer Reitan", "David Puig", "Ugo Coussaud", "Neal Shipley",
    "Nicolas Echavarria",
])

TIER3 = sorted([
    "Laurie Canter", "Filippo Celli", "Hamilton Coleman", "Cooper Dossey",
    "Adrien Dumont de Chassart", "Hennie du Plessis", "Ethan Fang",
    "Marek Fleming", "Ryan Gerard", "Vaughn Harber", "Padraig Harrington",
    "Harry Higgs", "Matthew Jordan", "Johnny Keefer", "T.K. Kim",
    "Nathan Kimsey", "Chris Kirk", "Jake Knapp", "Greyson Leach",
    "Bryan Lee", "Graeme McDowell", "James Nicholas", "Niklas Norgaard",
    "Ryuichi Oiwa", "Kaito Onishi", "Jackson Ormond", "John Parry",
    "Jake Peacock", "Chandler Phillips", "Giuseppe Puebla", "Mateo Pulcini",
    "Logan Reilly", "Rocco Repetto Taylor", "Matthew Robles",
    "Adrien Saddier", "Taihei Sato", "Jayden Schaper", "Matti Schmid",
    "Jack Schoenberger", "Manav Shah", "Preston Stout", "Spencer Tibbits",
    "Peter Uihlein", "Jackson Van Paris", "Dylan Wu",
    "Sudarshan Yellamaraju", "Carl Yuan", "Zac Blair", "Michael Brennan",
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
# ESPN uses a dedicated US Open endpoint during the tournament week.
# Falls back to the PGA scoreboard if the USO endpoint returns no events.
ESPN_URL     = "https://site.api.espn.com/apis/site/v2/sports/golf/uso/scoreboard"
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

    event        = events[0]
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
        # Scrape Thu-Sun (weekday 3-6) between 6 AM and 9 PM ET
        if now.weekday() in (3, 4, 5, 6) and 6 <= now.hour < 21:
            scrape_and_save()
        time.sleep(600)  # every 10 minutes

_updater_started = False

def start_updater():
    global _updater_started
    if _updater_started:
        return
    _updater_started = True
    # Immediate first scrape
    threading.Thread(target=scrape_and_save, daemon=True).start()
    # Periodic updater
    threading.Thread(target=_background_updater, daemon=True).start()
    print("[app] Background standings updater started.")

start_updater()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
