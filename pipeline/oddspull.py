"""Pull odds from every US sportsbook + Pinnacle (The Odds API) and build the board's odds document.

usage: python3 oddspull.py <api key | path to an odds.json already downloaded> <db dir> <out board_odds.json>
<db dir> is where ArtifactData saved the board: slate/d*.json, board/teams.json, board/teammap.json, board/prefs.json,
board/odds.json (any of the last three may be missing). Same logic as the board's "Update odds" button (cbb-board.html):
middle-of-books consensus, best-price frontier per side among YOUR books (board/prefs), first-seen openers, earlier
games kept 14 days, plus: Pinnacle (when it has the game) is the market reference for the line and no-vig price.
Costs 3 credits per pull (spreads only x regions us, us2, eu; totals and moneylines dropped Nov 10 2026).
"""
import sys, os, re, json, glob, urllib.request, urllib.error
import pandas as pd

SRC, DB, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
log = lambda *a: print(*a, file=sys.stderr, flush=True)
URL = ("https://api.the-odds-api.com/v4/sports/basketball_ncaab/odds?regions=us,us2,eu&markets=spreads"
       "&oddsFormat=american&apiKey={}")
load = lambda p, d=None: json.load(open(p)) if os.path.exists(p) else d

if os.path.exists(SRC):
    raw = json.load(open(SRC)); remaining = "?"
else:
    try:
        with urllib.request.urlopen(urllib.request.Request(URL.format(SRC), headers={"User-Agent": "CBB-model"}), timeout=60) as r:
            raw = json.load(r); remaining = r.headers.get("x-requests-remaining", "?")
    except urllib.error.HTTPError as e:
        sys.exit(f"Odds API error HTTP {e.code}: {e.read()[:200]!r}")
log(f"{len(raw)} events from The Odds API (credits left: {remaining})")

# ---- board state
games = {}
for f in glob.glob(f"{DB}/slate/d*.json") + glob.glob(f"{DB}/slate_d*.json"):
    for g in json.load(open(f)).get("games", []): games[g["id"]] = g
teams = [t["t"] for t in (load(f"{DB}/board/teams.json", {}) or {}).get("teams", [])]
teammap = (load(f"{DB}/board/teammap.json", {}) or {}).get("map", {})
prefs = load(f"{DB}/board/prefs.json", {}) or {}
prev_doc = load(f"{DB}/board/odds.json", {}) or {}
prev = prev_doc.get("games", {})
allow = set(prefs["books"]) if isinstance(prefs.get("books"), list) and prefs["books"] else None

def norm(s):
    s = str(s or "").lower().replace("&", "and"); s = re.sub(r"[.'’()]", "", s); s = re.sub(r"\bst\b", "state", s)
    return re.sub(r"\s+", " ", s).strip()
tset = set(teams); nidx = {norm(t): t for t in teams}
def team_for(n): return teammap.get(n) or (n if n in tset else nidx.get(norm(n)))

et_day = lambda iso: pd.Timestamp(iso).tz_convert("America/New_York").strftime("%Y-%m-%d")
by_pair = {}
for g in games.values(): by_pair.setdefault(f"{g['home']}|{g['away']}", []).append(g)
def near(lst, day):
    d0 = pd.Timestamp(day)
    for x in lst or []:
        if abs((pd.Timestamp(et_day(x["start"])) - d0).days) <= 1: return x
    return None

