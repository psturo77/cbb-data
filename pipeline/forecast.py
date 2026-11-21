"""Forecast scorecard: how well the model predicts games, next to Vegas and ESPN's BPI (board/forecast).

usage: python3 forecast.py <hoopR-mbb-data/mbb dir> <repo dir> <board dir>
<repo dir>:  pregame_inputs.csv (process.py: each game's inputs as of its game day, before it was played), model.csv,
             lines.csv (ESPN/DraftKings open + close). Keeps two files here between runs:
             bpi_games.csv    - ESPN BPI's pre-game forecast for finished games (fetched once per game; lastModified kept
                                so a forecast updated after tip can be dropped);
             forecast_log.csv - the board's own live predictions, frozen at the last run before each tip.
<board dir>: slate_d*.json and board_espn.json from dashboard.py / upcoming.py (read); board_forecast.json (written).

Two sets of model numbers:
- "replay": the model's prediction for every game this season, rebuilt from pre-game ratings (walk-forward: each game
  uses only games before its day) plus the market correction fit only on EARLIER games' closing lines. The recipe
  coefficients are today's (fit on ~23,000 games, nearly all earlier seasons). This gives the season to date at once.
- "live": what the board actually showed before tip, frozen from Nov 21 2026 on. It checks the replay.
"""
import sys, os, re, glob, json, time, urllib.request, concurrent.futures as cf
from math import erf, sqrt
import numpy as np, pandas as pd

SRC, REPO, BD = sys.argv[1:4]
log = lambda *a: print(*a, file=sys.stderr, flush=True)
now = pd.Timestamp.now(tz="UTC")
PRED = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/mens-college-basketball/events/{0}/competitions/{0}/predictor"
MKT = dict(lam=1.0, lam_home=20.0, half_life=30.0)       # same settings as process.py
MKT_TOT = dict(lam=3.0, lam_shift=15.0, half_life=30.0)
MOVE_MIN = 1.5                                            # line-move test: model vs opener disagreement, points
# past seasons, whole-system walk-forward backtests (fullsys.py / bpi_test.py, Oct 28-29 2026): average miss, points
PAST = [dict(season="2024-25", n=5763, model=8.88, open=8.90, close=8.79),
        dict(season="2025-26", n=5672, model=9.03, open=9.08, close=8.94, bpi=9.18)]
Phi = lambda x: 0.5 * (1 + np.vectorize(erf)(np.asarray(x, float) / sqrt(2)))

# ------------------------------------------------------------------ inputs
season = max(int(re.search(r"_(\d{4})\.parquet$", f).group(1)) for f in glob.glob(f"{SRC}/schedules/parquet/mbb_schedule_*.parquet"))
S = pd.read_parquet(f"{SRC}/schedules/parquet/mbb_schedule_{season}.parquet")
S["game_id"] = S.game_id.astype(str); S["start"] = pd.to_datetime(S.start_date, utc=True)
S["final"] = (S.status_type_completed == True) | (S.status_type_name == "STATUS_FINAL")
for c in ("home_id", "away_id"): S[c] = pd.to_numeric(S[c], errors="coerce")
S = S.drop_duplicates("game_id").set_index("game_id")

M = pd.read_csv(f"{REPO}/model.csv")
mg = M[M.target == "margin"].set_index("col1").coef; tg = M[M.target == "total"]
coef = dict(rd=mg.get("Net", 0), hca=mg.get("HCA", 0), rest=mg.get("LastGameSerial", 0), miss=mg.get("Miss", 0),
            c=float(M[(M.target == "margin") & (M.type == "const")].coef.iloc[0]))
tco = dict(pace=float(tg[tg.type == "paceadj"].coef.iloc[0]), tmp=float(tg[tg.type == "avg"].coef.iloc[0]),
           c=float(tg[tg.type == "const"].coef.iloc[0]))
SIG = float(M[(M.target == "param") & (M.label == "sigma_margin")].coef.iloc[0])

