"""Grade the board's recommendation ledger into the repo's recs.csv.

usage: python3 grade_recs.py <hoopR-mbb-data/mbb dir> <repo dir> <folder of rec docs>
<folder of rec docs> holds the board's "rec" collection as saved by ArtifactData (one <id>.json per recommendation).
Writes <repo>/recs.csv (every rec, with result, units won at the rec's own stake, closing line, CLV and market move from lines.csv)
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
for c in ("result", "units_won", "final", "close", "clv", "mv"): R[c] = None
# line age at the rec (Nov 21 2026): hours since our pulls first saw a line for the game. Recs from before Nov 21 have no first_seen;
# rec_first_seen.csv backfills the ones still in the odds doc then (opener first-seen times are only kept 14 days).
if "first_seen" not in R: R["first_seen"] = None
_bf = f"{REPO}/rec_first_seen.csv"
if os.path.exists(_bf):
    _m = pd.read_csv(_bf, dtype={"game_id": str}).set_index("game_id").first_seen
    R["first_seen"] = R.first_seen.where(R.first_seen.notna(), R.game_id.map(_m))
R["line_age_h"] = ((pd.to_datetime(R.rec_at, utc=True, format="ISO8601") - pd.to_datetime(R.first_seen, utc=True, format="ISO8601")).dt.total_seconds() / 3600).round(2)
for c in ("mkt_sp", "mkt_tot"):
    if c not in R: R[c] = None
# Two measures against the close (Nov 14 2026):
#   clv = the rec's own number vs the close. A book hanging a line 2 pts off the market "beats the close" by 2 even if the market never moves.
#   mv  = how far the market itself moved the rec's way: the close vs the market line when the rec was made (mkt_sp / mkt_tot).
#         This is the line-value test the scorecard and its stop rule use. (Market at rec is the board's line - Pinnacle or the books'
#         median; the close is DraftKings via ESPN, so expect some noise between the two sources.)
for i, r in R.iterrows():
    if r.game_id not in S.index: continue
    s = S.loc[r.game_id]; m = float(s.home_score) - float(s.away_score); t = float(s.home_score) + float(s.away_score)
    # The rec's home/away are usually the schedule's, but on some neutral-site games the board listed the teams the other way
    # round (Nov 21 2026: 9 recs, e.g. Bradley/George Mason). Orient the result and the close to the rec's own home team.
    swap = str(r.get("home")) == str(s.away_display_name) and str(r.get("away")) == str(s.home_display_name)
    if swap: m = -m
    if r.market == "spread": c = (m if r.side == "home" else -m) + r.line
    elif r.market == "total": c = (t - r.line) if r.side == "over" else (r.line - t)
    else: c = m if r.side == "home" else -m
    res = "win" if c > 0 else "loss" if c < 0 else "push"
    R.at[i, "result"] = res
    R.at[i, "final"] = (f"{s.home_display_name} {int(s.home_score)} vs {s.away_display_name} {int(s.away_score)}" if swap
                        else f"{s.away_display_name} {int(s.away_score)} @ {s.home_display_name} {int(s.home_score)}")
    R.at[i, "units_won"] = round(r.units * payout(r.price) if res == "win" else -r.units if res == "loss" else 0.0, 3)
    if len(L) and r.game_id in L.index:
        l = L.loc[r.game_id]; flip = pd.notna(l.get("home_id")) and str(int(float(l.home_id))) != str(int(s.home_id))
        if r.market == "spread" and pd.notna(l.spread_close):
            sp = -l.spread_close if flip else l.spread_close
            if swap: sp = -sp
            R.at[i, "close"] = sp; R.at[i, "clv"] = round(r.line - sp if r.side == "home" else r.line + sp, 2)
            if pd.notna(r.mkt_sp): R.at[i, "mv"] = round((r.mkt_sp - sp) if r.side == "home" else (sp - r.mkt_sp), 2)
        elif r.market == "total" and pd.notna(l.total_close):
            R.at[i, "close"] = l.total_close; R.at[i, "clv"] = round((l.total_close - r.line) if r.side == "over" else (r.line - l.total_close), 2)
            if pd.notna(r.mkt_tot): R.at[i, "mv"] = round((l.total_close - r.mkt_tot) if r.side == "over" else (r.mkt_tot - l.total_close), 2)
# ---- where a spread rec's edge came from (Nov 24 2026): the model vs one book's head start over the market.
#   model_pts = the board's fair line minus the market line, toward the rec's side (board weights from boardmath: open/close by time
#               to tip, early-season weights, missing players in full against early lines when the rec carries model_miss)
#   book_pts  = how many points better the rec's line was than the market line when made, credited up to OFF_FULL (1 pt) as the board does
#   driver    = "model" if model_pts > book_pts else "book".  tier = conferences of the two teams (ACC, Big East, Big Ten, Big 12, SEC).
from boardmath import W, CAP, OFF_FULL
HIGH = {2, 4, 7, 8, 23}
for c in ("model_pts", "book_pts", "driver", "tier"): R[c] = None
CONF = {}                                   # team id -> conference id, from any schedule row that has one
for side in ("home", "away"):
    if f"{side}_id" in S and f"{side}_conference_id" in S:
        for t, c in S[[f"{side}_id", f"{side}_conference_id"]].dropna().values: CONF[int(float(t))] = int(float(c))
for i, r in R.iterrows():
    if r.market != "spread" or pd.isna(r.get("mkt_sp")) or pd.isna(r.get("model_pm")) or pd.isna(r.get("line")): continue
    hrs = float(r.hours_to_tip) if pd.notna(r.get("hours_to_tip")) else 12.0
    tw = max(0.0, min(1.0, (hrs - 1) / 11)); early = bool(r.early) if pd.notna(r.get("early")) else False
    wo, wc = (W["openEarly"], W["closeEarly"]) if early else (W["open"], W["close"]); wsp = wc + (wo - wc) * tw
    mi = float(r.model_miss) if "model_miss" in R and pd.notna(r.get("model_miss")) else 0.0
    wm = wsp if early else W["close"] + (W["openMiss"] - W["close"]) * tw
    mkt = -float(r.mkt_sp); fm = mkt + wsp * max(-CAP, min(CAP, float(r.model_pm) - mi - mkt)) + wm * mi
    sgn = 1 if r.side == "home" else -1
    mp = sgn * (fm - mkt); head = (float(r.line) - float(r.mkt_sp)) if r.side == "home" else (float(r.line) + float(r.mkt_sp))
    bp = min(head, OFF_FULL)
    R.at[i, "model_pts"] = round(mp, 2); R.at[i, "book_pts"] = round(bp, 2); R.at[i, "driver"] = "model" if mp > bp else "book"
    s_ = S.loc[r.game_id] if r.game_id in S.index else None
    if s_ is not None and CONF and pd.notna(s_.get("home_id")) and pd.notna(s_.get("away_id")):
        cs = [CONF.get(int(float(s_.home_id))), CONF.get(int(float(s_.away_id)))]
        if None not in cs: R.at[i, "tier"] = ("other", "high vs other", "high-major")[sum(c in HIGH for c in cs)]
cols = ["id", "game_id", "rec_at", "start", "home", "away", "neutral", "early", "hours_to_tip", "market", "side", "line", "price", "book", "ev", "p",
        "units", "off_pinnacle", "timing", "prime", "caution", "model_pm", "model_pt", "injury_adj", "mkt_sp", "mkt_tot", "anchor", "open_sp", "open_tot",
        "first_seen", "line_age_h", "result", "units_won", "final", "close", "clv", "mv", "model_miss", "model_pts", "book_pts", "driver", "tier"]
R = R.reindex(columns=cols).sort_values(["start", "id"])
R.to_csv(f"{REPO}/recs.csv", index=False)
G = R[R.result.isin(["win", "loss", "push"])]
def summ(g):
    w, l = (g.result == "win").sum(), (g.result == "loss").sum(); st = g.units.sum()
    mv = pd.to_numeric(g.mv, errors="coerce").dropna()
    return (f"{w}-{l}" + (f", ROI {pd.to_numeric(g.units_won).sum() / st:+.1%}" if st else "")
            + (f", market moved our way {(mv > 0).mean():.0%}" if len(mv) else ""))
parts = [f"{mk} {summ(G[G.market == mk])}" for mk in ("spread", "total", "ml") if (G.market == mk).any()]
if "prime" in G and G.prime.fillna(False).astype(bool).any(): parts.append(f"PRIME {summ(G[G.prime.fillna(False).astype(bool)])}")
# ---- closing-line scoreboard: line value is the real test (a few hundred bets settle it; win-loss needs thousands).
# Judged on mv (market move since the rec), not clv - see above.
def clvstats(g):
    c = pd.to_numeric(g.mv, errors="coerce").dropna()
    return dict(n=int(len(c)), avg=round(float(c.mean()), 2) if len(c) else None, beat=round(float((c > 0).mean()), 3) if len(c) else None,
                same=round(float((c == 0).mean()), 3) if len(c) else None)
Rc = R[pd.to_numeric(R.mv, errors="coerce").notna()].sort_values("start")
sp = Rc[Rc.market == "spread"]; last = sp.tail(200)
st = clvstats(last)
# stop rule on spreads (totals points are worth about half as much): judged on the last 200 once 100+ have a closing line
status = "early" if st["n"] < 100 else "stop" if st["avg"] < 0.5 else "watch" if st["avg"] < 1.0 else "ok"
card = dict(updated=pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"), status=status, rule="last 200 spread recs: average market move under +0.5 pts = stop and re-check; under +1.0 = watch",
            measure="market move: the close vs the market line when the rec was made (not the rec's own number vs the close)",
            spread_last200=st, spread_all=clvstats(sp), total_all=clvstats(Rc[Rc.market == "total"]),
            prime=clvstats(Rc[Rc.prime.fillna(False).astype(bool)]) if "prime" in Rc else None,
            record=dict(w=int((G.result == "win").sum()), l=int((G.result == "loss").sum()), p=int((G.result == "push").sum()),
                        units=round(float(pd.to_numeric(G.units_won).sum()), 2), staked=round(float(G.units.sum()), 2)),
            close_source="DraftKings close via ESPN (lines.csv)")
# spreads by how long the line had been up when the rec was made (the backtest edge was at openers)
_age = pd.to_numeric(sp.line_age_h, errors="coerce")
card["spread_by_line_age"] = {lab: dict(clvstats(sp[(_age > lo) & (_age <= hi)]), w=int((sp[(_age > lo) & (_age <= hi)].result == "win").sum()),
                                        l=int((sp[(_age > lo) & (_age <= hi)].result == "loss").sum()),
                                        units=round(float(pd.to_numeric(sp[(_age > lo) & (_age <= hi)].units_won).sum()), 2))
                              for lab, lo, hi in (("under 1.5h", -1, 1.5), ("1.5-6h", 1.5, 6), ("6h+", 6, 1e9))}
card["spread_line_age_known"] = int(_age.notna().sum())
# spreads by where the edge came from (model vs a book's head start) and by conference tier (Nov 24 2026)
def _grp(g):
    return dict(clvstats(g), w=int((g.result == "win").sum()), l=int((g.result == "loss").sum()), units=round(float(pd.to_numeric(g.units_won).sum()), 2),
                model_pts=round(float(pd.to_numeric(g.model_pts, errors="coerce").mean()), 2) if len(g) else None,
                book_pts=round(float(pd.to_numeric(g.book_pts, errors="coerce").mean()), 2) if len(g) else None)
_late = ~sp.early.fillna(True).astype(bool)
card["spread_by_driver"] = {d: _grp(sp[sp.driver == d]) for d in ("model", "book")}
card["spread_by_driver_late"] = {d: _grp(sp[(sp.driver == d) & _late]) for d in ("model", "book")}   # both teams 5+ games
card["spread_by_tier"] = {t: _grp(sp[sp.tier == t]) for t in ("high-major", "high vs other", "other")}
# compact graded-rec feed for the artifact's Stats tab: one array per graded rec, newest 1500 (keeps the doc well under the size limit)
# [date(ET), market, side, line, price, units, units_won, result, clv, ev, p, prime, early, hours_to_tip, caution, book, mv]
def _n(x, d=3):
    try:
        v = float(x); return None if v != v else round(v, d)
    except Exception: return None
rows = []
for _, r in G.sort_values("start").iterrows():
    dt = pd.Timestamp(r.start).tz_convert("America/New_York").strftime("%Y-%m-%d") if pd.notna(r.start) else None
    rows.append([dt, r.market, r.side, _n(r.line, 1), _n(r.price, 0), _n(r.units, 2), _n(r.units_won, 3), r.result, _n(r.clv, 2), _n(r.ev, 3), _n(r.p, 3),
                 int(bool(r.prime)) if pd.notna(r.prime) else 0, int(bool(r.early)) if pd.notna(r.early) else 0, _n(r.hours_to_tip, 1),
                 1 if pd.notna(r.get("caution")) and r.get("caution") else 0, r.book, _n(r.mv, 2)])
card["cols"] = ["date", "market", "side", "line", "price", "units", "won", "result", "clv", "ev", "p", "prime", "early", "hrs", "caution", "book", "mv"]
card["rows"] = rows[-1500:]
json.dump(card, open(f"{REPO}/scorecard.json", "w"), separators=(",", ":"))
avg_txt = "-" if st["avg"] is None else f"{st['avg']:+.2f}"; beat_txt = "-" if st["beat"] is None else f"{st['beat']:.0%}"
print(f"board recs line value: status {status.upper()} | spreads last {st['n']}: market moved our way {avg_txt} pts avg, {beat_txt} of recs", file=sys.stderr)
print(f"board recs: {len(R)} recorded, {len(G)} graded" + (f": {summ(G)}" if len(G) else "") + (f" ({'; '.join(parts)})" if parts else ""), file=sys.stderr)