payout = lambda p: p / 100 if p > 0 else 100 / -p
implied = lambda p: -p / (-p + 100) if p < 0 else 100 / (p + 100)
def novig(h, a): x, y = implied(h), implied(a); return x / (x + y)
def mid(a):
    s = sorted(x for x in a if x is not None)
    return s[(len(s) - 1) // 2] if s else None
def frontier(opts, d):          # best line first (d=+1 higher is better), keep only prices nobody beats, max 3
    s = sorted(opts, key=lambda o: (-d * o[0], -payout(o[1]))); out, bp = [], -1
    for o in s:
        if payout(o[1]) > bp + 1e-9: out.append(o); bp = payout(o[1])
    return out[:3]
def best(a): return max(a, key=lambda x: payout(x[0])) if a else None

books, seen_books, out_games, fresh, unmatched = [], {}, {}, set(), set()
def bidx(t):
    if t not in books: books.append(t)
    return books.index(t)
now = pd.Timestamp.now(tz="UTC"); matched = live = 0
for ev in raw:
    for bm in ev.get("bookmakers", []): seen_books[bm["key"]] = bm.get("title", bm["key"])
    h, a = team_for(ev["home_team"]), team_for(ev["away_team"])
    if not h: unmatched.add(ev["home_team"])
    if not a: unmatched.add(ev["away_team"])
    if not h or not a: continue
    if pd.Timestamp(ev["commence_time"]) <= now: live += 1; continue
    day = et_day(ev["commence_time"]); flip = False
    g = near(by_pair.get(f"{h}|{a}"), day)
    if not g:
        g = near(by_pair.get(f"{a}|{h}"), day); flip = g is not None
    if not g: continue
    homeO, awayO = (ev["away_team"], ev["home_team"]) if flip else (ev["home_team"], ev["away_team"])
    SH, SA, OV, UN, MH, MA, NV = [], [], [], [], [], [], []
    pin = {}; nb = 0; upd = None
    for bm in ev.get("bookmakers", []):
        mk = {m["key"]: {o["name"]: o for o in m.get("outcomes", [])} for m in bm.get("markets", [])}
        sp, tt, ml = mk.get("spreads", {}), mk.get("totals", {}), mk.get("h2h", {})
        if bm["key"] == "pinnacle":           # the sharp reference, whether or not it's one of your books
            if homeO in sp and sp[homeO].get("point") is not None: pin["sp"] = sp[homeO]["point"]
            if "Over" in tt and tt["Over"].get("point") is not None: pin["tot"] = tt["Over"]["point"]
            if homeO in ml and awayO in ml: pin["nvh"] = round(novig(ml[homeO]["price"], ml[awayO]["price"]), 4)
        if allow and bm["key"] not in allow: continue
        bi = bidx(bm.get("title", bm["key"])); nb += 1
        upd = max(upd or bm.get("last_update", ""), bm.get("last_update", ""))
        if homeO in sp and sp[homeO].get("point") is not None: SH.append([sp[homeO]["point"], sp[homeO]["price"], bi])
        if awayO in sp and sp[awayO].get("point") is not None: SA.append([sp[awayO]["point"], sp[awayO]["price"], bi])
        if "Over" in tt and tt["Over"].get("point") is not None: OV.append([tt["Over"]["point"], tt["Over"]["price"], bi])
        if "Under" in tt and tt["Under"].get("point") is not None: UN.append([tt["Under"]["point"], tt["Under"]["price"], bi])
        if homeO in ml: MH.append([ml[homeO]["price"], bi])
        if awayO in ml: MA.append([ml[awayO]["price"], bi])
        if homeO in ml and awayO in ml: NV.append(novig(ml[homeO]["price"], ml[awayO]["price"]))
    if not nb: continue
    seen = upd or now.strftime("%Y-%m-%dT%H:%M:%SZ")
    m_sp, m_tot, m_nv = mid([x[0] for x in SH]), mid([x[0] for x in OV]), mid(NV)
    rec = dict(start=g["start"], nb=nb, seen=seen,
               sp=pin.get("sp", m_sp), tot=pin.get("tot", m_tot), nvh=pin.get("nvh", round(m_nv, 4) if m_nv is not None else None),
               spp=mid([x[1] for x in SH]), op=mid([x[1] for x in OV]), up=mid([x[1] for x in UN]),
               mlh=best(MH)[0] if MH else None, mla=best(MA)[0] if MA else None,
               mid=dict(sp=m_sp, tot=m_tot), anchor="Pinnacle" if "sp" in pin else None,
               opt=dict(sh=frontier(SH, 1), sa=frontier(SA, 1), ov=frontier(OV, -1), un=frontier(UN, 1), mh=best(MH), ma=best(MA)),
               q=dict(sh=SH, sa=SA, ov=OV, un=UN, mh=MH, ma=MA))   # every book's quote, for the board's bet detail (bet math uses opt)
    p0 = prev.get(g["id"])
    rec["open"] = p0["open"] if p0 and p0.get("open") else dict(sp=rec["sp"], tot=rec["tot"], nvh=rec["nvh"], seen=seen)
    out_games[g["id"]] = rec; fresh.add(g["id"]); matched += 1
cut = now - pd.Timedelta(days=14)
for gid, r in prev.items():
    if gid not in out_games and r.get("seen") and pd.Timestamp(r["seen"]) > cut:
        c = dict(r); c.pop("opt", None); c.pop("q", None); out_games[gid] = c
doc = dict(loaded=now.strftime("%Y-%m-%dT%H:%M:%SZ"), file="cloud pull", books=books,
           book=books[0] if len(books) == 1 else f"{len(books)} books",
           allBooks=sorted([dict(k=k, t=t) for k, t in seen_books.items()], key=lambda x: x["t"]),
           games=out_games, unmatched=sorted(unmatched), matched=matched, source="cloud")
size = lambda: len(json.dumps(doc))
far = sorted(fresh, key=lambda i: out_games[i]["start"], reverse=True)
for gid in far:
    if size() <= 230000: break
    out_games[gid].pop("q", None)
for gid in far:
    if size() <= 230000: break
    out_games[gid].pop("opt", None)
json.dump(doc, open(OUT, "w"), separators=(",", ":"))
pins = sum(1 for i in fresh if out_games[i].get("anchor"))
log(f"matched {matched} of {len(raw) - live} upcoming games from {len(books)} books ({pins} with Pinnacle); "
    f"{len(unmatched)} unmatched names; {size() // 1024} KB")
print(remaining)
