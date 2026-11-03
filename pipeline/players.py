"""Who matters on each team: every team's top players by Game Score, for valuing injury reports.

usage: python3 players.py <hoopR-mbb-data/mbb dir> <repo dir with model.csv> <out board_players.json>
Writes {updated, season, basis, coef, teams: {team_id: {n: team name, g: team games, p: [[athlete_id, name, gs, reg, sat]]}}}
  gs  = average Game Score per game played this season (last season's, for players on this season's roster, until
        a team has played 3 games), the same measure as process.py's missing-regulars term (Miss)
  reg = 1 if a regular (same rule as Miss: played 60%+ of games, 15+ minutes a game)
  sat = 1 if he sat the team's last game - already in the model's Miss term, so an injury report adds nothing
coef = the model's points per Game Score point missing (model.csv margin row with col1 Miss), about -0.11.
Run by the daily task (it has the hoopR data); the odds task's injuries.py reads it from board/players.
"""
import sys, os, re, glob, json
import numpy as np, pandas as pd

SRC, REPO, OUT = sys.argv[1:4]
log = lambda *a: print(*a, file=sys.stderr, flush=True)
MISS = dict(share=0.6, min_minutes=15, min_games=3)
TOP = 8
COLS = ["game_id", "game_date", "team_id", "team_display_name", "athlete_id", "athlete_display_name", "minutes", "did_not_play",
        "points", "field_goals_made", "field_goals_attempted", "free_throws_made", "free_throws_attempted",
        "offensive_rebounds", "defensive_rebounds", "steals", "assists", "blocks", "fouls", "turnovers"]

def box(season):
    f = f"{SRC}/player_box/parquet/player_box_{season}.parquet"
    if not os.path.exists(f): return None
    p = pd.read_parquet(f, columns=COLS)
    p = p[p.athlete_id.notna() & p.team_id.notna()].copy()
    p["team_id"] = p.team_id.astype(int); p["athlete_id"] = p.athlete_id.astype(int); p["game_id"] = p.game_id.astype(str)
    p["min"] = p.minutes.fillna(0); p.loc[p.did_not_play == True, "min"] = 0
    v = lambda c: p[c].fillna(0)
    p["gs"] = (v("points") + 0.4 * v("field_goals_made") - 0.7 * v("field_goals_attempted") - 0.4 * (v("free_throws_attempted") - v("free_throws_made"))
               + 0.7 * v("offensive_rebounds") + 0.3 * v("defensive_rebounds") + v("steals") + 0.7 * v("assists") + 0.7 * v("blocks")
               - 0.4 * v("fouls") - v("turnovers"))
    p["date"] = pd.to_datetime(p.game_date)
    return p

def per_player(p):
    """per (team, athlete): games, avg minutes and avg Game Score over games played; sat the team's last game."""
    rows = []
    for tid, tp in p.groupby("team_id"):
        gids = tp[["game_id", "date"]].drop_duplicates().sort_values(["date", "game_id"]).game_id.tolist()
        last = gids[-1]
        for aid, ap in tp.groupby("athlete_id"):
            pl = ap[ap["min"] > 0]
            rows.append(dict(team_id=tid, team=tp.team_display_name.iloc[-1], tg=len(gids), athlete_id=aid,
                             name=ap.athlete_display_name.iloc[-1], gp=len(pl), mpg=pl["min"].mean() if len(pl) else 0,
                             gs=pl.gs.mean() if len(pl) else 0, sat=int(not (ap[ap.game_id == last]["min"] > 0).any())))
    return pd.DataFrame(rows)

seasons = sorted(int(re.search(r"(\d{4})\.parquet$", f).group(1)) for f in glob.glob(f"{SRC}/rosters/parquet/rosters_*.parquet"))
season = seasons[-1]
cur = box(season); prev = box(season - 1)
C = per_player(cur) if cur is not None and len(cur) else pd.DataFrame()
P = per_player(prev) if prev is not None else pd.DataFrame()
ros = pd.read_parquet(f"{SRC}/rosters/parquet/rosters_{season}.parquet", columns=["team_id", "team_display_name", "athlete_id", "display_name"])
ros = ros.dropna(subset=["team_id", "athlete_id"]); ros["team_id"] = ros.team_id.astype(int); ros["athlete_id"] = ros.athlete_id.astype(int)

# last season's production per player (main team = most minutes), for teams without 3 games yet
if len(P):
    P["tot_min"] = P.mpg * P.gp
    last = P.sort_values("tot_min").drop_duplicates("athlete_id", keep="last").set_index("athlete_id")
else:
    last = pd.DataFrame(columns=["gs", "mpg", "gp", "tg"])

teams, n_cur = {}, 0
for tid, tr in ros.groupby("team_id"):
    name = tr.team_display_name.iloc[0]
    ct = C[C.team_id == tid] if len(C) else pd.DataFrame()
    tg = int(ct.tg.iloc[0]) if len(ct) else 0
    if tg >= MISS["min_games"]:
        n_cur += 1
        d = ct.assign(reg=((ct.gp >= MISS["share"] * tg) & (ct.mpg >= MISS["min_minutes"])).astype(int))
        lst = [[int(r.athlete_id), r["name"], round(max(r.gs, 0), 1), int(r.reg), int(r.sat)] for _, r in d.iterrows()]
    else:
        lst = []
        for _, r in tr.iterrows():
            if r.athlete_id in last.index:
                l = last.loc[r.athlete_id]
                reg = int(l.gp >= MISS["share"] * l.tg and l.mpg >= MISS["min_minutes"])
                lst.append([int(r.athlete_id), r.display_name, round(max(float(l.gs), 0), 1), reg, 0])
    lst = sorted([x for x in lst if x[2] > 0], key=lambda x: -x[2])[:TOP]
    if lst: teams[str(tid)] = dict(n=name, g=tg, p=lst)

M = pd.read_csv(f"{REPO}/model.csv")
row = M[(M.target == "margin") & (M.col1.astype(str) == "Miss")]
coef = float(row.coef.iloc[0]) if len(row) else -0.110
doc = dict(updated=pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"), season=season,
           basis=f"{n_cur} teams on this season's games, the rest on last season's", coef=round(coef, 4), teams=teams)
json.dump(doc, open(OUT, "w"), separators=(",", ":"), ensure_ascii=False)
log(f"players: {len(teams)} teams, {sum(len(t['p']) for t in teams.values())} players ({n_cur} teams from this season); "
    f"coef {coef:+.3f}; {len(json.dumps(doc)) // 1024} KB")
