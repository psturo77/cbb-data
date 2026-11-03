"""Props forward test: record today's posted player-points props next to the model's projections (no betting yet).

usage: ODDS_API_KEY=... python3 props_snap.py <board_props.json> <board/teammap.json> <folder of existing prop docs> <out folder> [max events, default 60]
1. The Odds API /events (free) -> today's games (US Eastern) still to tip; matched to board_props games by team names.
2. For each matched game: /events/{id}/odds?regions=us&markets=player_points (about 1 credit each).
3. For every player with a posted line who is in the projections, writes <out>/<game_id>-<athlete_id>.json (first time
   seen only): the consensus (middle) line, best over / under price and book, number of books, the projection, his
   average, and lean = projection - line. Graded later by grade_props.py.
Prints a one-line summary to stderr (events checked, lines found, matched, credits left). Never prints the key.
"""
import sys, os, re, glob, json, urllib.request, urllib.parse, urllib.error
import pandas as pd

PROPS, TEAMMAP, HAVE, OUT = sys.argv[1:5]
MAXEV = int(sys.argv[5]) if len(sys.argv) > 5 else 60
KEY = os.environ.get("ODDS_API_KEY", "")
API = "https://api.the-odds-api.com/v4/sports/basketball_ncaab"
log = lambda *a: print(*a, file=sys.stderr, flush=True)
os.makedirs(OUT, exist_ok=True)
proj = json.load(open(PROPS)).get("games", {})
teammap = (json.load(open(TEAMMAP)) if os.path.exists(TEAMMAP) else {}).get("map", {})
have = {os.path.basename(f)[:-5] for f in glob.glob(f"{HAVE}/*.json")}
now = pd.Timestamp.now(tz="UTC"); today = now.tz_convert("America/New_York").strftime("%Y-%m-%d")

def norm(s):
    s = str(s or "").lower().replace("&", "and"); s = re.sub(r"[.'’()]", "", s); s = re.sub(r"\bst\b", "state", s)
    return re.sub(r"\s+", " ", s).strip()
def pnorm(s):          # player names: drop suffixes and punctuation
    s = re.sub(r"[.'’,-]", " ", str(s or "").lower()); s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()
team = lambda n: norm(teammap.get(n, n))
by_pair = {}
for gid, g in proj.items(): by_pair[(norm(g["home"]), norm(g["away"]))] = gid

def get(path, **q):
    if os.environ.get("PROPS_FAKE"):                       # offline test: canned responses in a folder
        f = "events.json" if path == "events" else path.split("/")[1] + ".json"
        return json.load(open(os.path.join(os.environ["PROPS_FAKE"], f))), "test"
    url = f"{API}/{path}?" + urllib.parse.urlencode(dict(apiKey=KEY, **q))
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "cbb-props"}), timeout=30) as r:
        return json.load(r), r.headers.get("x-requests-remaining")

if not KEY and not os.environ.get("PROPS_FAKE"): sys.exit("ODDS_API_KEY is not set")
events, left = get("events", commenceTimeFrom=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   commenceTimeTo=(now + pd.Timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"))
todo = []
for ev in events:
    if pd.Timestamp(ev["commence_time"]).tz_convert("America/New_York").strftime("%Y-%m-%d") != today: continue
    h, a = team(ev["home_team"]), team(ev["away_team"])
    gid = by_pair.get((h, a)) or by_pair.get((a, h))
    if gid: todo.append((ev, gid))
payout = lambda p: p / 100 if p > 0 else 100 / -p
n_ev = n_lines = n_new = n_unmatched = 0
for ev, gid in todo[:MAXEV]:
    try:
        d, left = get(f"events/{ev['id']}/odds", regions="us", markets="player_points", oddsFormat="american")
    except urllib.error.HTTPError as e:
        log(f"props: HTTP {e.code} for one event - skipped"); continue
    n_ev += 1
    lines = {}
    for bm in d.get("bookmakers", []):
        for m in bm.get("markets", []):
            if m.get("key") != "player_points": continue
            for o in m.get("outcomes", []):
                nm, pt = o.get("description"), o.get("point")
                if nm is None or pt is None: continue
                L = lines.setdefault(pnorm(nm), dict(name=nm, pts=[], over=None, under=None, books=set()))
                L["books"].add(bm["key"])
                if o["name"] == "Over":
                    L["pts"].append(pt)
                    if L["over"] is None or (pt, -payout(o["price"])) < (L["over"][0], -payout(L["over"][1])): L["over"] = [pt, o["price"], bm.get("title", bm["key"])]
                elif o["name"] == "Under":
                    if L["under"] is None or (-pt, -payout(o["price"])) < (-L["under"][0], -payout(L["under"][1])): L["under"] = [pt, o["price"], bm.get("title", bm["key"])]
    g = proj[gid]; players = {pnorm(p[1]): p for p in g["players"]}
    for k, L in lines.items():
        n_lines += 1
        p = players.get(k)
        if not p: n_unmatched += 1; continue
        pid = f"{gid}-{p[0]}"
        if pid in have or not L["pts"]: continue
        s = sorted(L["pts"]); line = s[(len(s) - 1) // 2]
        doc = dict(game_id=gid, start=g["start"], rec_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"), athlete_id=p[0], name=p[1], team_side=p[2],
                   line=line, over=L["over"], under=L["under"], n_books=len(L["books"]), proj=p[3], avg=p[4], exp_min=p[5], games=p[6],
                   lean=round(p[3] - line, 2), event_id=ev["id"])
        json.dump(doc, open(f"{OUT}/{pid}.json", "w")); have.add(pid); n_new += 1
log(f"props: {len(todo)} of today's games matched, {n_ev} checked; {n_lines} player lines posted, {n_new} new recorded, "
    f"{n_unmatched} names not in projections; credits left {left}")
