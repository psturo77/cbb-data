"""Paper trading: an objective record of what the board would have bet.

usage: python3 paper.py <hoopR-mbb-data/mbb dir> <repo dir> <dash dir>
Reads <dash>/board_meta.json, <dash>/slate_d*.json and <dash>/board_espn.json (ESPN/DraftKings lines),
applies the board's Bets-tab math (same constants as cbb-board.html), and appends to <repo>/paper.csv the best bet
for every upcoming game whose edge clears MIN_EV - once per game, at the first line it qualified at.
Then grades earlier rows: result from the schedule's final score, CLV against the closing line in <repo>/lines.csv.
"""
import sys, os, re, glob, json
from math import erf, sqrt
import numpy as np, pandas as pd

SRC, REPO, DASH = sys.argv[1], sys.argv[2], sys.argv[3]
log = lambda *a: print(*a, file=sys.stderr, flush=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from boardmath import bets_for, top_bets, MIN_EV, payout

meta = json.load(open(f"{DASH}/board_meta.json"))
sdm, sdt = meta.get("sigma_margin", 11.2), meta.get("sigma_total", 16.7)
espn = json.load(open(f"{DASH}/board_espn.json")) if os.path.exists(f"{DASH}/board_espn.json") else {"games": {}}
games = {}
for f in glob.glob(f"{DASH}/slate_d*.json"):
    for g in json.load(open(f))["games"]: games[g["id"]] = g
now = pd.Timestamp.now(tz="UTC")

path = f"{REPO}/paper.csv"
P = pd.read_csv(path, dtype={"game_id": str}) if os.path.exists(path) else pd.DataFrame()
grp = lambda m: "total" if m == "total" else "side"          # one side bet and one total per game, at most
have = set(zip(P.game_id, P.market.map(grp))) if len(P) else set()
new = []
for gid, o in espn.get("games", {}).items():
    g = games.get(gid)
    if not g or g.get("pm") is None or pd.Timestamp(g["start"]) <= now: continue
    for b in top_bets(bets_for(g, o, sdm, sdt)):
        if b["ev"] < MIN_EV or (gid, grp(b["market"])) in have: continue
        new.append(dict(game_id=gid, date=g["start"][:10], start=g["start"], recorded=o["seen"], home=g["home"], away=g["away"],
                        early=bool(g.get("early")), book=o.get("book", ""), market=b["market"], side=b["side"], line=b["line"],
                        price=b["price"], p=round(b["p"], 4), ev=round(b["ev"], 4), units=round(b["units"], 2),
                        model_pm=g["pm"], model_pt=g.get("pt"), mkt_sp=o.get("sp"), mkt_tot=o.get("tot")))
        have.add((gid, grp(b["market"])))
if new: P = pd.concat([P, pd.DataFrame(new)], ignore_index=True)

# ---- grade finished games
if len(P):
    season = max(int(re.search(r"_(\d{4})\.parquet$", f).group(1)) for f in glob.glob(f"{SRC}/schedules/parquet/mbb_schedule_*.parquet"))
    S = pd.read_parquet(f"{SRC}/schedules/parquet/mbb_schedule_{season}.parquet")
    S = S[(S.status_type_completed == True)]; S["game_id"] = S.game_id.astype(str); S = S.set_index("game_id")
    L = pd.read_csv(f"{REPO}/lines.csv", dtype={"game_id": str}).set_index("game_id") if os.path.exists(f"{REPO}/lines.csv") else pd.DataFrame()
    for col in ("result", "units_won", "close", "clv", "final"):
        if col not in P: P[col] = np.nan
    P["result"] = P.result.astype(object); P["final"] = P.final.astype(object)
    for i, r in P[P.result.isna()].iterrows():
        if r.game_id not in S.index: continue
        s = S.loc[r.game_id]; m = float(s.home_score) - float(s.away_score); t = float(s.home_score) + float(s.away_score)
        if r.market == "spread": c = (m if r.side == "home" else -m) + r.line
        elif r.market == "total": c = (t - r.line) if r.side == "over" else (r.line - t)
        else: c = m if r.side == "home" else -m
        res = "win" if c > 0 else "loss" if c < 0 else "push"
        P.at[i, "result"] = res; P.at[i, "final"] = f"{s.away_display_name} {int(s.away_score)} @ {s.home_display_name} {int(s.home_score)}"
        P.at[i, "units_won"] = round(r.units * payout(r.price) if res == "win" else -r.units if res == "loss" else 0.0, 3)
        if len(L) and r.game_id in L.index:
            l = L.loc[r.game_id]; flip = pd.notna(l.get("home_id")) and str(int(float(l.home_id))) != str(int(s.home_id))
            if r.market == "spread" and pd.notna(l.spread_close):
                sp = -l.spread_close if flip else l.spread_close
                P.at[i, "close"] = sp; P.at[i, "clv"] = r.line - sp if r.side == "home" else r.line + sp
            elif r.market == "total" and pd.notna(l.total_close):
                P.at[i, "close"] = l.total_close; P.at[i, "clv"] = (l.total_close - r.line) if r.side == "over" else (r.line - l.total_close)
    P.to_csv(path, index=False)
    g = P[P.result.isin(["win", "loss", "push"])]
    w, l = (g.result == "win").sum(), (g.result == "loss").sum()
    staked = g.units.sum()
    log(f"paper trading: {len(new)} new picks today, {len(P)} total; graded {len(g)}: {w}-{l}"
        + (f", ROI {g.units_won.sum() / staked:+.1%} on {staked:.1f}u" if staked else "")
        + (f", beat the close {(g.clv > 0).mean():.0%} of the time (avg CLV {g.clv.mean():+.2f})" if g.clv.notna().any() else ""))
else:
    log("paper trading: no qualifying picks yet")
