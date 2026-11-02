"""Build the CBB dashboard's data documents from the published model.

usage: python3 dashboard.py <hoopR-mbb-data/mbb dir> <dir with teams.csv, model.csv, meta.json> <out dir> [YYYY-MM-DD today]
writes <out>/board_meta.json, <out>/board_teams.json and <out>/slate_dYYYY-MM-DD.json for today-7 .. today+28 (US Eastern)
Predictions use exactly the recipe in model.csv (same math as the Excel workbook's Engine).
"""
import sys, os, glob, re, json
from math import erf, sqrt
import numpy as np
import pandas as pd

SRC, IN, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
TZ = "America/New_York"
today = pd.Timestamp(sys.argv[4]) if len(sys.argv) > 4 else pd.Timestamp.now(tz=TZ).tz_localize(None).normalize()
BACK, AHEAD = 7, 28
EPOCH = pd.Timestamp("1899-12-30")
os.makedirs(OUT, exist_ok=True)

teams = pd.read_csv(f"{IN}/teams.csv").set_index("Team")
if "gp" in teams: teams["gp"] = teams.gp.fillna(0)          # early season: teams that haven't played yet
model = pd.read_csv(f"{IN}/model.csv", keep_default_na=False)
meta = json.load(open(f"{IN}/meta.json"))
stat = {r.label: float(r.coef) for r in model.itertuples() if r.target in ("param", "stat")}
terms = model[model.target.isin(["margin", "total"]) & (model.type != "none")].copy()


def evaluate(target, h, a, neutral, day_serial, last_h, last_a, parts=None):
    H, A = teams.loc[h], teams.loc[a]
    s = 0.0
    for m in terms[terms.target == target].itertuples():
        c = float(m.coef)
        if m.type == "const": v = 1.0
        elif m.type == "diff": v = H[m.col1] - A[m.col1]
        elif m.type == "sum": v = H[m.col1] + A[m.col1]
        elif m.type == "avg": v = (H[m.col1] + A[m.col1]) / 2
        elif m.type == "home": v = H[m.col1] * (1 - neutral)
        elif m.type == "paceadj": v = (H[m.col1] + A[m.col1]) / 2 * (H[m.col2] + A[m.col3] + A[m.col2] + H[m.col3]) / 200
        elif m.type == "rest_diff":
            cap = float(m.cap); v = min(cap, day_serial - last_h) - min(cap, day_serial - last_a)
        else: v = 0.0
        s += c * v
        if parts is not None: parts.append(round(c * v, 2))
    return s


# ---------------------------------------------------------------- schedule for the current season
seasons = sorted(int(re.search(r"_(\d{4})\.parquet$", f).group(1)) for f in glob.glob(f"{SRC}/schedules/parquet/mbb_schedule_*.parquet"))
frames = []
for y in seasons[-2:]:
    s = pd.read_parquet(f"{SRC}/schedules/parquet/mbb_schedule_{y}.parquet")
    keep = ["game_id", "start_date", "time_valid", "neutral_site", "status_type_name", "status_type_completed",
            "home_display_name", "away_display_name", "home_id", "home_location", "away_location", "home_abbreviation", "away_abbreviation", "home_score", "away_score", "notes_headline", "conference_competition"]
    frames.append(s[[c for c in keep if c in s.columns]])
S = pd.concat(frames, ignore_index=True).drop_duplicates("game_id", keep="last")

S["start"] = pd.to_datetime(S.start_date, utc=True)
S["et"] = S.start.dt.tz_convert(TZ)
S["day"] = S.et.dt.tz_localize(None).dt.normalize()
S = S.sort_values(["start", "game_id"])

# each team's previous game date (from the schedule) -> rest days, and games played before each date
last_known = {t: EPOCH + pd.Timedelta(days=int(v)) for t, v in teams.LastGameSerial.items()}
gp_now = teams.gp.to_dict()