L = pd.read_csv(f"{REPO}/lines.csv", dtype={"game_id": str}).drop_duplicates("game_id", keep="last").set_index("game_id")
L["home_id"] = pd.to_numeric(L.get("home_id"), errors="coerce")

# ------------------------------------------------------------------ ESPN BPI pre-game forecasts (cache)
BPI_F = f"{REPO}/bpi_games.csv"
B = pd.read_csv(BPI_F, dtype={"game_id": str}) if os.path.exists(BPI_F) else pd.DataFrame(columns=["game_id", "tid", "mov", "modified"])
done_ids = [g for g in S.index[S.final & (S.start.dt.year >= season - 1)] if g not in set(B.game_id)]
def get_bpi(gid):
    for a in range(3):
        try:
            req = urllib.request.Request(PRED.format(gid), headers={"User-Agent": "Mozilla/5.0 CBB-model"})
            with urllib.request.urlopen(req, timeout=20) as r: d = json.load(r)
            h = d.get("homeTeam") or {}; st = {x.get("name"): x.get("value") for x in h.get("statistics", [])}
            t = re.search(r"/teams/(\d+)", str((h.get("team") or {}).get("$ref", "")))
            return dict(game_id=gid, tid=int(t.group(1)) if t else None, mov=st.get("teampredmov"), modified=d.get("lastModified"))
        except urllib.error.HTTPError as e:
            if e.code == 404: return dict(game_id=gid, tid=None, mov=None, modified=None)
        except Exception: pass
        time.sleep(1 + a)
    return None
new = []
if done_ids:
    with cf.ThreadPoolExecutor(6) as ex:
        new = [r for r in ex.map(get_bpi, done_ids[:1500]) if r is not None]
    if new:
        B = pd.concat([B, pd.DataFrame(new)], ignore_index=True).drop_duplicates("game_id", keep="last")
        B.to_csv(BPI_F, index=False)
log(f"forecast: BPI fetched for {len(new)} of {len(done_ids)} new finished games; {B.mov.notna().sum()} in cache")
B = B.set_index("game_id")

# ------------------------------------------------------------------ freeze the board's live pre-game predictions
LOG_F = f"{REPO}/forecast_log.csv"
LG = pd.read_csv(LOG_F, dtype={"game_id": str}) if os.path.exists(LOG_F) else pd.DataFrame()
espn = json.load(open(f"{BD}/board_espn.json")).get("games", {}) if os.path.exists(f"{BD}/board_espn.json") else {}
snap = []
for f in sorted(glob.glob(f"{BD}/slate_d*.json")):
    for g in json.load(open(f)).get("games", []):
        if g.get("status") != "pre" or g.get("pm") is None or g.get("tbd"): continue
        if pd.Timestamp(g["start"]) <= now: continue
        e = espn.get(g["id"], {})
        snap.append(dict(game_id=g["id"], start=g["start"], home=g["home"], away=g["away"], hid=g.get("hid"),
                         pm=g["pm"], pt=g.get("pt"), wp=g.get("wp"), early=int(bool(g.get("early"))),
                         bpi=e.get("bpi"), sp=e.get("sp"), captured=now.strftime("%Y-%m-%dT%H:%M:%SZ")))
if snap:
    N = pd.DataFrame(snap)
    keep = LG[~LG.game_id.isin(N.game_id)] if len(LG) else LG
    LG = pd.concat([keep, N], ignore_index=True).sort_values(["start", "game_id"])
    LG.to_csv(LOG_F, index=False)
log(f"forecast: froze {len(snap)} upcoming games' live predictions; log has {len(LG)}")

