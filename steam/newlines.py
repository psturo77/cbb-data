"""CBB new-line alerts: the moment DraftKings or FanDuel first posts a spread or total on a game the model has a number
for, price it with the board's own math (steam/boardmath.py, copied daily from the project) and push it to your phone
if it clears the board's bar. Opening lines are where this model's edge lives - they move fast after they post.

Run by .github/workflows/newlines.yml every 30 minutes (Nov-Apr). Standard library only (boardmath's pandas use is stood in for).
Inputs (committed daily by the data refresh task):
  steam/model.json    {updated, teammap, games: {espn_game_id: {start, home, away, neutral, early, pm, pt}}} for the next few days
  steam/boardmath.py  the board's math
Environment: ODDS_API_KEY, NTFY_TOPIC (GitHub secrets); BOOKS (default: steam/books.txt, shared with the steam checker); MIN_EV (default 0.03);
  AHEAD_H (only games tipping within this many hours, default 40); MIN_CREDITS (default 1500); DRY_RUN=1;
  ODDS_FILE / EVENTS_FILE = saved json to test without the API; STATE_FILE (default .newlines_state.json).
Cost: the events list is free; the odds call (Pinnacle + your books, spreads + totals = 2 credits) is made only when some
game in the window still has a book/market we haven't seen posted. First run with no saved state: records what is
already posted without alerting (so nothing old is sent as "new").
Logs every alert to steam/newlines.csv (graded later against the closing line).
"""
import os, sys, re, csv, json, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
try:
    import pandas  # noqa: F401  (boardmath uses only pandas.Timestamp; a tiny stand-in keeps this job dependency-free)
except ImportError:
    import types
    _pd = types.ModuleType("pandas"); _pd.Timestamp = lambda s: datetime.fromisoformat(str(s).replace("Z", "+00:00")); sys.modules["pandas"] = _pd
from boardmath import bets_for, min_for, timing

API = "https://api.the-odds-api.com/v4/sports/basketball_ncaab"
KEY, TOPIC = os.environ.get("ODDS_API_KEY", ""), os.environ.get("NTFY_TOPIC", "")
_bk = os.environ.get("BOOKS") or open(os.path.join(HERE, "books.txt")).read()      # same list as the steam checker
BOOKS = [b.strip() for b in _bk.replace("\n", ",").split(",") if b.strip() and not b.strip().startswith("#") and b.strip() != "pinnacle"][:9]
MIN_EV = float(os.environ.get("MIN_EV", "0.03")); AHEAD_H = float(os.environ.get("AHEAD_H", "40"))
MIN_CREDITS = int(os.environ.get("MIN_CREDITS", "1500")); DRY = os.environ.get("DRY_RUN") == "1"
STATE = os.environ.get("STATE_FILE", ".newlines_state.json"); LOG = os.path.join(HERE, "newlines.csv")
LOGF = ["alerted", "game_id", "event_id", "start", "home", "away", "market", "side", "line", "price", "book", "ev", "p", "units",
        "model", "pin_line", "move_expected"]
BOARD = "https://claude.ai/artifact/SgPtmedZ2wVPiYHaJoRNZh"
now = datetime.now(timezone.utc)
ts = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))

def norm(s):
    s = str(s or "").lower().replace("&", "and"); s = re.sub(r"[.'’()]", "", s); s = re.sub(r"\bst\b", "state", s)
    return re.sub(r"\s+", " ", s).strip()

def get(path, **q):
    url = f"{API}/{path}?" + urllib.parse.urlencode(dict(apiKey=KEY, **q))
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "cbb-newlines"}), timeout=30) as r:
        return json.load(r), r.headers.get("x-requests-remaining")

def notify(title, body):
    if DRY or not TOPIC: print(f"[notify] {title}\n{body}"); return
    req = urllib.request.Request(f"https://ntfy.sh/{TOPIC}", data=body.encode(), method="POST",
                                 headers={"Title": title, "Priority": "high", "Tags": "basketball,new", "Click": BOARD})
    urllib.request.urlopen(req, timeout=20).read()

M = json.load(open(os.path.join(HERE, "model.json")))
tm = M.get("teammap") or {}
team = lambda n: norm(tm.get(n, n))
pairs = {}
for gid, g in M["games"].items():
    g["id"] = gid; pairs[(norm(g["home"]), norm(g["away"]))] = (gid, False); pairs[(norm(g["away"]), norm(g["home"]))] = (gid, True)

seed = not os.path.exists(STATE)
state = {} if seed else json.load(open(STATE))
state = {k: v for k, v in state.items() if ts(v) > now - timedelta(days=4)}
def save(): json.dump(state, open(STATE, "w"))

# ---- 1. games in the window that the model has a number for (events list is free)
t0, t1 = now.strftime("%Y-%m-%dT%H:%M:%SZ"), (now + timedelta(hours=AHEAD_H)).strftime("%Y-%m-%dT%H:%M:%SZ")
if os.environ.get("EVENTS_FILE"): events, left = json.load(open(os.environ["EVENTS_FILE"])), None
else:
    if not KEY: sys.exit("ODDS_API_KEY is not set")
    events, left = get("events", commenceTimeFrom=t0, commenceTimeTo=t1)
ours = {}
for ev in events:
    m = pairs.get((team(ev["home_team"]), team(ev["away_team"])))
    if m: ours[ev["id"]] = m
todo = [e for e in ours if any(f"{e}|{b}|{k}" not in state for b in BOOKS for k in ("spreads", "totals"))]
if not todo:
    print(f"{len(events)} events, {len(ours)} with a model number; every line already seen - no odds pulled"); save(); sys.exit(0)
