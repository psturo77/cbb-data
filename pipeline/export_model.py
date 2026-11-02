"""Export the model's numbers for the next few days to the repo, for the GitHub new-line checker (steam/newlines.py).

usage: python3 export_model.py <dash dir with slate_dYYYY-MM-DD.json> <board/teammap.json or -> <repo dir> [days ahead, default 3]
Writes <repo>/steam/model.json: {updated, teammap, games: {game_id: {start, home, away, neutral, early, pm, pt}}} for games
not yet started from today (US Eastern) through today + days ahead, and copies boardmath.py (next to this script) to
<repo>/steam/boardmath.py so the checker prices bets exactly like the board.
"""
import sys, os, glob, json, shutil
import pandas as pd

DASH, TM, REPO = sys.argv[1:4]
AHEAD = int(sys.argv[4]) if len(sys.argv) > 4 else 3
today = pd.Timestamp.now(tz="America/New_York").normalize().tz_localize(None)
days = {(today + pd.Timedelta(days=i)).strftime("d%Y-%m-%d") for i in range(AHEAD + 1)}
games = {}
for f in sorted(glob.glob(f"{DASH}/slate_d*.json")):
    if os.path.basename(f)[6:-5] not in days: continue
    s = json.load(open(f)); gs = s.get("games", s) if isinstance(s, dict) else s
    for g in (gs.values() if isinstance(gs, dict) else gs):
        if g.get("status") not in (None, "pre") or g.get("pm") is None: continue
        games[str(g["id"])] = {k: g.get(k) for k in ("start", "home", "away", "neutral", "early", "pm", "pt")}
tm = {}
if TM != "-" and os.path.exists(TM):
    d = json.load(open(TM)); tm = d.get("map", d) if isinstance(d, dict) else {}
os.makedirs(f"{REPO}/steam", exist_ok=True)
json.dump(dict(updated=pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"), teammap=tm, games=games),
          open(f"{REPO}/steam/model.json", "w"), separators=(",", ":"))
shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "boardmath.py"), f"{REPO}/steam/boardmath.py")
print(f"export_model: {len(games)} upcoming games ({min(days)[1:]} to {max(days)[1:]}), {len(tm)} team-name matches", file=sys.stderr)
