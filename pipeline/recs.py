"""The board's recommendation ledger: every bet the board recommends, recorded the first time it qualifies.

usage: python3 recs.py <folder of board slate docs> <board_meta.json> <board_espn.json> <board_odds.json> <folder of existing rec docs> <out folder> [--inj=board_injuries.json]
Uses the board's own rules (boardmath.py): the board's choice of odds (all books with Pinnacle as the market line, unless
ESPN's line is more than 6 hours newer), up to two bets per game (best side bet + best total), 3% minimum edge, 6% for a
road team, injuries applied. For each bet not already in the ledger (doc id <game_id>-s / <game_id>-t) it writes
<out folder>/<id>.json - the run then saves them to the board's "rec" collection. Prints one line per new rec.
Graded later by grade_recs.py (daily task) into the repo's recs.csv.
"""
import sys, os, glob, json
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from boardmath import calibrate_all, bets_for, top_bets, min_for, timing, payout, is_prime, caution

flags = {a.split("=")[0]: (a.split("=", 1)[1] if "=" in a else "") for a in sys.argv[1:] if a.startswith("--")}
SLATES, META, ESPN, ODDS, HAVE, OUT = [a for a in sys.argv[1:] if not a.startswith("--")][:6]
MIN = 0.03
INJ = json.load(open(flags["--inj"])).get("games", {}) if flags.get("--inj") and os.path.exists(flags["--inj"]) else {}
meta = json.load(open(META)); sdm, sdt = meta.get("sigma_margin", 11.2), meta.get("sigma_total", 16.7)
espn = json.load(open(ESPN)).get("games", {}) if os.path.exists(ESPN) else {}
od = json.load(open(ODDS)) if os.path.exists(ODDS) else {}
ug, books = od.get("games", {}), od.get("books", [])
have = {os.path.basename(f)[:-5] for f in glob.glob(f"{HAVE}/*.json")}
games = {}
for f in glob.glob(f"{SLATES}/slate_d*.json") + glob.glob(f"{SLATES}/d20*.json"):
    for g in json.load(open(f)).get("games", []): games[g["id"]] = g
games, CAL = calibrate_all(games, ug)          # early-season market calibration (same as the board)
now = pd.Timestamp.now(tz="UTC")
os.makedirs(OUT, exist_ok=True)

def rec(gid):                      # same rule as the board's oddsRec()
    u, e = ug.get(gid), espn.get(gid)
    if not e: return u, bool(u)
    if u and pd.Timestamp(u["seen"]) >= pd.Timestamp(e["seen"]) - pd.Timedelta(hours=6):
        r = dict(u)
        if (e.get("open") or {}).get("sp") is not None: r["open"] = e["open"]
        if e.get("bpi") is not None: r["bpi"] = e["bpi"]
        return r, True
    return e, False

n = 0
for gid in set(espn) | set(ug):
    g = games.get(gid)
    if not g or g.get("pm") is None or g.get("status") != "pre" or pd.Timestamp(g["start"]) <= now: continue
    o, mine = rec(gid)
    if not o or not o.get("seen"): continue
    j = INJ.get(gid) or {}
    gg = dict(g, pm=g["pm"] + j["adj"]) if j.get("adj") else g
    for b in top_bets(bets_for(gg, o, sdm, sdt, books if mine else None)):
        rid = f"{gid}-{'t' if b['market'] == 'total' else 's'}"
        if rid in have or b["ev"] < min_for(gg, b, MIN): continue
        tm = timing(gg, b, o)
        doc = dict(game_id=gid, rec_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"), odds_seen=o.get("seen"), start=g["start"], tbd=bool(g.get("tbd")),
                   home=g["home"], away=g["away"], neutral=int(bool(g.get("neutral"))), early=bool(g.get("early")),
                   hours_to_tip=round((pd.Timestamp(g["start"]) - now).total_seconds() / 3600, 1),
                   market=b["market"], side=b["side"], line=b["line"], price=b["price"], book=b.get("book") or o.get("book", ""),
                   ev=round(b["ev"], 4), p=round(b["p"], 4), units=round(b["units"], 2), off_pinnacle=b.get("off"),
                   timing=None if tm is None else round(tm, 2), prime=bool(is_prime(gg, b, o)), caution=caution(gg, b, o),
                   model_pm=round(gg["pm"], 2), model_pt=g.get("pt"), injury_adj=j.get("adj") or 0,
                   mkt_sp=o.get("sp"), mkt_tot=o.get("tot"), mkt_nvh=o.get("nvh"), anchor=o.get("anchor") or ("ESPN" if not mine else "books"),
                   open_sp=(o.get("open") or {}).get("sp"), open_tot=(o.get("open") or {}).get("tot"))
        json.dump({k: v for k, v in doc.items() if v is not None}, open(f"{OUT}/{rid}.json", "w"))
        have.add(rid); n += 1
        print(f"{rid}: {b['market']} {b['side']} {b['line']} {b['price']} {doc['book']} edge {b['ev']:+.1%}")
print(f"recs: {n} new board recommendation(s) recorded; {len(have)} in the ledger", file=sys.stderr)
