"""Injury reports for upcoming games, valued in points with the model's own missing-regulars term.

usage: python3 injuries.py <folder of board slate docs> <board_players.json> <out board_injuries.json> [ESPN injuries json]
Reads ESPN's college injury list (site.api.espn.com .../mens-college-basketball/injuries; or a saved copy), matches each
player by ESPN athlete id to board/players, and for every upcoming game (next 7 days) writes
  {updated, n, games: {game_id: {adj, h: [[name, status, gs, counted]], a: [...]}}}
adj = home margin change = coef x (value OUT for home - value OUT for away), counting only regulars listed Out who
did NOT sit the team's last game (those are already in the model's Miss term). Day-To-Day/Questionable: listed, not counted.
"""
import sys, os, re, glob, json, urllib.request
import pandas as pd

SLATES, PLAYERS, OUT = sys.argv[1:4]
SRCF = sys.argv[4] if len(sys.argv) > 4 else None
URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/injuries"
log = lambda *a: print(*a, file=sys.stderr, flush=True)
CAP = 25.0

raw = json.load(open(SRCF)) if SRCF else json.load(urllib.request.urlopen(urllib.request.Request(URL, headers={"User-Agent": "CBB-model"}), timeout=30))
pl = json.load(open(PLAYERS)) if os.path.exists(PLAYERS) else {"teams": {}, "coef": -0.110}
coef = pl.get("coef", -0.110)
byname = {t["n"]: (tid, t) for tid, t in pl["teams"].items()}
now = pd.Timestamp.now(tz="UTC")

reports = {}                                    # team name -> [(athlete id, name, status)]
for t in raw.get("injuries", []):
    tname = t.get("displayName") or (pl["teams"].get(str(t.get("id")), {}) or {}).get("n")
    for i in t.get("injuries", []):
        a = i.get("athlete") or {}; aid = None
        for l in a.get("links", []):
            m = re.search(r"/id/(\d+)", l.get("href", ""))
            if m: aid = int(m.group(1)); break
        reports.setdefault(tname, []).append((aid, a.get("displayName", "?"), i.get("status", "")))

def side(team):
    tid, t = byname.get(team, (None, {"p": []}))
    val = {p[0]: p for p in t["p"]}
    out, tot = [], 0.0
    for aid, name, st in reports.get(team, []):
        p = val.get(aid); gs = p[2] if p else 0.0
        counted = bool(p and st.lower().startswith("out") and p[3] == 1 and p[4] == 0)
        if counted: tot += gs
        out.append([name, st, gs, int(counted)])
    return out, min(tot, CAP)

games = {}
for f in glob.glob(f"{SLATES}/slate_d*.json") + glob.glob(f"{SLATES}/d20*.json"):
    for g in json.load(open(f)).get("games", []):
        st = pd.Timestamp(g["start"])
        if g.get("status") != "pre" or st <= now or st > now + pd.Timedelta(days=7): continue
        h, hv = side(g["home"]); a, av = side(g["away"])
        if h or a: games[g["id"]] = dict(adj=round(coef * (hv - av), 2) + 0.0, h=h, a=a)
doc = dict(updated=now.strftime("%Y-%m-%dT%H:%M:%SZ"), n=sum(len(v) for v in reports.values()), games=games)
json.dump(doc, open(OUT, "w"), separators=(",", ":"), ensure_ascii=False)
log(f"injuries: {doc['n']} players listed by ESPN; {len(games)} upcoming games affected, "
    f"{sum(1 for g in games.values() if g['adj'])} with a line change")