# ------------------------------------------------------------------ replay: pre-game predictions for the season so far
P = pd.read_csv(f"{REPO}/pregame_inputs.csv", dtype={"game_id": str}, parse_dates=["date"])
P = P[P.game_id.isin(S.index[S.final])].copy()
s = S.loc[P.game_id]
same = (s.home_id.values == P.hid.values)
P["margin"] = np.where(same, s.home_score.values - s.away_score.values, s.away_score.values - s.home_score.values).astype(float)
P["total"] = (s.home_score.values + s.away_score.values).astype(float)
P["start"] = s.start.values
P["pm0"] = coef["rd"] * P.RatingDiff + coef["hca"] * P.HCAh + coef["rest"] * P.RestDiff + coef["miss"] * P.MissDiff + coef["c"]
P["pt0"] = tco["pace"] * P.PaceAdj + tco["tmp"] * P.AvgTempo + tco["c"]
# lines in the replay's orientation (P.hid = home). ESPN's home_id when it has one, else the schedule's home team.
l = L.reindex(P.game_id)
lh = np.where(pd.notna(l.home_id.values), l.home_id.values, s.home_id.values)
sgn = np.where(lh == P.hid.values, 1.0, np.where(lh == P.aid.values, -1.0, np.nan))
for c in ("spread_open", "spread_close"): P[c] = l[c].values * sgn
P["total_open"] = l.total_open.values; P["total_close"] = l.total_close.values
P["line"] = P.spread_close.fillna(P.spread_open); P["tline"] = P.total_close.fillna(P.total_open)
# ESPN BPI in the same orientation; dropped if ESPN last updated it after tip
b = B.reindex(P.game_id)
bt = pd.to_numeric(b.tid, errors="coerce").values; bm = pd.to_numeric(b.mov, errors="coerce").values
mod = pd.to_datetime(b.modified, utc=True, errors="coerce").values
P["bpi"] = np.where(bt == P.hid.values, bm, np.where(bt == P.aid.values, -bm, np.nan))
P.loc[pd.notna(mod) & (mod > P.start.values), "bpi"] = np.nan

# market corrections, walk-forward: each day uses only closing lines from earlier days (as the live model did)
ids = sorted(set(P.hid) | set(P.aid)); ix = {t: i for i, t in enumerate(ids)}; n = len(ids)
P["hi"] = P.hid.map(ix); P["ai"] = P.aid.map(ix)
adj = np.zeros(len(P)); tadj = np.zeros(len(P))
for day in sorted(P.date.unique()):
    past = P[P.date < day]; today = np.where(P.date.values == day)[0]; g = P.iloc[today]
    p = past.dropna(subset=["line"])
    if len(p) >= 20:
        w = 0.5 ** ((day - p.date).dt.days.values / MKT["half_life"])
        X = np.zeros((len(p), n + 1)); r = np.arange(len(p)); X[r, p.hi] = 1; X[r, p.ai] = -1; X[:, n] = 1 - p.neutral.values
        beta = np.linalg.solve(X.T @ (X * w[:, None]) + np.diag([MKT["lam"]] * n + [MKT["lam_home"]]), X.T @ ((-p.line - p.pm0).values * w))
        adj[today] = beta[g.hi] - beta[g.ai] + beta[n] * (1 - g.neutral.values)
    p = past.dropna(subset=["tline", "PaceAdj"])
    if len(p) >= 20:
        w = 0.5 ** ((day - p.date).dt.days.values / MKT_TOT["half_life"])
        X = np.zeros((len(p), n + 1)); r = np.arange(len(p)); X[r, p.hi] = 1; X[r, p.ai] += 1; X[:, n] = 1.0
        beta = np.linalg.solve(X.T @ (X * w[:, None]) + np.diag([MKT_TOT["lam"]] * n + [MKT_TOT["lam_shift"]]), X.T @ ((p.tline - p.pt0).values * w))
        tadj[today] = beta[g.hi] + beta[g.ai] + beta[n]
P["pm"] = P.pm0 + adj; P["pt"] = P.pt0 + tadj
# early season = either team had played fewer than 5 games before this one (any opponent, from the schedule)
F = S[S.final].reset_index()[["game_id", "start", "home_id", "away_id"]]
apps = pd.concat([F[["game_id", "start", "home_id"]].rename(columns={"home_id": "t"}),
                  F[["game_id", "start", "away_id"]].rename(columns={"away_id": "t"})]).sort_values("start")