if left is not None and int(float(left)) < MIN_CREDITS:
    print(f"only {left} credits left - skipped"); save(); sys.exit(0)

# ---- 2. one odds pull: Pinnacle + your books, spreads and totals (2 credits)
if os.environ.get("ODDS_FILE"): odds, left = json.load(open(os.environ["ODDS_FILE"])), "test"
else:
    try:
        odds, left = get("odds", bookmakers=",".join(["pinnacle"] + BOOKS), markets="spreads,totals", oddsFormat="american",
                         commenceTimeFrom=t0, commenceTimeTo=t1)
    except urllib.error.HTTPError as e:
        sys.exit(f"odds call failed: HTTP {e.code}")
    if os.environ.get("SAVE_ODDS"): json.dump(odds, open(os.environ["SAVE_ODDS"], "w"))

# ---- 3. first sighting of a book's line -> price it like the board does at that moment
fmt = lambda x: "PK" if abs(x) < 0.05 else (f"+{x:g}" if x > 0 else f"{x:g}")
pr = lambda p: f"+{p}" if p > 0 else f"{p}"
log = list(csv.DictReader(open(LOG))) if os.path.exists(LOG) else []
hits = []
for ev in odds:
    if ev["id"] not in ours or ts(ev["commence_time"]) <= now: continue
    gid, flip = ours[ev["id"]]; g = M["games"][gid]
    bm = {b["key"]: b for b in ev.get("bookmakers", [])}
    def mk(book, key):
        b = bm.get(book); m = next((x for x in (b or {}).get("markets", []) if x["key"] == key), None)
        return {o["name"]: o for o in m["outcomes"]} if m else None
    # the model's home team = Odds API home unless flipped (neutral sites often list the teams the other way round)
    hname = ev["away_team"] if flip else ev["home_team"]; aname = ev["home_team"] if flip else ev["away_team"]
    pin_sp, pin_t = mk("pinnacle", "spreads"), mk("pinnacle", "totals")
    for book in BOOKS:
        for key in ("spreads", "totals"):
            k = f"{ev['id']}|{book}|{key}"
            o_ = mk(book, key)
            if not o_ or k in state: continue
            state[k] = now.isoformat()
            if seed: continue
            if key == "spreads":
                h, a = o_.get(hname), o_.get(aname)
                if not h or not a or h.get("point") is None: continue
                o = dict(sp=h["point"], spp=h["price"], spa=a["price"])
                pin = f"Pinnacle {pin_sp[hname]['point']:+g}" if pin_sp and hname in pin_sp and pin_sp[hname].get("point") is not None else "Pinnacle not up yet"
            else:
                ov, un = o_.get("Over"), o_.get("Under")
                if not ov or not un or ov.get("point") is None: continue
                o = dict(tot=ov["point"], op=ov["price"], up=un["price"])
                pin = f"Pinnacle {pin_t['Over']['point']:g}" if pin_t and pin_t.get("Over") and pin_t["Over"].get("point") is not None else "Pinnacle not up yet"
            o.update(seen=now.strftime("%Y-%m-%dT%H:%M:%SZ"), book=bm[book]["title"]); o["open"] = {"sp": o.get("sp"), "tot": o.get("tot")}
            bets = [b for b in bets_for(g, o) if b["market"] == ("spread" if key == "spreads" else "total")]
            b = next((b for b in bets if b["ev"] >= min_for(g, b, MIN_EV)), None)
            if not b: continue
            tmv = timing(g, b, o)
            nm = (g["home"] if b["side"] == "home" else g["away"]) if b["market"] == "spread" else b["side"].title()
            txt = f"{nm} {fmt(b['line']) if b['market'] == 'spread' else format(b['line'], 'g')} {pr(b['price'])}"
            mdl = f"model {g['home']} {fmt(round(-g['pm'], 1))}" if b["market"] == "spread" else f"model total {g['pt']:.1f}"
            tip = ts(g["start"]).astimezone(timezone(timedelta(hours=-5)))
            hits.append((b["ev"], f"{txt} at {o['book']} - edge {b['ev']:+.1%}, stake {b['units']:.1f}u"
                                  + (f", line expected to move {tmv:.1f} against it" if tmv and tmv >= 1 else "")
                                  + f" - {mdl}; {pin} - {g['away']} at {g['home']}, {tip.strftime('%a %-I:%M %p')} ET"))
            log.append(dict(alerted=now.strftime("%Y-%m-%dT%H:%M:%SZ"), game_id=gid, event_id=ev["id"], start=g["start"], home=g["home"],
                            away=g["away"], market=b["market"], side=b["side"], line=b["line"], price=b["price"], book=o["book"],
                            ev=round(b["ev"], 4), p=round(b["p"], 4), units=round(b["units"], 2), model=g["pm"] if b["market"] == "spread" else g["pt"],
                            pin_line=pin, move_expected=round(tmv, 2) if tmv is not None else ""))
if hits:
    hits.sort(reverse=True)
    notify(f"CBB new line{'s' if len(hits) > 1 else ''}: {len(hits)} worth a look", "\n".join(h for _, h in hits[:8]))
    with open(LOG, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOGF); w.writeheader(); [w.writerow({k: r.get(k, "") for k in LOGF}) for r in log]
save()
print(f"{len(odds)} games pulled, {len(todo)} with unseen lines, {len(hits)} alert(s){' (first run: recorded only)' if seed else ''}; credits left {left}")
