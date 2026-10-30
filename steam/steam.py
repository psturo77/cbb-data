"""CBB steam checker: every few minutes near tip-off, find prices at your sportsbooks that are better than
Pinnacle's no-vig line (a book slow to follow a sharp move, or simply off-market) and push them to your phone.

Run by .github/workflows/steam.yml (GitHub Actions). Standard library only.
Environment:
  ODDS_API_KEY   The Odds API key (GitHub secret)
  NTFY_TOPIC     your private ntfy.sh topic name (GitHub secret); alerts go to https://ntfy.sh/<topic>
  BOOKS          sportsbook keys to check, comma separated (default: steam/books.txt)
  MIN_EV         minimum edge vs Pinnacle's no-vig price (default 0.02 = 2%)
  WINDOW_H       only games starting within this many hours (default 4)
  MIN_CREDITS    stop pulling below this many API credits left (default 1500)
  DRY_RUN=1      print instead of sending; ODDS_FILE=<saved odds json> to test without the API
Cost: 1 free events call; odds only if a game tips within WINDOW_H: markets spreads,totals x 1 bookmaker group
(Pinnacle + up to 9 books) = 2 credits.
"""
import os, sys, json, time, math, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta
from statistics import NormalDist

API = "https://api.the-odds-api.com/v4/sports/basketball_ncaab"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("STATE_FILE", ".steam_state.json")
KEY = os.environ.get("ODDS_API_KEY", "")
TOPIC = os.environ.get("NTFY_TOPIC", "")
MIN_EV = float(os.environ.get("MIN_EV", "0.02"))
WINDOW_H = float(os.environ.get("WINDOW_H", "4"))
MIN_CREDITS = int(os.environ.get("MIN_CREDITS", "1500"))
DRY = os.environ.get("DRY_RUN") == "1"
BOARD = "https://claude.ai/artifact/SgPtmedZ2wVPiYHaJoRNZh"
SD = {"spreads": 11.2, "totals": 16.7}     # outcome spread around the line (the model's sigmas), for converting points to chance
MAX_GAP = 3.0                               # ignore a book line more than 3 pts from Pinnacle's (alt line or bad data)
N = NormalDist()
now = datetime.now(timezone.utc)

def books():
    b = os.environ.get("BOOKS") or open(os.path.join(HERE, "books.txt")).read()
    ks = [x.strip() for x in b.replace("\n", ",").split(",") if x.strip() and not x.strip().startswith("#")]
    return [k for k in ks if k != "pinnacle"][:9]

def get(path, **q):
    url = f"{API}/{path}?" + urllib.parse.urlencode(dict(apiKey=KEY, **q))
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "cbb-steam"}), timeout=30) as r:
        return json.load(r), r.headers.get("x-requests-remaining")

def notify(title, body, prio="high"):
    if DRY or not TOPIC:
        print(f"[notify] {title}\n{body}"); return
    req = urllib.request.Request(f"https://ntfy.sh/{TOPIC}", data=body.encode(), method="POST",
                                 headers={"Title": title, "Priority": prio, "Tags": "basketball", "Click": BOARD})
    urllib.request.urlopen(req, timeout=20).read()

payout = lambda p: p / 100 if p > 0 else 100 / -p
implied = lambda p: -p / (-p + 100) if p < 0 else 100 / (p + 100)
fmt = lambda x: "PK" if abs(x) < 0.05 else (f"+{x:g}" if x > 0 else f"{x:g}")
price = lambda p: f"+{p}" if p > 0 else f"{p}"
ts = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))

state = json.load(open(STATE)) if os.path.exists(STATE) else {}
state = {k: v for k, v in state.items() if ts(v) > now - timedelta(days=2)}

def save():
    json.dump(state, open(STATE, "w"))

# ---- 1. anything tipping soon? (events list is free)
if os.environ.get("ODDS_FILE"):
    odds, left = json.load(open(os.environ["ODDS_FILE"])), "test"
