"""Grade the board's recommendation ledger into the repo's recs.csv.

usage: python3 grade_recs.py <hoopR-mbb-data/mbb dir> <repo dir> <folder of rec docs>
<folder of rec docs> holds the board's "rec" collection as saved by ArtifactData (one <id>.json per recommendation).
Writes <repo>/recs.csv (every rec, with result, units won at the rec's own stake, closing line and CLV from lines.csv)
and prints a one-line summary: record, ROI, beat-the-close rate - overall and by market.
"""
import sys, os, re, glob, json
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from boardmath import payout

SRC, REPO, RECS = sys.argv[1:4]
rows = []
for f in sorted(glob.glob(f"{RECS}/*.json")):
    d = json.load(open(f)); d["id"] = os.path.basename(f)[:-5]; rows.append(d)
if not rows:
    print("board recs: none recorded yet", file=sys.stderr); sys.exit(0)
R = pd.DataFrame(rows); R["game_id"] = R.game_id.astype(str)
season = max(int(re.search(r"_(\d{4})\.parquet$", f).group(1)) for f in glob.glob(f"{SRC}/schedules/parquet/mbb_schedule_*.parquet"))
S = pd.read_parquet(f"{SRC}/schedules/parquet/mbb_schedule_{season}.parquet")
S = S[S.status_type_completed == True].copy(); S["game_id"] = S.game_id.astype(str); S = S.set_index("game_id")
L = pd.read_csv(f"{REPO}/lines.csv", dtype={"game_id": str}).drop_duplicates("game_id", keep="last").set_index("game_id") if os.path.exists(f"{REPO}/lines.csv") else pd.DataFrame()
for c in ("result", "units_won", "final", "close", "clv"): R[c] = None
for i, r in R.iterrows():
    if r.game_id not in S.index: continue
    s = S.loc[r.game_id]; m = float(s.home_score) - float(s.away_score); t = float(s.home_score) + float(s.away_score)
    # the rec's home/away are the model's (schedule) orientation
    if r.market == "spread": c = (m if r.side == "home" else -m) + r.line
    elif r.market == "total": c = (t - r.line) if r.side == "over" else (r.line - t)
    else: c = m if r.side == "home" else -m
    res = "win" if c > 0 else "loss" if c < 0 else "push"
    R.at[i, "result"] = res; R.at[i, "final"] = f"{s.away_display_name} {int(s.away_score)} @ {s.home_display_name} {int(s.home_score)}"
    R.at[i, "units_won"] = round(r.units * payout(r.price) if res == "win" else -r.units if res == "loss" else 0.0, 3)
    if len(L) and r.game_id in L.index:
        l = L.loc[r.game_id]; flip = pd.notna(l.get("home_id")) and str(int(float(l.home_id))) != str(int(s.home_id))
        if r.market == "spread" and pd.notna(l.spread_close):
            sp = -l.spread_close if flip else l.spread_close
            R.at[i, "close"] = sp; R.at[i, "clv"] = round(r.line - sp if r.side == "home" else r.line + sp, 2)
        elif r.market == "total" and pd.notna(l.total_close):
            R.at[i, "close"] = l.total_close; R.at[i, "clv"] = round((l.total_close - r.line) if r.side == "over" else (r.line - l.total_close), 2)
cols = ["id", "game_id", "rec_at", "start", "home", "away", "neutral", "early", "hours_to_tip", "market", "side", "line", "price", "book", "ev", "p",
        "units", "off_pinnacle", "timing", "prime", "caution", "model_pm", "model_pt", "injury_adj", "mkt_sp", "mkt_tot", "anchor", "open_sp", "open_tot",
        "result", "units_won", "final", "close", "clv"]
R = R.reindex(columns=cols).sort_values(["start", "id"])
R.to_csv(f"{REPO}/recs.csv", index=False)
G = R[R.result.isin(["win", "loss", "push"])]
def summ(g):
    w, l = (g.result == "win").sum(), (g.result == "loss").sum(); st = g.units.sum()
    clv = pd.to_numeric(g.clv, errors="coerce").dropna()
    return (f"{w}-{l}" + (f", ROI {pd.to_numeric(g.units_won).sum() / st:+.1%}" if st else "")
            + (f", beat close {(clv > 0).mean():.0%}" if len(clv) else ""))
