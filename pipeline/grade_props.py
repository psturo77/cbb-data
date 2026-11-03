"""Grade the props forward test into the repo's props.csv.

usage: python3 grade_props.py <hoopR-mbb-data/mbb dir> <repo dir> <folder of prop docs>
For each recorded prop: the player's actual points (player box), whether he went over/under the consensus line, and
whether the model's lean (projection - line) was right. Prints how often the lean was right and the flat-bet return at
the best posted price, by size of lean - the evidence for whether props are worth betting.
"""
import sys, os, re, glob, json
import numpy as np, pandas as pd

SRC, REPO, DOCS = sys.argv[1:4]
rows = []
for f in glob.glob(f"{DOCS}/*.json"):
    d = json.load(open(f)); d["id"] = os.path.basename(f)[:-5]; rows.append(d)
if not rows: print("props: none recorded yet", file=sys.stderr); sys.exit(0)
R = pd.DataFrame(rows)
season = max(int(re.search(r"_(\d{4})\.parquet$", f).group(1)) for f in glob.glob(f"{SRC}/player_box/parquet/player_box_*.parquet"))
p = pd.read_parquet(f"{SRC}/player_box/parquet/player_box_{season}.parquet", columns=["game_id", "athlete_id", "points", "minutes", "did_not_play"])
p = p[p.athlete_id.notna()].copy(); p["key"] = p.game_id.astype(str) + "-" + p.athlete_id.astype(int).astype(str)
act = p.set_index("key")
payout = lambda pr: pr / 100 if pr > 0 else 100 / -pr
R["actual"] = R.id.map(act.points); R["mins"] = R.id.map(act.minutes)
R["dnp"] = R.id.map(act.did_not_play)
G = R[R.actual.notna() & (R.mins.fillna(0) > 0)].copy()
G["result"] = np.where(G.actual > G.line, "over", np.where(G.actual < G.line, "under", "push"))
G["lean_side"] = np.where(G.lean > 0, "over", "under")
G["lean_right"] = np.where(G.result == "push", np.nan, (G.result == G.lean_side).astype(float))
def flat(r):           # 1u on the model's side at the best posted price for that side
    o = r.over if r.lean_side == "over" else r.under
    if r.result == "push" or not isinstance(o, list): return 0.0
    L, pr = o[0], o[1]; a = r.actual
    win = a > L if r.lean_side == "over" else a < L
    return payout(pr) if win else (-1.0 if a != L else 0.0)
G["units"] = G.apply(flat, axis=1) if len(G) else []
out = R.merge(G[["id", "result", "lean_side", "lean_right", "units"]], on="id", how="left")
out.drop(columns=[c for c in ("over", "under") if c in out]).assign(
    over_line=R.over.map(lambda o: o[0] if isinstance(o, list) else None), over_price=R.over.map(lambda o: o[1] if isinstance(o, list) else None),
    under_line=R.under.map(lambda o: o[0] if isinstance(o, list) else None), under_price=R.under.map(lambda o: o[1] if isinstance(o, list) else None)
).sort_values(["start", "id"]).to_csv(f"{REPO}/props.csv", index=False)
parts = []
for lo, hi in ((0, 1.5), (1.5, 3), (3, 99)):
    g = G[(G.lean.abs() >= lo) & (G.lean.abs() < hi) & G.lean_right.notna()]
    if len(g): parts.append(f"lean {lo:g}-{hi:g}: {len(g)} props, right {g.lean_right.mean():.0%}, flat ROI {g.units.sum() / len(g):+.1%}")
print(f"props: {len(R)} recorded, {len(G)} graded" + (" | " + "; ".join(parts) if parts else ""), file=sys.stderr)