else:
    if not KEY: sys.exit("ODDS_API_KEY is not set")
    t0, t1 = now.strftime("%Y-%m-%dT%H:%M:%SZ"), (now + timedelta(hours=WINDOW_H)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        events, left = get("events", commenceTimeFrom=t0, commenceTimeTo=t1)
    except urllib.error.HTTPError as e:
        sys.exit(f"events call failed: HTTP {e.code}")
    if not events:
        print(f"no games tip in the next {WINDOW_H:g}h - no odds pulled"); save(); sys.exit(0)
    if left is not None and int(float(left)) < MIN_CREDITS:
        if "low_credits" not in state:
            notify("CBB steam checker paused", f"Only {left} Odds API credits left - fast checks stopped until next month's credits.", "default")
            state["low_credits"] = now.isoformat()
        save(); sys.exit(0)
    # ---- 2. one odds pull: Pinnacle + your books, spreads and totals (2 credits)
    try:
        odds, left = get("odds", bookmakers=",".join(["pinnacle"] + books()), markets="spreads,totals",
                         oddsFormat="american", commenceTimeFrom=t0, commenceTimeTo=t1)
    except urllib.error.HTTPError as e:
        sys.exit(f"odds call failed: HTTP {e.code}")

# ---- 3. every price at your books vs Pinnacle's no-vig chance at the same line
hits = []
for ev in odds:
    bm = {b["key"]: b for b in ev.get("bookmakers", [])}
    pin = bm.get("pinnacle")
    if not pin or ts(ev["commence_time"]) <= now: continue
    if ts(pin["last_update"]) < now - timedelta(minutes=60): continue          # Pinnacle quote too old to trust
    pmk = {m["key"]: {o["name"]: o for o in m["outcomes"]} for m in pin.get("markets", [])}
    for key, b in bm.items():
        if key == "pinnacle": continue
        for m in b.get("markets", []):
            P = pmk.get(m["key"])
            if not P or len(P) != 2 or m["key"] not in SD: continue
            names = list(P)
            nv = {n: implied(P[n]["price"]) for n in names}; s = sum(nv.values()); nv = {n: v / s for n, v in nv.items()}
            for o in m["outcomes"]:
                po = P.get(o["name"])
                if not po or o.get("point") is None or po.get("point") is None: continue
                gap = o["point"] - po["point"]
                if abs(gap) > MAX_GAP: continue
                # more points is better for a spread side and an Under; a lower total is better for an Over
                better = -gap if o["name"] == "Over" else gap
                p = N.cdf(N.inv_cdf(min(max(nv[o["name"]], 1e-4), 1 - 1e-4)) + better / SD[m["key"]])
                ev_ = p * payout(o["price"]) - (1 - p)
                if ev_ < MIN_EV: continue
                side = o["name"] if m["key"] == "totals" else o["name"]
                line = (f"{side} {o['point']:g}" if m["key"] == "totals" else f"{side} {fmt(o['point'])}")
                pin_line = (f"{po['point']:g} {price(po['price'])}" if m["key"] == "totals" else f"{fmt(po['point'])} {price(po['price'])}")
                k = f"{ev['id']}|{m['key']}|{o['name']}|{key}|{o['point']}|{o['price']}"
                if k in state: continue
                state[k] = now.isoformat()
                tip = ts(ev["commence_time"]).astimezone(timezone(timedelta(hours=-5)))   # US Eastern (standard time, Nov-Mar)
                hits.append((ev_, f"{line} {price(o['price'])} at {b['title']} - edge {ev_:+.1%} vs Pinnacle ({pin_line})"
                                   f" - {ev['away_team']} at {ev['home_team']}, {tip.strftime('%-I:%M %p')} ET"))
hits.sort(reverse=True)
if hits:
    notify(f"CBB: {len(hits)} price{'s' if len(hits) > 1 else ''} beating Pinnacle", "\n".join(h for _, h in hits[:8]))
save()
print(f"{len(odds)} games checked, {len(hits)} new alert(s); credits left {left}")
