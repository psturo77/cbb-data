"""Live scores for the board (board/scores), run by every odds cycle (every 2-3 min, 7 AM-midnight ET). Nov 22 2026.

usage: python3 live_scores.py <slate dir with d*.json> <out file>
Reads ESPN's scoreboard for yesterday and today (ET) and keeps the games on the board's slates. Each game:
{st: "pre"|"live"|"final", h: ESPN home team id, hs, as (ESPN's home/away scores), d: short status like "2nd 4:12"}.
The page orients each game to its slate's home team (hid) and grades picks as soon as a game is final, so finals reach the
board within minutes instead of waiting for the four daily results runs.
"""
import sys, os, glob, json, urllib.request
import pandas as pd

SL, OUT = sys.argv[1:3]
URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard?dates={}&groups=50&limit=500"
log = lambda *a: print(*a, file=sys.stderr, flush=True)
now = pd.Timestamp.now(tz="UTC"); et = now.tz_convert("America/New_York")
ids = {g["id"] for f in glob.glob(f"{SL}/d*.json") for g in json.load(open(f)).get("games", [])}
games, failed = {}, []
for d in (et - pd.Timedelta(days=1), et):
    js = None
    for a in range(3):
        try:
            req = urllib.request.Request(URL.format(d.strftime("%Y%m%d")), headers={"User-Agent": "Mozilla/5.0 CBB-model"})
            with urllib.request.urlopen(req, timeout=20) as r: js = json.load(r); break
        except Exception as e: err = e
    if js is None: failed.append(f"{d:%m/%d} ({err})"); continue
    for ev in js.get("events", []):
        if ev.get("id") not in ids: continue
        c = (ev.get("competitions") or [{}])[0]; cs = {x.get("homeAway"): x for x in c.get("competitors", [])}
        h, a = cs.get("home"), cs.get("away")
        if not h or not a: continue
        t = ((c.get("status") or ev.get("status") or {}).get("type") or {})
        st = "final" if t.get("completed") else "live" if t.get("state") == "in" else "pre"
        sc = lambda x: int(float(x["score"])) if st != "pre" and str(x.get("score", "")).strip() not in ("", "None") else None
        games[ev["id"]] = dict(st=st, h=int(h["team"]["id"]), hs=sc(h), **{"as": sc(a)}, d=t.get("shortDetail") or "")
json.dump(dict(updated=now.strftime("%Y-%m-%dT%H:%M:%SZ"), n=len(games), games=games), open(OUT, "w"), separators=(",", ":"))
fin = sum(g["st"] == "final" for g in games.values()); live = sum(g["st"] == "live" for g in games.values())
log(f"live_scores: {len(games)} board games yesterday/today - {fin} final, {live} in progress" + (f"; ESPN failed for {', '.join(failed)}" if failed else ""))
if not games and failed: os.remove(OUT); sys.exit(1)