parts = [f"{mk} {summ(G[G.market == mk])}" for mk in ("spread", "total", "ml") if (G.market == mk).any()]
if "prime" in G and G.prime.fillna(False).astype(bool).any(): parts.append(f"PRIME {summ(G[G.prime.fillna(False).astype(bool)])}")
# ---- closing-line scoreboard: CLV is the real test (a few hundred bets settle it; win-loss needs thousands)
def clvstats(g):
    c = pd.to_numeric(g.clv, errors="coerce").dropna()
    return dict(n=int(len(c)), avg=round(float(c.mean()), 2) if len(c) else None, beat=round(float((c > 0).mean()), 3) if len(c) else None,
                same=round(float((c == 0).mean()), 3) if len(c) else None)
Rc = R[pd.to_numeric(R.clv, errors="coerce").notna()].sort_values("start")
sp = Rc[Rc.market == "spread"]; last = sp.tail(200)
st = clvstats(last)
# stop rule on spreads (totals points are worth about half as much): judged on the last 200 once 100+ have a closing line
status = "early" if st["n"] < 100 else "stop" if st["avg"] < 0.5 else "watch" if st["avg"] < 1.0 else "ok"
card = dict(updated=pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"), status=status, rule="last 200 spread recs: average CLV under +0.5 pts = stop and re-check; under +1.0 = watch",
            spread_last200=st, spread_all=clvstats(sp), total_all=clvstats(Rc[Rc.market == "total"]),
            prime=clvstats(Rc[Rc.prime.fillna(False).astype(bool)]) if "prime" in Rc else None,
            record=dict(w=int((G.result == "win").sum()), l=int((G.result == "loss").sum()), p=int((G.result == "push").sum()),
                        units=round(float(pd.to_numeric(G.units_won).sum()), 2), staked=round(float(G.units.sum()), 2)),
            close_source="DraftKings close via ESPN (lines.csv)")
# compact graded-rec feed for the artifact's Stats tab: one array per graded rec, newest 1500 (keeps the doc well under the size limit)
# [date(ET), market, side, line, price, units, units_won, result, clv, ev, p, prime, early, hours_to_tip, caution, book]
def _n(x, d=3):
    try:
        v = float(x); return None if v != v else round(v, d)
    except Exception: return None
rows = []
for _, r in G.sort_values("start").iterrows():
    dt = pd.Timestamp(r.start).tz_convert("America/New_York").strftime("%Y-%m-%d") if pd.notna(r.start) else None
    rows.append([dt, r.market, r.side, _n(r.line, 1), _n(r.price, 0), _n(r.units, 2), _n(r.units_won, 3), r.result, _n(r.clv, 2), _n(r.ev, 3), _n(r.p, 3),
                 int(bool(r.prime)) if pd.notna(r.prime) else 0, int(bool(r.early)) if pd.notna(r.early) else 0, _n(r.hours_to_tip, 1),
                 1 if pd.notna(r.get("caution")) and r.get("caution") else 0, r.book])
card["cols"] = ["date", "market", "side", "line", "price", "units", "won", "result", "clv", "ev", "p", "prime", "early", "hrs", "caution", "book"]
card["rows"] = rows[-1500:]
json.dump(card, open(f"{REPO}/scorecard.json", "w"), separators=(",", ":"))
avg_txt = "-" if st["avg"] is None else f"{st['avg']:+.2f}"; beat_txt = "-" if st["beat"] is None else f"{st['beat']:.0%}"
print(f"board recs CLV: status {status.upper()} | spreads last {st['n']}: avg CLV {avg_txt} pts, beat close {beat_txt}", file=sys.stderr)
print(f"board recs: {len(R)} recorded, {len(G)} graded" + (f": {summ(G)}" if len(G) else "") + (f" ({'; '.join(parts)})" if parts else ""), file=sys.stderr)
