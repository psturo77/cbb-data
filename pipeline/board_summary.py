"""Daily summary + manifest for the board-data branch (run by the "CBB daily data" Action after the build).

usage: python3 board_summary.py <repo dir> <dash dir> <log dir> <lines rows before>
Writes <dash>/manifest.json: {built, today (US Eastern), data_through, checks, files: [...], lines: [summary lines]}.
The Claude daily task reads it, writes the listed files to the board, and copies `lines` into its reply and board/runlog.
"""
import sys, os, re, glob, json
import pandas as pd

R, D, LOG, L0 = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
now = pd.Timestamp.now(tz="UTC"); today = now.tz_convert("America/New_York").strftime("%Y-%m-%d")
logtxt = lambda n: open(f"{LOG}/{n}.txt").read() if os.path.exists(f"{LOG}/{n}.txt") else ""
grab = lambda n, pat: [l.strip() for l in logtxt(n).splitlines() if re.search(pat, l)]
lines = grab("espn_schedule", r"^espn_schedule:") or ["SCHEDULE NOT COMPLETED - espn_schedule.py printed no summary"]
meta = json.load(open(f"{R}/meta.json"))
gbs = meta.get("games_by_season", {})
cur = meta.get("stats_season", "")
lines.append(f"Games through {meta.get('data_through')}; {meta.get('games')} training games"
             + (f", {gbs[cur]} this season" if cur in gbs else ""))
t = pd.read_csv(f"{R}/teams.csv")
lines.append(f"Ratings: {meta.get('stats_season')} through {meta.get('stats_through')}; HCA {t.HCA.min():.2f}-{t.HCA.max():.2f}")
m = pd.read_csv(f"{R}/model.csv", keep_default_na=False)
c = lambda q: float(m[q].coef.iloc[0]) if q.any() else float("nan")
net = c((m.target == "margin") & (m.type == "diff") & (m.col1 == "Net"))
miss = c((m.target == "margin") & (m.col1 == "Miss"))
sg = c(m.label == "sigma_margin")
lines.append(f"Spread model: rating coef {net:.3f}, missing-players coef {miss:+.3f}, sigma_margin {sg:.2f}")
l1 = sum(1 for _ in open(f"{R}/lines.csv"))
espn = json.load(open(f"{D}/board_espn.json")) if os.path.exists(f"{D}/board_espn.json") else None
lines.append(f"Closing lines saved: {l1 - L0} new; upcoming games with ESPN lines: {len(espn['games']) if espn else 'n/a (upcoming.py failed)'}; "
             f"teams with Miss > 0: {int((t.Miss > 0).sum())}")
lines += grab("players", r"^players:") or ["players.py failed - board/players not refreshed"]
lines += grab("props_proj", r"^props_proj:") or ["props_proj.py failed - board/props not refreshed"]
sl = f"{D}/slate_d{today}.json"
lines.append(f"Today's slate: {len(json.load(open(sl)).get('games', [])) if os.path.exists(sl) else 0} games")
files = sorted(os.path.basename(f) for f in glob.glob(f"{D}/*.json") if not f.endswith("manifest.json"))
json.dump(dict(built=now.strftime("%Y-%m-%dT%H:%M:%SZ"), today=today, data_through=meta.get("data_through"), checks="passed",
               files=files, lines=lines), open(f"{D}/manifest.json", "w"), indent=1)
print("\n".join(lines))
