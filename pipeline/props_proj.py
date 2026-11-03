"""Player points projections for the next two days of games (props forward test - not used for betting yet).

usage: python3 props_proj.py <hoopR-mbb-data/mbb dir> <out board_props.json> [today YYYY-MM-DD]
Projection = expected minutes x points per minute x opponent defense x pace, as tested in lab/props_test.py (2025-26:
when it was 2+ pts below a player's average he fell short 60% of the time vs a 53% base rate). Early in the season the
rates are shrunk toward last season (players) and the league average (opponents).
Writes {updated, games: {game_id: {start, home, away, players: [[athlete_id, name, team_side, proj, avg, exp_min, n]]}}}
for players averaging 6+ points (the ones books post props on). The odds task's props_snap.py pairs these with posted lines.
"""
import sys, os, re, glob, json
import numpy as np, pandas as pd

SRC, OUT = sys.argv[1], sys.argv[2]
TODAY = pd.Timestamp(sys.argv[3]) if len(sys.argv) > 3 and not sys.argv[3].startswith("--") else pd.Timestamp.now(tz="America/New_York").tz_localize(None).normalize()
log = lambda *a: print(*a, file=sys.stderr, flush=True)
season = max(int(re.search(r"_(\d{4})\.parquet$", f).group(1)) for f in glob.glob(f"{SRC}/schedules/parquet/mbb_schedule_*.parquet"))
K_PLAYER, K_TEAM = 5.0, 6.0          # shrinkage: games of last-season rate / league average blended in

def box(y):
    f = f"{SRC}/player_box/parquet/player_box_{y}.parquet"
    if not os.path.exists(f): return pd.DataFrame(columns=["game_id", "game_date", "team_id", "athlete_id", "athlete_display_name", "minutes", "did_not_play", "points", "min", "date"])
    p = pd.read_parquet(f, columns=["game_id", "game_date", "team_id", "athlete_id", "athlete_display_name", "minutes", "did_not_play", "points"])
    p = p[p.athlete_id.notna() & p.team_id.notna()].copy()
    p["min"] = p.minutes.fillna(0).clip(0, 60); p.loc[p.did_not_play == True, "min"] = 0
    p["date"] = pd.to_datetime(p.game_date); p["athlete_id"] = p.athlete_id.astype(int); p["team_id"] = p.team_id.astype(int)
    return p[p.date < TODAY]

def teambox(y):
    f = f"{SRC}/team_box/parquet/team_box_{y}.parquet"
    if not os.path.exists(f): return pd.DataFrame()
    t = pd.read_parquet(f, columns=["game_id", "game_date", "team_id", "opponent_team_score", "field_goals_attempted", "offensive_rebounds", "turnovers", "free_throws_attempted"])
    t["date"] = pd.to_datetime(t.game_date); t = t[t.date < TODAY].copy(); t["team_id"] = t.team_id.astype(int)
    t["poss"] = t.field_goals_attempted - t.offensive_rebounds + t.turnovers + 0.475 * t.free_throws_attempted
    return t[t.poss > 40]

cur, prev = box(season), box(season - 1)
tb = teambox(season); tbp = teambox(season - 1)
lg_ppp = (tbp.opponent_team_score.sum() / tbp.poss.sum()) if len(tbp) else 1.03
lg_pace = tbp.poss.mean() if len(tbp) else 68.0
if len(tb):
    lg_ppp = (tb.opponent_team_score.sum() + K_TEAM * 50 * lg_ppp) / (tb.poss.sum() + K_TEAM * 50) if len(tb) < 2000 else tb.opponent_team_score.sum() / tb.poss.sum()
T = tb.groupby("team_id").agg(n=("poss", "size"), pts=("opponent_team_score", "sum"), poss=("poss", "sum"), pace=("poss", "mean")) if len(tb) else pd.DataFrame(columns=["n", "pts", "poss", "pace"])
def team_fac(tid):
    if tid in T.index:
        r = T.loc[tid]; d = (r.pts + K_TEAM * 70 * lg_ppp) / (r.poss + K_TEAM * 70); pace = (r.pace * r.n + K_TEAM * lg_pace) / (r.n + K_TEAM)
    else: d, pace = lg_ppp, lg_pace
    return d / lg_ppp, pace

# per-player rates: this season, shrunk toward last season's rate (any team)
def rates(p):
    pl = p[p["min"] > 0]
    return pl.groupby("athlete_id").agg(pts=("points", "sum"), mins=("min", "sum"), n=("min", "size"))
R = rates(cur); Rp = rates(prev)
last5 = cur[cur["min"] > 0].sort_values("date").groupby("athlete_id").tail(5).groupby("athlete_id")["min"].mean()
names = cur.sort_values("date").groupby("athlete_id").athlete_display_name.last() if len(cur) else pd.Series(dtype=str)
team_of = cur.sort_values("date").groupby("athlete_id").team_id.last() if len(cur) else pd.Series(dtype=int)
played_last = {}
for tid, tp in cur.groupby("team_id"):
    lg = tp.sort_values("date").game_id.iloc[-1]; played_last[tid] = set(tp[(tp.game_id == lg) & (tp["min"] > 0)].athlete_id)

sch = pd.read_parquet(f"{SRC}/schedules/parquet/mbb_schedule_{season}.parquet",
                      columns=["game_id", "start_date", "home_id", "away_id", "home_display_name", "away_display_name", "status_type_completed"])
sch["start"] = pd.to_datetime(sch.start_date, utc=True)
et = sch.start.dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize()
up = sch[(et >= TODAY) & (et <= TODAY + pd.Timedelta(days=1)) & ((sch.status_type_completed != True) | ("--test" in sys.argv))]
games = {}
for _, g in up.iterrows():
    hid, aid = int(g.home_id), int(g.away_id)
    rows = []
    for side, tid, oid in (("home", hid, aid), ("away", aid, hid)):
        dfac, opace = team_fac(oid); _, own = team_fac(tid)
        for a in played_last.get(tid, set()):
            if a not in R.index: continue
            r = R.loc[a]; lp = Rp.loc[a] if a in Rp.index else None
            base_ppm = (lp.pts / lp.mins) if lp is not None and lp.mins > 100 else 0.30
            ppm = (r.pts + K_PLAYER * 25 * base_ppm) / (r.mins + K_PLAYER * 25)
            avg_min = r.mins / r.n; exp_min = 0.6 * last5.get(a, avg_min) + 0.4 * avg_min
            avg = r.pts / r.n
            if avg < 6 or r.n < 2: continue
            proj = exp_min * ppm * dfac * (((opace + own) / 2) / lg_pace)
            rows.append([int(a), str(names.get(a, "")), side, round(proj, 2), round(avg, 2), round(exp_min, 1), int(r.n)])
    if rows:
        games[str(g.game_id)] = dict(start=g.start.strftime("%Y-%m-%dT%H:%M:%SZ"), home=g.home_display_name, away=g.away_display_name,
                                     players=sorted(rows, key=lambda x: -x[3]))
if len(json.dumps(games)) > 200000:                      # stay well under the board's 256 KB document limit
    for g in games.values(): g["players"] = [r for r in g["players"] if r[4] >= 8]
doc = dict(updated=pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"), for_days=[str(TODAY.date()), str((TODAY + pd.Timedelta(days=1)).date())], games=games)
json.dump(doc, open(OUT, "w"), separators=(",", ":"), ensure_ascii=False)
log(f"props_proj: {sum(len(g['players']) for g in games.values())} player projections for {len(games)} games ({doc['for_days'][0]} - {doc['for_days'][1]}); {len(json.dumps(doc)) // 1024} KB")