rows_by_day = {}
lo, hi = today - pd.Timedelta(days=BACK), today + pd.Timedelta(days=AHEAD)
window = S[(S.day >= lo) & (S.day <= hi)]
for g in window.itertuples():
    h, a = g.home_display_name, g.away_display_name
    neutral = int(bool(g.neutral_site))
    final = bool(g.status_type_completed) or g.status_type_name == "STATUS_FINAL"
    live = g.status_type_name in ("STATUS_IN_PROGRESS", "STATUS_HALFTIME", "STATUS_END_PERIOD")
    status = "final" if final else ("live" if live else ("off" if g.status_type_name in ("STATUS_POSTPONED", "STATUS_CANCELED") else "pre"))
    rec = dict(id=str(g.game_id), start=g.start.strftime("%Y-%m-%dT%H:%M:%SZ"), tbd=not bool(g.time_valid),
               home=h, away=a, hloc=(g.home_location or h), aloc=(g.away_location or a),
               hab=(g.home_abbreviation or (g.home_location or h)[:4].upper()), aab=(g.away_abbreviation or (g.away_location or a)[:4].upper()), neutral=neutral, note=(g.notes_headline or "")[:80], conf=bool(g.conference_competition),
               status=status, hid=(int(g.home_id) if pd.notna(g.home_id) else None))
    if final and pd.notna(g.home_score) and pd.notna(g.away_score):
        rec["hs"], rec["as"] = int(g.home_score), int(g.away_score)
    if h in teams.index and a in teams.index:
        # last game before this one: latest of the team's last played game and any earlier scheduled game
        def prev(team):
            earlier = S[((S.home_display_name == team) | (S.away_display_name == team)) & (S.start < g.start)]
            cands = [last_known.get(team, EPOCH)] + ([earlier.day.max()] if len(earlier) else [])
            return max(cands)
        def gp_at(team):
            upcoming = S[((S.home_display_name == team) | (S.away_display_name == team)) & (S.day >= today) & (S.start < g.start)
                         & (S.status_type_completed != True)]
            return int(gp_now.get(team, 0)) + len(upcoming)
        ph, pa = prev(h), prev(a)
        serial = (g.day - EPOCH).days
        lh, la = (ph - EPOCH).days, (pa - EPOCH).days
        mparts = []
        pm = evaluate("margin", h, a, neutral, serial, lh, la, mparts)
        pt = evaluate("total", h, a, neutral, serial, lh, la)
        wp = 0.5 * (1 + erf(pm / stat["sigma_margin"] / sqrt(2)))
        rec.update(pm=round(pm, 2), pt=round(pt, 1), wp=round(wp, 4),
                   rest_h=int((g.day - ph).days), rest_a=int((g.day - pa).days),
                   gp_h=gp_at(h), gp_a=gp_at(a), mt=mparts)   # mt: each margin term's points, in board_meta.terms order
        if "Miss" in teams.columns:
            rec["miss_h"], rec["miss_a"] = round(float(teams.at[h, "Miss"]), 1), round(float(teams.at[a, "Miss"]), 1)
        rec["early"] = rec["gp_h"] < 5 or rec["gp_a"] < 5
    else:
        rec["unrated"] = [t for t in (h, a) if t not in teams.index]
    rows_by_day.setdefault(g.day.strftime("%Y-%m-%d"), []).append(rec)

for i in range(-BACK, AHEAD + 1):
    d = (today + pd.Timedelta(days=i)).strftime("%Y-%m-%d")
    games = rows_by_day.get(d, [])
    json.dump({"date": d, "games": games, "n": len(games)}, open(f"{OUT}/slate_d{d}.json", "w"), separators=(",", ":"))

# ---------------------------------------------------------------- teams
T = teams.reset_index().sort_values("Net", ascending=False).reset_index(drop=True)
team_rows = [dict(t=r.Team, rk=i + 1, net=round(r.Net, 2), hca=round(r.HCA, 2), o=round(r.AdjO, 1), d=round(r.AdjD, 1),
                  tp=round(r.AdjT, 1), gp=int(r.gp)) for i, r in T.iterrows()]
json.dump({"teams": team_rows, "n": len(team_rows)}, open(f"{OUT}/board_teams.json", "w"), separators=(",", ":"))

# ---------------------------------------------------------------- meta
term_rows = [dict(target=m.target, type=m.type, label=m.label, coef=round(float(m.coef), 4),
                  se=(round(float(m.se), 4) if m.se != "" else None),
                  cols=[c for c in (m.col1, m.col2, m.col3) if c], cap=(float(m.cap) if m.cap != "" else None))
             for m in terms.itertuples()]
board_meta = dict(
    updated=pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"), today=today.strftime("%Y-%m-%d"),
    data_through=meta.get("data_through"), stats_season=meta.get("stats_season"), stats_through=meta.get("stats_through"),
    n_games=int(stat.get("n_games", 0)), sigma_margin=round(stat["sigma_margin"], 3), sigma_total=round(stat["sigma_total"], 3),
    terms=term_rows,
    fit=dict(spread_adjr2=round(stat.get("spread_adjr2_efficiency", 0), 4), spread_adjr2_box=round(stat.get("spread_adjr2_box", 0), 4),
             total_adjr2=round(stat.get("total_adjr2_efficiency", 0), 4), total_adjr2_box=round(stat.get("total_adjr2_box", 0), 4)),
    days=[(today + pd.Timedelta(days=i)).strftime("%Y-%m-%d") for i in range(-BACK, AHEAD + 1)],
    engine=meta.get("engine", ""))
json.dump(board_meta, open(f"{OUT}/board_meta.json", "w"), separators=(",", ":"))
print(f"slate days {BACK} back / {AHEAD} ahead of {today.date()}: " +
      ", ".join(f"{k[5:]}={len(v)}" for k, v in sorted(rows_by_day.items())), file=sys.stderr)