apps["k"] = apps.groupby("t").cumcount()   # games before this one
k = apps.set_index(["game_id", "t"]).k
P["early"] = [min(k.get((g, h), 0), k.get((g, a), 0)) < 5 for g, h, a in zip(P.game_id, P.hid, P.aid)]

# ESPN opening lines sometimes have the wrong sign (Oct 30 2026 finding): leave those games out of every spread comparison
bad = (P.spread_open.notna() & P.spread_close.notna() &
       (((P.spread_open - P.spread_close).abs() >= 8) |
        ((P.spread_open * P.spread_close < 0) & (P.spread_open.abs() >= 3) & (P.spread_close.abs() >= 3))))
P["bad_line"] = bad
P.drop(columns=["hi", "ai"]).to_csv(f"{REPO}/forecast_replay.csv", index=False)

# ------------------------------------------------------------------ scorecard
r2 = lambda x: None if x is None or x != x else round(float(x), 2)
def acc(E, cols):
    """average miss (points), median miss and % right winner for each prediction column (home margin), same games"""
    out = dict(n=int(len(E)))
    for c in cols:
        e = (E.margin - E[c]).abs(); nz = E[c] != 0
        out[c] = dict(mae=r2(e.mean()), med=r2(e.median()), win=r2(100 * (np.sign(E[c][nz]) == np.sign(E.margin[nz])).mean()))
    return out
P["open_m"] = -P.spread_open; P["close_m"] = -P.spread_close
E = P[P.pm.notna() & P.open_m.notna() & P.close_m.notna() & ~P.bad_line]
EB = E[E.bpi.notna()]
spread = dict(all=acc(E, ["pm", "open_m", "close_m"]), early=acc(E[E.early], ["pm", "open_m", "close_m"]),
              later=acc(E[~E.early], ["pm", "open_m", "close_m"]),
              bpi_all=acc(EB, ["pm", "bpi", "open_m", "close_m"]), bpi_later=acc(EB[~EB.early], ["pm", "bpi", "open_m", "close_m"]),
              closer_than_open=r2(100 * ((E.margin - E.pm).abs() < (E.margin - E.open_m).abs()).mean()),
              closer_than_close=r2(100 * ((E.margin - E.pm).abs() < (E.margin - E.close_m).abs()).mean()),
              closer_than_bpi=r2(100 * ((EB.margin - EB.pm).abs() < (EB.margin - EB.bpi).abs()).mean()) if len(EB) else None,
              dropped_bad_lines=int(bad.sum()))

def moves(E, c):
    """when a forecast disagrees with the opener by MOVE_MIN+ points: did the line then move toward it?"""
    d = E[c] - E.open_m; mv = E.close_m - E.open_m; X = E[d.abs() >= MOVE_MIN]
    if not len(X): return dict(n=0)
    dd = np.sign(X[c] - X.open_m); m = (X.close_m - X.open_m) * dd
    tiers = {}
    for lo, hi, name in ((MOVE_MIN, 3, "1.5-3"), (3, 99, "3+")):
        T = X[(d.loc[X.index].abs() >= lo) & (d.loc[X.index].abs() < hi)]; mt = (T.close_m - T.open_m) * np.sign(T[c] - T.open_m)
        tiers[name] = dict(n=int(len(T)), toward=r2(100 * (mt > 0).mean()) if len(T) else None, avg=r2(mt.mean()) if len(T) else None)
    return dict(n=int(len(X)), toward=r2(100 * (m > 0).mean()), against=r2(100 * (m < 0).mean()), same=r2(100 * (m == 0).mean()),
                avg=r2(m.mean()), tiers=tiers)
linemove = dict(model=moves(E, "pm"), bpi=moves(EB, "bpi"), model_on_bpi_games=moves(EB, "pm"), min=MOVE_MIN)

C = P[P.pm.notna() & (P.margin != 0)].copy()
C["wp"] = Phi(C.pm / SIG); fav = np.maximum(C.wp, 1 - C.wp); won = np.where(C.wp >= 0.5, C.margin > 0, C.margin < 0)
cal = []
for lo, hi in ((.5, .6), (.6, .7), (.7, .8), (.8, .9), (.9, .97), (.97, 1.01)):
    sel = (fav >= lo) & (fav < hi)
    if sel.sum(): cal.append(dict(lo=lo, hi=min(hi, 1.0), n=int(sel.sum()), pred=r2(100 * fav[sel].mean()), won=r2(100 * won[sel].mean())))
