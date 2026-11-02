"""Sanity checks before the daily data commit (same rules as the Claude daily refresh). Exit 1 on failure.
usage: python3 check.py <repo dir> <games rows before> <lines rows before> <data_through before>"""
import sys, json, pandas as pd
R, g0, l0, d0 = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
bad = []
g = sum(1 for _ in open(f"{R}/games.csv"))
if g < g0: bad.append(f"games.csv shrank {g0} -> {g}")
t = pd.read_csv(f"{R}/teams.csv")
need = ["Team", "Net", "HCA", "LastGameSerial", "HomeFlag", "MktAdj", "MktHome", "Miss", "TotAdj", "TotShift"]
if len(t) < 350: bad.append(f"teams.csv has {len(t)} teams")
miss = [c for c in need if c not in t]
if miss: bad.append(f"teams.csv missing {miss}")
for c, lo, hi in (("HCA", 0, 7), ("MktAdj", -8, 8), ("Miss", 0, 25), ("TotAdj", -15, 15)):
    if c in t and (t[c].min() < lo or t[c].max() > hi): bad.append(f"{c} out of range {lo}..{hi}: {t[c].min():.2f}..{t[c].max():.2f}")
m = pd.read_csv(f"{R}/model.csv", keep_default_na=False)
if list(m.columns) != ["target", "type", "col1", "col2", "col3", "cap", "coef", "se", "label"]: bad.append("model.csv header")
else:
    if list(m.target[:10]) != ["margin"] * 10 or list(m.target[10:20]) != ["total"] * 10: bad.append("model.csv row layout")
    sg = m[m.label == "sigma_margin"].coef.astype(float)
    if not len(sg) or not 8 <= sg.iloc[0] <= 15: bad.append("sigma_margin")
    net = m[(m.target == "margin") & (m.type == "diff") & (m.col1 == "Net")].coef.astype(float)
    if not len(net) or not 0.7 <= net.iloc[0] <= 1.2: bad.append("Net coefficient")
d = json.load(open(f"{R}/meta.json")).get("data_through", "")
if d0 and d < d0: bad.append(f"data_through went back {d0} -> {d}")
l = sum(1 for _ in open(f"{R}/lines.csv"))
if l < l0: bad.append(f"lines.csv shrank {l0} -> {l}")
print("checks: " + ("passed" if not bad else "FAILED - " + "; ".join(bad)))
sys.exit(1 if bad else 0)
