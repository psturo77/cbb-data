"""Edge alerts: upcoming games with a NEW board edge at or above a threshold, plus new arbs across your books.

Alerts are any NEW bet whose edge clears the threshold (default 5%; road teams need 6%), best edge first; CAUTION totals
are left out. The --digest morning summary lists every qualifying bet (3%+), CAUTION ones tagged, ranked by edge.
usage: python3 alerts.py <folder of board slate docs> <board_meta.json> <board_espn.json> <alerted.json> [min edge, default 0.05] [board_odds.json]
                        [--inj=board_injuries.json] [--digest[=min edge, default 0.03]]
--inj: players listed Out move the model line, as on the board. --digest: instead of new alerts, print the morning summary
("today's best bets", one line per game tipping today ET whose best bet clears the digest minimum, best first, whether or
not it was alerted before); <alerted.json> is left unchanged.
Prints one line per new alert (nothing else on stdout) and rewrites <alerted.json> as {"alerted": {"<key>": "<time>"}}
so each bet is only announced once. Uses the board's own math (boardmath.py) and the board's choice of odds:
the all-books pull (board/odds: best price among your books, Pinnacle as the market line) unless ESPN's line is
more than 6 hours newer; ESPN's true opener and BPI are used either way.
"""
import sys, os, glob, json
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from boardmath import calibrate_all, bets_for, top_bets, min_for, arbs_for, payout, caution

flags = {a.split("=")[0]: (a.split("=", 1)[1] if "=" in a else "") for a in sys.argv[1:] if a.startswith("--")}
args = [a for a in sys.argv[1:] if not a.startswith("--")]
SLATES, META, ESPN, SEEN = args[0:4]
MIN = float(args[4]) if len(args) > 4 else 0.05
ODDS = args[5] if len(args) > 5 else None
INJ = json.load(open(flags["--inj"])).get("games", {}) if flags.get("--inj") and os.path.exists(flags["--inj"]) else {}
DIGEST = "--digest" in flags
if DIGEST: MIN = float(flags["--digest"] or 0.03)
meta = json.load(open(META)); sdm, sdt = meta.get("sigma_margin", 11.2), meta.get("sigma_total", 16.7)
espn = json.load(open(ESPN)).get("games", {}) if os.path.exists(ESPN) else {}
od = json.load(open(ODDS)) if ODDS and os.path.exists(ODDS) else {}
ug, books = od.get("games", {}), od.get("books", [])
seen = json.load(open(SEEN)).get("alerted", {}) if os.path.exists(SEEN) else {}
games = {}
for f in glob.glob(f"{SLATES}/slate_d*.json") + glob.glob(f"{SLATES}/d20*.json"):
    for g in json.load(open(f)).get("games", []): games[g["id"]] = g
games, CAL = calibrate_all(games, ug)          # early-season market calibration (same as the board)
now = pd.Timestamp.now(tz="UTC")
et = lambda t: pd.Timestamp(t).tz_convert("America/New_York")
loc = lambda g, side: g.get("hloc" if side == "home" else "aloc") or g.get(side)
fmt = lambda x: "PK" if abs(x) < 0.05 else (f"+{x:g}" if x > 0 else f"−{abs(x):g}")
price = lambda p: f"+{int(p)}" if p > 0 else f"−{abs(int(p))}"

def rec(gid):                      # same rule as the board's oddsRec()
    u, e = ug.get(gid), espn.get(gid)
    if not e: return u, bool(u)
    if u and pd.Timestamp(u["seen"]) >= pd.Timestamp(e["seen"]) - pd.Timedelta(hours=6):
        r = dict(u)
        if (e.get("open") or {}).get("sp") is not None: r["open"] = e["open"]
        if e.get("bpi") is not None: r["bpi"] = e["bpi"]
        return r, True
    return e, False

lines = []
for gid in set(espn) | set(ug):
    g = games.get(gid)
    if not g or g.get("pm") is None or g.get("status") != "pre" or pd.Timestamp(g["start"]) <= now: continue
    o, mine = rec(gid)
    if not o or not o.get("seen"): continue
    j = INJ.get(gid) or {}
    if j.get("adj"): g = dict(g, pm=g["pm"] + j["adj"])
    if DIGEST and et(g["start"]).date() != now.tz_convert("America/New_York").date(): continue
    inj = "; ".join(f"{x[0]} ({loc(g, s)}) {x[1].lower()}" for s, k in (("home", "h"), ("away", "a")) for x in j.get(k, []))
    tip = "time TBD" if g.get("tbd") else et(g["start"]).strftime("%a %b %-d %-I:%M %p")
    matchup = f"{loc(g, 'away')} at {loc(g, 'home')}, {tip}"
    for b in top_bets(bets_for(g, o, sdm, sdt, books if mine else None)):
        if b["ev"] < min_for(g, b, MIN): continue
        warn = caution(g, b, o)
        if not DIGEST and warn: continue                       # alerts: any new bet over the threshold, never CAUTION ones
        name = (f"{loc(g, b['side'])} {fmt(b['line'])}" if b["market"] == "spread" else
                f"{'Over' if b['side'] == 'over' else 'Under'} {b['line']:g}" if b["market"] == "total" else f"{loc(g, b['side'])} ML")
        key = f"{gid}|{b['market']}|{b['side']}|{b['line']}"
        if key in seen and not DIGEST: continue
        off = f" - {b['off']:g} pts better than Pinnacle" if b.get("off") else ""
        tag = "CAUTION " if warn else ""
        lines.append((b["ev"], f"{tag}{name} {price(b['price'])} ({b.get('book') or o.get('book', 'ESPN')}) - edge {b['ev']:+.1%}, "
                               f"{b['p']:.0%} to win, {b['units']:.1f}u - {matchup}{off}" + (f" - injury: {inj}" if inj else "")))
        seen[key] = now.isoformat()
    if mine and o.get("opt") and not DIGEST:
        for x in arbs_for(o, books):
            if not x["arb"]: continue
            def leg(l):
                side, L, P, bk = l
                nm = (f"{loc(g, side)} {fmt(L)}" if x["kind"] == "spread" else f"{side.title()} {L:g}" if x["kind"] == "total" else f"{loc(g, side)} ML")
                return f"{nm} {price(P)} at {bk}"
            key = f"{gid}|arb|{x['kind']}|{x['a'][1]}|{x['a'][2]}|{x['b'][1]}|{x['b'][2]}"
            if key in seen: continue
            share = (1 / (1 + payout(x["a"][2]))) / x["sum"]
            lines.append((1 + (1 / x["sum"] - 1), f"ARB +{1 / x['sum'] - 1:.1%} locked - {leg(x['a'])} ({share:.0%} of stake) + "
                                                f"{leg(x['b'])} ({1 - share:.0%}) - {matchup}"))
            seen[key] = now.isoformat()
for _, l in sorted(lines, reverse=True): print(l)
if DIGEST:
    print(f"digest: {len(lines)} game(s) today with a best bet at {MIN:.0%}+", file=sys.stderr); sys.exit(0)
cut = (now - pd.Timedelta(days=14)).isoformat()
json.dump({"alerted": {k: v for k, v in seen.items() if v >= cut}}, open(SEEN, "w"))
print(f"{len(lines)} new alert(s) (edges {MIN:.0%}+ and arbs) across {len(set(espn) | set(ug))} games with lines", file=sys.stderr)