CB = C[C.spread_close.notna() & ~C.bad_line]
brier = dict(n=int(len(CB)), model=round(float(((Phi(CB.pm / SIG) - (CB.margin > 0)) ** 2).mean()), 4),
             close=round(float(((Phi(-CB.spread_close / SIG) - (CB.margin > 0)) ** 2).mean()), 4)) if len(CB) else None

T = P[P.pt.notna() & P.total_open.notna() & P.total_close.notna() & ((P.total_open - P.total_close).abs() < 15)]
totals = dict(n=int(len(T)), model=r2((T.total - T.pt).abs().mean()), open=r2((T.total - T.total_open).abs().mean()),
              close=r2((T.total - T.total_close).abs().mean()))

E2 = E.assign(week=(E.date - pd.to_timedelta(E.date.dt.weekday, unit="D")).dt.strftime("%Y-%m-%d"))
weeks = [dict(week=w, n=int(len(x)), model=r2((x.margin - x.pm).abs().mean()), open=r2((x.margin - x.open_m).abs().mean()),
              close=r2((x.margin - x.close_m).abs().mean()), early=r2(100 * x.early.mean())) for w, x in E2.groupby("week")]

# live (frozen) predictions once their games are final
live = dict(n=0, since=None)
if len(LG):
    G = LG[LG.game_id.isin(S.index[S.final])].copy()
    if len(G):
        sg = S.loc[G.game_id]
        G["margin"] = (sg.home_score.values - sg.away_score.values).astype(float)   # slates use the schedule's home team
        lg = L.reindex(G.game_id); lh = np.where(pd.notna(lg.home_id.values), lg.home_id.values, sg.home_id.values)
        G["close_m"] = -lg.spread_close.values * np.where(lh == sg.home_id.values, 1.0, np.where(lh == sg.away_id.values, -1.0, np.nan))
        G = G[G.close_m.notna()]
        R = P.set_index("game_id")
        rp = R.pm.reindex(G.game_id).values * np.where(R.hid.reindex(G.game_id).values == sg.loc[G.game_id].home_id.values, 1, -1)
        G["replay"] = rp
        live = dict(n=int(len(G)), since=str(LG.start.min())[:10], model=r2((G.margin - G.pm).abs().mean()),
                    close=r2((G.margin - G.close_m).abs().mean()), replay=r2((G.margin - G.replay).abs().mean()),
                    agree=r2((G.pm - G.replay).abs().mean()))

card = dict(updated=now.strftime("%Y-%m-%dT%H:%M:%SZ"), season=f"{season-1}-{str(season)[2:]}",
            games=int(len(P)), through=str(P.date.max().date()) if len(P) else None, sigma=round(SIG, 2),
            spread=spread, linemove=linemove, calibration=cal, brier=brier, totals=totals, weeks=weeks, live=live, past=PAST,
            notes=["Replay: each game's prediction uses only games played before its day; the market correction uses only earlier days' closing lines.",
                   "Lines: DraftKings via ESPN. Games with an obviously broken ESPN opener are left out of spread comparisons.",
                   "BPI: ESPN's own pre-game projected margin, dropped when ESPN updated it after tip."])
json.dump(card, open(f"{BD}/board_forecast.json", "w"), separators=(",", ":"))
a = spread["all"]
log(f"forecast: {a['n']} games: avg miss model {a['pm']['mae']} | opener {a['open_m']['mae']} | close {a['close_m']['mae']}; "
    f"BPI games {spread['bpi_all']['n']}: model {spread['bpi_all']['pm']['mae']} vs BPI {spread['bpi_all']['bpi']['mae']}; "
    f"line moved toward model {linemove['model'].get('toward')}% of {linemove['model'].get('n')}")
