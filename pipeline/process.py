"""CBB model pipeline (v3): ratings engine + model fit, published as data files for the Excel workbook.

usage: python3 process.py <hoopR-mbb-data/mbb dir> <output dir>
needs, for every season YYYY >= 2021 that exists:  team_box/parquet/team_box_YYYY.parquet,
      schedules/parquet/mbb_schedule_YYYY.parquet, player_box/parquet/player_box_YYYY.parquet
and, for an upcoming season with no games yet:    rosters/parquet/rosters_YYYY.parquet

Model (tuned on 2021-22..2024-25, tested out of sample on 2025-26 against ESPN lines, Oct 2026):
  * Team rating = ridge fit of point margin (home court per team, shrunk to league average), no recency weighting
    and no blowout cap (both made predictions worse). Prior each season from last season's rating, returning
    minutes and incoming-transfer quality (roster-based preseason ratings).
  * Spread = a*(rating diff) + b*(team home court, 0 if neutral) + c*(rest-day diff, capped at 2) + d
  * Totals from per-100 offense/defense/tempo ratings with an 80-day recency half-life:
    total = e*pace-adjusted points + f*avg tempo + g
  * Every training row uses only games before that game day.
  * Market correction (Oct 2026): each team's average gap between the market's closing line and the model's own
    pre-game number in this season's earlier games (ridge, lambda 1, 30-day half-life, shrunk to 0 = trust the model).
    Added to the spread as two fixed terms (MktAdj team difference, MktHome home court). Needs <output dir>/lines.csv
    from lines.py; without it the corrections are 0. In the 2025-26 walk-forward test it doubled the model's value
    against opening lines (0.26 -> 0.56 per point) and matched the closing line's accuracy from December on.
  * Missing regulars (Oct 2026): Miss = summed average Game Score of a team's regulars (played 60%+ of its games,
    15+ minutes) who sat its most recent game, assumed still out. Fitted in the spread regression as MissDiff
    (home - away). 2022-25 fit about -0.10 points per Game Score point; held up on 2025-26 (-0.13), and the
    market prices only about half of it (closing line).
  * Preseason prior by PRODUCTION (Oct 2026): rating = c0 + c1*last + c2*last*RP + c3*TQp + c4*TVl + c5*RETv + c6*LOST
    + c7*program (avg rating 2-3 seasons back) + c8*max(last-10,0)*(1-RP) (elite programs reload - without these two the
    prior underrated 20+ teams by ~3 pts); leave-one-season-out error 4.50 -> 4.06, no bias by tier.
    RP = share of last season's team Game Score returning, TQp = incoming transfers' Game Score share x old team rating,
    TVl = transfers' Game Score per game x old team level (0-1), RETv / LOST = returning / departed Game Score per game.
    Then blended 50/50 with ESPN's preseason BPI when bpi_preseason_YYYY.csv exists (see BPI_W).
    Leave-one-season-out: prior error 4.50 -> 4.12 pts (2026 from the preseason roster: 4.71 -> 4.32); Nov 3-23 2025
    games from preseason ratings alone: MAE 10.35 -> 10.00.
Outputs: teams.csv, model.csv (the recipe the workbook follows), games.csv, meta.json
"""
import sys, os, re, glob, json
import numpy as np
import pandas as pd
import scipy.sparse as sp

SRC = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/data_src/hoopr/mbb"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/home/claude/v3"
FIRST = 2021                     # warm-up season (prior source); training rows start the season after
MIN_TEAM_GAMES = 15
MIN_PRIOR = 5                    # both teams need this many prior games for a training row
REST_CAP = 2
CFG = dict(lam=4.0, lam_h=30.0, carry=0.85, carry_h=0.5, lam_eff=2.0, half_life_tot=80.0)
BPI_W = 0.5   # preseason prior = 50% ours + 50% ESPN preseason BPI (bpi_preseason_YYYY.csv in the output dir, from bpi.py)
MKT = dict(lam=1.0, lam_home=20.0, half_life=30.0)      # market correction (see docstring)
MKT_TOT = dict(lam=3.0, lam_shift=15.0, half_life=30.0)  # totals version: corrections add (home + away)
EPOCH = pd.Timestamp("1899-12-30")
log = lambda *a: print(*a, file=sys.stderr, flush=True)


def season_files(kind, pattern):
    out = []
    for f in glob.glob(f"{SRC}/{kind}/parquet/{pattern}_*.parquet"):
        m = re.search(r"_(\d{4})\.parquet$", f)
        if m and int(m.group(1)) >= FIRST: out.append(int(m.group(1)))
    return sorted(out)


# ================================================================== loading
def load_season(season, prev_d1=frozenset()):
    t = pd.read_parquet(f"{SRC}/team_box/parquet/team_box_{season}.parquet")
    t["team_id"] = t.team_id.astype(int); t["opponent_team_id"] = t.opponent_team_id.astype(int)
    counts = t.team_id.value_counts()
    d1 = set(counts[counts >= MIN_TEAM_GAMES].index) | (set(prev_d1) & set(counts.index))
    t = t[t.team_id.isin(d1) & t.opponent_team_id.isin(d1)].copy()
    t = t[(t.field_goals_attempted > 0) & t.team_score.notna() & t.opponent_team_score.notna()]
    t = t[t.game_id.map(t.game_id.value_counts()) == 2]
    try:
        s = pd.read_parquet(f"{SRC}/schedules/parquet/mbb_schedule_{season}.parquet")[["game_id", "neutral_site"]]
        nm = dict(zip(s.game_id.astype(str), s.neutral_site.astype(bool)))
    except Exception:
        nm = {}
    t["neutral"] = t.game_id.astype(str).map(nm).fillna(False).astype(bool)
    t["date"] = pd.to_datetime(t.game_date)
    t["poss"] = t.field_goals_attempted - t.offensive_rebounds + t.turnovers + 0.475 * t.free_throws_attempted
    t["game_poss"] = t.game_id.map(t.groupby("game_id").poss.mean())
    opp = t[["game_id", "team_id", "total_rebounds", "turnovers"]].rename(
        columns={"team_id": "opponent_team_id", "total_rebounds": "opp_reb", "turnovers": "opp_to"})
    t = t.merge(opp, on=["game_id", "opponent_team_id"])
    h = t[t.team_home_away == "home"]
    g = pd.DataFrame({"game_id": h.game_id.astype(str).values, "date": h.date.values, "hid": h.team_id.values,
                      "aid": h.opponent_team_id.values, "neutral": h.neutral.values.astype(int),
                      "hs": h.team_score.values.astype(float), "as_": h.opponent_team_score.values.astype(float)})
    g["margin"] = g.hs - g.as_; g["total"] = g.hs + g.as_
    return t, g.sort_values(["date", "game_id"]).reset_index(drop=True), d1


_mins = {}
def minutes(season):
    """Each player's minutes and main team in a season (from player box scores)."""
    if season not in _mins:
        f = f"{SRC}/player_box/parquet/player_box_{season}.parquet"
        if not os.path.exists(f): return None
        p = pd.read_parquet(f, columns=["athlete_id", "team_id", "minutes", "did_not_play"])
        p = p[~p.did_not_play.fillna(False)].dropna(subset=["minutes", "athlete_id", "team_id"])
        p["athlete_id"] = p.athlete_id.astype(int); p["team_id"] = p.team_id.astype(int)
        agg = p.groupby(["athlete_id", "team_id"]).minutes.sum().reset_index()
        _mins[season] = agg.sort_values("minutes").drop_duplicates("athlete_id", keep="last")
    return _mins[season]


MISS = dict(share=0.6, min_minutes=15.0, min_games=3, cap=25.0)
def absences(season):
    """Missing-regulars value per (game_id, team_id) before each game, and each team's value now (after its last game)."""
    f = f"{SRC}/player_box/parquet/player_box_{season}.parquet"
    if not os.path.exists(f): return {}, {}
    p = pd.read_parquet(f, columns=["game_id", "game_date", "team_id", "athlete_id", "minutes", "did_not_play", "points",
                                    "field_goals_made", "field_goals_attempted", "free_throws_made", "free_throws_attempted",
                                    "offensive_rebounds", "defensive_rebounds", "steals", "assists", "blocks", "fouls", "turnovers"])
    p = p[p.athlete_id.notna() & p.team_id.notna()].copy()
    p["team_id"] = p.team_id.astype(int); p["game_id"] = p.game_id.astype(str)
    p["min"] = p.minutes.fillna(0); p.loc[p.did_not_play == True, "min"] = 0
    v = lambda c: p[c].fillna(0)
    p["gs"] = (v("points") + 0.4 * v("field_goals_made") - 0.7 * v("field_goals_attempted") - 0.4 * (v("free_throws_attempted") - v("free_throws_made"))
               + 0.7 * v("offensive_rebounds") + 0.3 * v("defensive_rebounds") + v("steals") + 0.7 * v("assists") + 0.7 * v("blocks")
               - 0.4 * v("fouls") - v("turnovers"))
    p["date"] = pd.to_datetime(p.game_date)
    before, now = {}, {}
    for tid, tp in p.groupby("team_id"):
        gids = tp[["game_id", "date"]].drop_duplicates().sort_values(["date", "game_id"]).game_id.tolist()
        M = tp.pivot_table(index="athlete_id", columns="game_id", values="min", aggfunc="sum").reindex(columns=gids).fillna(0).values
        G = tp.pivot_table(index="athlete_id", columns="game_id", values="gs", aggfunc="sum").reindex(columns=gids).fillna(0).values
        def miss_at(k):          # value of regulars out, before the team's k-th game (0-based), from games 0..k-1
            if k < MISS["min_games"] or (M[:, k - 1] > 0).sum() < 5: return 0.0   # last box score incomplete: no call
            pm, pg = M[:, :k], G[:, :k]; played = pm > 0; gp = played.sum(1)
            avg_min = np.where(gp > 0, pm.sum(1) / np.maximum(gp, 1), 0)
            avg_gs = np.where(gp > 0, (pg * played).sum(1) / np.maximum(gp, 1), 0)
            out = (gp >= MISS["share"] * k) & (avg_min >= MISS["min_minutes"]) & (M[:, k - 1] == 0)
            return float(min(np.clip(avg_gs[out], 0, None).sum(), MISS["cap"]))
        for k, gid in enumerate(gids): before[(gid, tid)] = miss_at(k)
        now[tid] = miss_at(len(gids))
    return before, now


def roster(season):
    f = f"{SRC}/rosters/parquet/rosters_{season}.parquet"
    if not os.path.exists(f): return None
    r = pd.read_parquet(f, columns=["athlete_id", "team_id", "team_display_name"]).dropna(subset=["athlete_id", "team_id"])
    r["athlete_id"] = r.athlete_id.astype(int); r["team_id"] = r.team_id.astype(int)
    return r


_prod = {}
def production(season):
    """Each player's minutes, total Game Score and Game Score per team game in a season (main team only)."""
    if season not in _prod:
        f = f"{SRC}/player_box/parquet/player_box_{season}.parquet"
        if not os.path.exists(f): return None
        p = pd.read_parquet(f, columns=["athlete_id", "team_id", "game_id", "minutes", "did_not_play", "points", "field_goals_made",
                                        "field_goals_attempted", "free_throws_made", "free_throws_attempted", "offensive_rebounds",
                                        "defensive_rebounds", "steals", "assists", "blocks", "fouls", "turnovers"])
        p = p[~p.did_not_play.fillna(False)].dropna(subset=["athlete_id", "team_id", "minutes"])
        v = lambda c: p[c].fillna(0)
        p["gs"] = (v("points") + 0.4 * v("field_goals_made") - 0.7 * v("field_goals_attempted") - 0.4 * (v("free_throws_attempted") - v("free_throws_made"))
                   + 0.7 * v("offensive_rebounds") + 0.3 * v("defensive_rebounds") + v("steals") + 0.7 * v("assists") + 0.7 * v("blocks")
                   - 0.4 * v("fouls") - v("turnovers"))
        p["athlete_id"] = p.athlete_id.astype(int); p["team_id"] = p.team_id.astype(int)
        a = p.groupby(["athlete_id", "team_id"]).agg(minutes=("minutes", "sum"), gs=("gs", "sum")).reset_index()
        a["gs_pg"] = a.gs / a.team_id.map(p.groupby("team_id").game_id.nunique())
        _prod[season] = a.sort_values("minutes").drop_duplicates("athlete_id", keep="last")
    return _prod[season]


def prod_features(final_prev_r, prod_prev, roster_now):
    """Production-based roster features (see docstring) for each team in roster_now."""
    tot_m = prod_prev.groupby("team_id").minutes.sum(); tot_g = prod_prev.groupby("team_id").gs.sum()
    m = roster_now[["athlete_id", "team_id"]].merge(prod_prev, on="athlete_id", how="left", suffixes=("", "_prev"))
    same = m[m.team_id == m.team_id_prev]; tr = m[m.team_id_prev.notna() & (m.team_id != m.team_id_prev)].copy()
    lo, hi = (final_prev_r.min(), final_prev_r.max()) if len(final_prev_r) else (0.0, 1.0)
    old_r = tr.team_id_prev.map(final_prev_r).fillna(lo)
    tr["q_gs"] = (tr.gs / tr.team_id_prev.map(tot_g)).clip(-0.2, 1) * old_r
    tr["v_lvl"] = tr.gs_pg * (old_r - lo) / max(hi - lo, 1e-9)
    idx = sorted(set(roster_now.team_id)); F = pd.DataFrame(index=idx)
    F["RM"] = (same.groupby("team_id").minutes.sum().reindex(idx).fillna(0) / tot_m.reindex(idx)).clip(0, 1)
    F["RP"] = (same.groupby("team_id").gs.sum().reindex(idx).fillna(0) / tot_g.reindex(idx)).clip(0, 1.2)
    F["TQp"] = tr.groupby("team_id").q_gs.sum().reindex(idx).fillna(0)
    F["TVl"] = tr.groupby("team_id").v_lvl.sum().reindex(idx).fillna(0)
    F["RETv"] = same.groupby("team_id").gs_pg.sum().reindex(idx).fillna(0)
    F["LOST"] = prod_prev[~prod_prev.athlete_id.isin(set(same.athlete_id))].groupby("team_id").gs_pg.sum().reindex(idx).fillna(0)
    return F


_FIN = {}      # full-season ratings by season (set by fit_prior_coef), for the program-strength term
def program(ids, season, last):
    """Program strength: average full-season rating two and three seasons back (falls back to last season)."""
    out = []
    for x, l in zip(ids, last):
        v = [_FIN[y]["r"][x] for y in (season - 2, season - 3) if y in _FIN and x in _FIN[y]["r"]]
        out.append(np.mean(v) if v else l)
    return np.asarray(out, float)


def prior_X(last, F, prog):
    """Design matrix for the production prior: const, last, last*RP, TQp, TVl, RETv, LOST, program, top-team lost production."""
    top_lost = np.maximum(last - 10, 0) * (1 - np.clip(F.RP.values, 0, 1))
    return np.c_[np.ones(len(last)), last, last * F.RP.values, F.TQp.values, F.TVl.values, F.RETv.values, F.LOST.values, prog, top_lost]


def roster_features(final_prev_r, minutes_prev, roster_now):
    """RM = share of last season's team minutes returning; TQ = incoming transfers' minutes share x old team rating."""
    tot = minutes_prev.groupby("team_id").minutes.sum()
    m = roster_now[["athlete_id", "team_id"]].merge(minutes_prev, on="athlete_id", how="left", suffixes=("", "_prev"))
    same = m[m.team_id == m.team_id_prev].groupby("team_id").minutes.sum()
    tr = m[m.team_id_prev.notna() & (m.team_id != m.team_id_prev)].copy()
    tr["share"] = tr.minutes / tr.team_id_prev.map(tot)
    lo = final_prev_r.min() if len(final_prev_r) else 0.0
    tr["q"] = tr.share * tr.team_id_prev.map(final_prev_r).fillna(lo)
    idx = sorted(set(roster_now.team_id))
    out = pd.DataFrame(index=idx)
    out["RM"] = (same.reindex(idx).fillna(0) / tot.reindex(idx)).clip(0, 1)
    out["TQ"] = tr.groupby("team_id").q.sum().reindex(idx).fillna(0)
    return out


# ================================================================== solvers
BASE_W = 300.0    # current season only: league-wide levels (home court, scoring, tempo) start from last season's, worth ~300 games
def solve_margin(pg, idx, day, prior_r, prior_h, base=None):
    n = len(idx); m = len(pg)
    hi = pg.hid.map(idx).values; ai = pg.aid.map(idx).values
    home = (1 - pg.neutral.values).astype(float)
    k = 1 + 2 * n; r_ = np.arange(m)
    A = sp.csr_matrix((np.r_[home, np.ones(m), -np.ones(m), home],
                       (np.r_[r_, r_, r_, r_], np.r_[np.zeros(m, int), 1 + hi, 1 + ai, 1 + n + hi])), shape=(m, k))
    M = (A.T @ A).toarray(); b = A.T @ pg.margin.values.astype(float)
    lam = np.r_[0.0 if base is None else BASE_W, np.full(n, CFG["lam"]), np.full(n, CFG["lam_h"])]
    pri = np.r_[0.0 if base is None else base["hca"], prior_r, prior_h]
    M[np.diag_indices(k)] += lam + 1e-9
    sol = np.linalg.solve(M, b + lam * pri)
    r = sol[1:1 + n]
    return r - r.mean(), sol[0] + sol[1 + n:], sol[0]


def solve_eff(pt, idx, day, prior_o, prior_d, prior_t, base=None):
    n = len(idx)
    ti = pt.team_id.map(idx).values; oj = pt.opponent_team_id.map(idx).values
    loc = np.where(pt.neutral, 0, np.where(pt.team_home_away == "home", 1, -1)).astype(float)
    y = (100 * pt.team_score / pt.poss).values
    w = 0.5 ** ((day - pt.date).dt.days.values / CFG["half_life_tot"])
    m = len(y); r_ = np.arange(m); k = 2 + 2 * n
    A = sp.csr_matrix((np.r_[np.ones(m), loc, np.ones(m), -np.ones(m)],
                       (np.r_[r_, r_, r_, r_], np.r_[np.zeros(m, int), np.ones(m, int), 2 + ti, 2 + n + oj])), shape=(m, k))
    Aw = A.multiply(w[:, None]).tocsr()
    M = (A.T @ Aw).toarray(); b = Aw.T @ y
    lam = np.r_[(0.0, 0.0) if base is None else (2 * BASE_W, 2 * BASE_W), np.full(2 * n, CFG["lam_eff"])]
    M[np.diag_indices(k)] += lam + 1e-9
    sol = np.linalg.solve(M, b + lam * np.r_[(0, 0) if base is None else (base["mu"], base["loc"]), prior_o, prior_d])
    mu, o, d = sol[0], sol[2:2 + n], sol[2 + n:]
    hr = pt[pt.team_home_away == "home"]
    gi = hr.team_id.map(idx).values; gj = hr.opponent_team_id.map(idx).values; p = hr.game_poss.values
    wt = 0.5 ** ((day - hr.date).dt.days.values / CFG["half_life_tot"])
    mt = len(p); r2 = np.arange(mt)
    B = sp.csr_matrix((np.ones(3 * mt), (np.r_[r2, r2, r2], np.r_[np.zeros(mt, int), 1 + gi, 1 + gj])), shape=(mt, n + 1))
    Bw = B.multiply(wt[:, None]).tocsr()
    Mt = (B.T @ Bw).toarray(); bt = Bw.T @ p
    lt = np.r_[0.0 if base is None else BASE_W, np.full(n, CFG["lam_eff"])]
    Mt[np.diag_indices(n + 1)] += lt + 1e-9
    st = np.linalg.solve(Mt, bt + lt * np.r_[0 if base is None else base["tmp"], prior_t])
    solve_eff.loc = float(sol[1]); solve_eff.tmp = float(st[0])
    return mu + o, mu - d, st[0] + st[1:], o, d, st[1:], mu


def box_avgs(pt):
    g = pt.groupby("team_id")
    return pd.DataFrame({"ppg": g.team_score.mean(), "opp": g.opponent_team_score.mean(),
                         "fg": g.field_goals_made.sum() / g.field_goals_attempted.sum(),
                         "fg3": g.three_point_field_goals_made.sum() / g.three_point_field_goals_attempted.sum(),
                         "reb": g.total_rebounds.mean() - g.opp_reb.mean(), "to": g.opp_to.mean() - g.turnovers.mean(),
                         "gp": g.size()})


# ================================================================== priors
def make_prior(ids, prev, season, prior_coef):
    n = len(ids)
    if prev is None:
        z = np.zeros(n); return z, z, z, z, z
    last = np.array([prev["r"].get(x, np.nan) for x in ids])
    last = np.where(np.isnan(last), np.nanpercentile(last, 10), last)
    pr_r = CFG["carry"] * last
    if prior_coef is not None:
        mp = minutes(season - 1)
        now = minutes(season)
        if now is None or len(now) < 1000:          # season not under way yet -> use the published roster
            rr = roster(season); now = rr if rr is not None else now
        pp = production(season - 1)
        if len(prior_coef) == 9 and pp is not None and now is not None:
            F = prod_features(pd.Series(prev["r"]), pp, now).reindex(ids)
            F["RP"] = F.RP.fillna(F.RP.median()); F = F.fillna(0.0)
            pr_r = prior_X(last, F, program(ids, season, last)) @ np.asarray(prior_coef)
        elif len(prior_coef) == 4 and mp is not None and now is not None:
            rf = roster_features(pd.Series(prev["r"]), mp, now)
            rm = rf.RM.reindex(ids); rm = rm.fillna(rm.median()).values
            tq = rf.TQ.reindex(ids).fillna(0).values
            c = prior_coef
            pr_r = c[0] + c[1] * last + c[2] * last * rm + c[3] * tq
    bf = f"{OUT}/bpi_preseason_{season}.csv"
    if os.path.exists(bf):
        # ESPN's preseason BPI, saved before the season's first game. 2025-26 test: blending it 50/50 with our
        # preseason ratings cut early-season error (Nov 3-23: 9.91 -> 9.69 pts/game); later in the season the model's
        # own results take over, as the ratings move away from the prior.
        B = pd.read_csv(bf).set_index("team_id").bpi
        b = pd.Series(ids).map(B).values.astype(float); ok = ~np.isnan(b)
        if ok.sum() >= 300:
            b = b - b[ok].mean() + np.mean(pr_r[ok])
            pr_r = np.where(ok, (1 - BPI_W) * pr_r + BPI_W * b, pr_r)
    pr_h = np.array([prev["hdev"].get(x, 0.0) for x in ids]) * CFG["carry_h"]
    pr_o = np.array([CFG["carry"] * prev["o"].get(x, 0) for x in ids])
    pr_d = np.array([CFG["carry"] * prev["d"].get(x, 0) for x in ids])
    pr_t = np.array([CFG["carry"] * prev["t"].get(x, 0) for x in ids])
    return pr_r, pr_h, pr_o, pr_d, pr_t


def finals_only(data, prior_coef):
    """Full-season ratings for each season (no walk-forward) - used to fit the roster prior."""
    prev, finals = None, {}
    for y in sorted(data):
        t, g, d1 = data[y]
        ids = sorted(d1 & (set(g.hid) | set(g.aid))); idx = {x: i for i, x in enumerate(ids)}
        pr_r, pr_h, *_ = make_prior(ids, prev, y, prior_coef)
        r, hca_t, hca = solve_margin(g, idx, g.date.max(), pr_r, pr_h)
        prev = dict(r=dict(zip(ids, r)), hdev=dict(zip(ids, hca_t - hca)), o={}, d={}, t={})
        finals[y] = prev
    return finals


def fit_prior_coef(data, completed):
    fin = finals_only(data, None)
    _FIN.clear(); _FIN.update(fin)
    rows = []
    for y in completed:
        if y - 1 not in fin or minutes(y) is None or production(y - 1) is None: continue
        prev = pd.Series(fin[y - 1]["r"]); cur = pd.Series(fin[y]["r"])
        rf = prod_features(prev, production(y - 1), minutes(y))
        d = pd.DataFrame({"cur": cur}).join(prev.rename("last")).join(rf).dropna(subset=["cur", "last"])
        d["RP"] = d.RP.fillna(d.RP.median()); d = d.fillna(0.0)
        d["prog"] = program(list(d.index), y, d["last"].values); rows.append(d)
    if not rows: return None
    D = pd.concat(rows)
    b, *_ = np.linalg.lstsq(prior_X(D["last"].values, D, D.prog.values), D.cur, rcond=None)
    log("prior coef [const, last, last*RP, TQp, TVl, RETv, LOST, program, topLost]:", np.round(b, 3), "from", len(D), "team-seasons")
    return [float(x) for x in b]


# ================================================================== main walk-forward
seasons = season_files("team_box", "team_box")
data, prev_d1 = {}, frozenset()
for y in seasons:
    data[y] = load_season(y, prev_d1); prev_d1 = data[y][2]
    log(y, "games", len(data[y][1]))
latest = seasons[-1]
latest_done = data[latest][1].date.max() >= pd.Timestamp(f"{latest}-03-20")
completed = [y for y in seasons if y > FIRST and (y < latest or latest_done)]
prior_coef = fit_prior_coef(data, completed)

names = {}
rows, prev = [], None
cur_rows = []          # current season, every game: the model's pre-game inputs (for the market correction)
finals, prev_last = {}, {}
for y in seasons:
    t, g, d1 = data[y]
    names.update(t.groupby("team_id").team_display_name.last().to_dict())
    ids = set(d1 & (set(g.hid) | set(g.aid)))
    if y == latest and not latest_done and prev is not None:   # season under way: keep teams that haven't played yet
        ids |= set(prev["r"])
    ids = sorted(ids); idx = {x: i for i, x in enumerate(ids)}; n = len(ids)
    pr_r, pr_h, pr_o, pr_d, pr_t = make_prior(ids, prev, y, prior_coef)
    # season under way: league-wide levels lean on last season's until enough games are in (3 games on Nov 2 2026 made home court ~0)
    BASE = None
    if y == latest and not latest_done and (y - 1) in finals:
        a = finals[y - 1].attrs; BASE = dict(hca=a["hca"], mu=a["mu"], loc=a.get("loc", 0.0), tmp=a.get("tmp", float(finals[y - 1].AdjT.mean())))
    miss_before, miss_now = absences(y)
    mdiff = lambda gm: miss_before.get((str(gm.game_id), gm.hid), 0.0) - miss_before.get((str(gm.game_id), gm.aid), 0.0)
    last_play = {}
    for day, today in g.groupby("date"):
        pg = g[g.date < day]; pt = t[t.date < day]
        cnt = pd.concat([pg.hid, pg.aid]).value_counts()
        if y > FIRST and len(pg) >= 30:
            r, hca_t, hca = solve_margin(pg, idx, day, pr_r, pr_h, BASE)
            ao, ad, at, *_ = solve_eff(pt, idx, day, pr_o, pr_d, pr_t, BASE)
            bx = box_avgs(pt)
            for gm in today.itertuples(index=False):
                if cnt.get(gm.hid, 0) < MIN_PRIOR or cnt.get(gm.aid, 0) < MIN_PRIOR: continue
                hi, ai = idx[gm.hid], idx[gm.aid]
                rh = (day - last_play[gm.hid]).days if gm.hid in last_play else 99
                ra = (day - last_play[gm.aid]).days if gm.aid in last_play else 99
                H, A = bx.loc[gm.hid], bx.loc[gm.aid]
                rows.append(dict(
                    Date=day.date(), Home=names[gm.hid], Away=names[gm.aid], Neutral=gm.neutral,
                    HomeScore=int(gm.hs), AwayScore=int(gm.as_),
                    RatingDiff=r[hi] - r[ai], HCAh=hca_t[hi] * (1 - gm.neutral),
                    RestDiff=min(rh, REST_CAP) - min(ra, REST_CAP),
                    EffMarginDiff=(ao[hi] - ad[hi]) - (ao[ai] - ad[ai]),
                    PPGDiff=H.ppg - A.ppg, OppPPGDiff=H.opp - A.opp, FGDiff=H.fg - A.fg, FG3Diff=H.fg3 - A.fg3,
                    RebDiff=H.reb - A.reb, TODiff=H.to - A.to,
                    PaceAdj=(at[hi] + at[ai]) / 2 * (ao[hi] + ad[ai] + ao[ai] + ad[hi]) / 200,
                    AvgTempo=(at[hi] + at[ai]) / 2, SumPPG=H.ppg + A.ppg, SumOpp=H.opp + A.opp,
                    Season=f"{y-1}-{str(y)[2:]}", MissDiff=mdiff(gm)))
        if y == latest and not latest_done:
            if not (y > FIRST and len(pg) >= 30):      # first days of the season: the preseason ratings
                base = finals[y - 1].attrs["hca"] if (y - 1) in finals else 3.0
                r = np.asarray(pr_r) - np.mean(pr_r); hca_t = base + np.asarray(pr_h)
                if (y - 1) in finals:
                    mu0, tmu0 = finals[y - 1].attrs["mu"], float(finals[y - 1].AdjT.mean())
                    ao, ad, at = mu0 + np.asarray(pr_o), mu0 - np.asarray(pr_d), tmu0 + np.asarray(pr_t)
            for gm in today.itertuples(index=False):
                hi, ai = idx[gm.hid], idx[gm.aid]
                rh = (day - last_play[gm.hid]).days if gm.hid in last_play else 99
                ra = (day - last_play[gm.aid]).days if gm.aid in last_play else 99
                cur_rows.append(dict(game_id=str(gm.game_id), date=day, hid=gm.hid, aid=gm.aid, neutral=int(gm.neutral),
                                     RatingDiff=r[hi] - r[ai], HCAh=hca_t[hi] * (1 - gm.neutral),
                                     RestDiff=min(rh, REST_CAP) - min(ra, REST_CAP), MissDiff=mdiff(gm),
                                     PaceAdj=(at[hi] + at[ai]) / 2 * (ao[hi] + ad[ai] + ao[ai] + ad[hi]) / 200,
                                     AvgTempo=(at[hi] + at[ai]) / 2))
        for gm in today.itertuples(index=False):
            last_play[gm.hid] = day; last_play[gm.aid] = day
    end = g.date.max() + pd.Timedelta(days=1)
    r, hca_t, hca = solve_margin(g, idx, end, pr_r, pr_h, BASE)
    ao, ad, at, o, d, tp, mu = solve_eff(t, idx, end, pr_o, pr_d, pr_t, BASE)
    prev = dict(r=dict(zip(ids, r)), hdev=dict(zip(ids, hca_t - hca)), o=dict(zip(ids, o)), d=dict(zip(ids, d)),
                t=dict(zip(ids, tp)))
    bx = box_avgs(t)
    finals[y] = pd.DataFrame({"team_id": ids, "Net": r, "HCA": hca_t, "AdjO": ao, "AdjD": ad, "AdjT": at,
                              "LastGame": [last_play.get(x, prev_last.get(x, g.date.min())) for x in ids]}).set_index("team_id").join(bx)
    finals[y].attrs.update(through=str(g.date.max().date()), hca=float(hca), mu=float(mu), loc=solve_eff.loc, tmp=solve_eff.tmp)
    prev_last = finals[y].LastGame.to_dict()
    finals[y]["Miss"] = pd.Series(miss_now).reindex(finals[y].index).fillna(0.0)
    log(y, "walk-forward done; rows so far", len(rows))

# ================================================================== team table for the season being predicted
upcoming = latest + 1
if latest_done and roster(upcoming) is not None:
    # preseason: roster-based priors for the new season, before any games
    ros = roster(upcoming)
    ids = sorted(set(finals[latest].index) | set(ros.team_id[ros.team_id.isin(set(names))]))
    names.update(ros.groupby("team_id").team_display_name.last().to_dict())
    pr_r, pr_h, pr_o, pr_d, pr_t = make_prior(ids, prev, upcoming, prior_coef)
    pf = finals[latest]
    mu, hca = pf.attrs["mu"], pf.attrs["hca"]
    tmu = float(pf.AdjT.mean())
    team = pd.DataFrame({"team_id": ids, "Net": pr_r - pr_r.mean(), "HCA": hca + pr_h,
                         "AdjO": mu + pr_o, "AdjD": mu - pr_d, "AdjT": tmu + pr_t}).set_index("team_id")
    team = team.join(pf[["ppg", "opp", "fg", "fg3", "reb", "to", "LastGame"]])
    team["gp"] = 0
    team["Miss"] = 0.0
    stats_season, stats_through = upcoming, f"preseason (rosters as of {pd.Timestamp.now(tz='America/New_York').date()})"
else:
    team = finals[latest].copy(); stats_season, stats_through = latest, finals[latest].attrs["through"]
team["Team"] = team.index.map(names)
team = team.dropna(subset=["Team"])
for c in ("ppg", "opp", "fg", "fg3", "reb", "to"):
    team[c] = team[c].fillna(team[c].median())
team["LastGame"] = pd.to_datetime(team.LastGame).fillna(pd.Timestamp(f"{latest}-03-01"))
team["LastGameSerial"] = (team.LastGame - EPOCH).dt.days
team["HomeFlag"] = 1.0
team = team.drop_duplicates("Team")

# ================================================================== model fit -> recipe
G = pd.DataFrame(rows)
G["Margin"] = G.HomeScore - G.AwayScore; G["Total"] = G.HomeScore + G.AwayScore; G["HomeAdv"] = 1 - G.Neutral
G.drop(columns=["Margin", "Total", "HomeAdv"]).to_csv(f"{OUT}/games.csv", index=False)

def ols(df, y, xs):
    X = np.c_[df[xs].values, np.ones(len(df))]
    b, *_ = np.linalg.lstsq(X, df[y].values, rcond=None)
    e = df[y].values - X @ b; n, p = len(df), len(xs)
    s2 = e @ e / (n - p - 1)
    se = np.sqrt(np.diag(np.linalg.inv(X.T @ X)) * s2)
    r2 = 1 - (e @ e) / ((df[y] - df[y].mean()) ** 2).sum()
    return dict(coef=b, se=se, r2=r2, adjr2=1 - (1 - r2) * (n - 1) / (n - p - 1), rmse=float(np.sqrt(s2)), xs=xs)

TERM = {"RatingDiff": ("diff", "Net", "", "", "Team rating difference (points, home - away)"),
        "HCAh": ("home", "HCA", "", "", "Home team's home-court edge (0 if neutral)"),
        "MissDiff": ("diff", "Miss", "", "", "Missing regulars (value of players who sat the last game)"),
        "RestDiff": ("rest_diff", "LastGameSerial", "", "", f"Rest days difference (capped at {REST_CAP})"),
        "HomeAdv": ("home", "HomeFlag", "", "", "Home court (0 if neutral)"),
        "PPGDiff": ("diff", "ppg", "", "", "PPG difference"), "OppPPGDiff": ("diff", "opp", "", "", "Opp PPG difference"),
        "FGDiff": ("diff", "fg", "", "", "FG% difference"), "FG3Diff": ("diff", "fg3", "", "", "3P% difference"),
        "RebDiff": ("diff", "reb", "", "", "Rebound margin difference"), "TODiff": ("diff", "to", "", "", "Turnover margin difference"),
        "PaceAdj": ("paceadj", "AdjT", "AdjO", "AdjD", "Pace-adjusted expected points"),
        "AvgTempo": ("avg", "AdjT", "", "", "Average tempo"),
        "SumPPG": ("sum", "ppg", "", "", "Sum of PPG"), "SumOpp": ("sum", "opp", "", "", "Sum of Opp PPG")}
fits = {"spread_eff": ols(G, "Margin", ["RatingDiff", "HCAh", "RestDiff", "MissDiff"]),
        "spread_box": ols(G, "Margin", ["HomeAdv", "PPGDiff", "OppPPGDiff", "FGDiff", "FG3Diff", "RebDiff", "TODiff"]),
        "total_eff": ols(G, "Total", ["PaceAdj", "AvgTempo"]),
        "total_box": ols(G, "Total", ["SumPPG", "SumOpp"])}
sp_pick = "spread_eff" if fits["spread_eff"]["adjr2"] >= fits["spread_box"]["adjr2"] else "spread_box"
to_pick = "total_eff" if fits["total_eff"]["adjr2"] >= fits["total_box"]["adjr2"] else "total_box"

# ================================================================== market correction from this season's closing lines
def market_correction(fit):
    """Ridge fit of (closing-line home margin - model's pre-game margin) = adj[home] - adj[away] + home*(1-neutral)."""
    zero = (pd.Series(0.0, index=team.index), 0.0, 0)
    if not cur_rows or not os.path.exists(f"{OUT}/lines.csv") or not set(fit["xs"]) <= {"RatingDiff", "HCAh", "RestDiff", "MissDiff"}: return zero
    L = pd.read_csv(f"{OUT}/lines.csv", dtype={"game_id": str})
    L["line"] = L.spread_close.fillna(L.spread_open)
    if "home_id" not in L: L["home_id"] = np.nan
    C = pd.DataFrame(cur_rows).merge(L[["game_id", "line", "home_id"]].dropna(subset=["line"]), on="game_id")
    flip = C.home_id.notna() & (pd.to_numeric(C.home_id, errors="coerce") == C.aid)   # ESPN listed it the other way round
    C.loc[flip, "line"] = -C.loc[flip, "line"]
    if len(C) < 20: return zero
    C["pm"] = sum(cf * C[x] for x, cf in zip(fit["xs"], fit["coef"][:-1])) + fit["coef"][-1]
    C["res"] = -C.line - C.pm
    ids = sorted(set(C.hid) | set(C.aid) | set(team.index)); ix = {t: i for i, t in enumerate(ids)}; n = len(ids)
    asof = C.date.max() + pd.Timedelta(days=1)
    w = 0.5 ** ((asof - C.date).dt.days.values / MKT["half_life"])
    X = np.zeros((len(C), n + 1)); rr = np.arange(len(C))
    X[rr, C.hid.map(ix).values] = 1; X[rr, C.aid.map(ix).values] = -1; X[:, n] = 1 - C.neutral.values
    A = X.T @ (X * w[:, None]) + np.diag([MKT["lam"]] * n + [MKT["lam_home"]])
    beta = np.linalg.solve(A, X.T @ (C.res.values * w))
    adj = pd.Series(beta[:n], index=ids).reindex(team.index).fillna(0.0)
    log(f"market correction from {len(C)} games with lines: home {beta[n]:+.2f}, team spread sd {adj.std():.2f}, "
        f"largest {adj.abs().max():.2f}")
    return adj, float(beta[n]), len(C)

def total_correction(fit):
    """Same idea for totals: (closing total - model total) = adj[home] + adj[away] + league shift.
    2025-26 walk-forward: total MAE 14.13 -> 13.72 (closing total 13.39); still no edge against the closing total."""
    zero = (pd.Series(0.0, index=team.index), 0.0, 0)
    if not cur_rows or not os.path.exists(f"{OUT}/lines.csv") or fit["xs"] != ["PaceAdj", "AvgTempo"]: return zero
    L = pd.read_csv(f"{OUT}/lines.csv", dtype={"game_id": str})
    L["tline"] = L.total_close.fillna(L.total_open)
    C = pd.DataFrame(cur_rows).dropna(subset=["PaceAdj"]).merge(L[["game_id", "tline"]].dropna(), on="game_id")
    if len(C) < 20: return zero
    c = fit["coef"]; C["res"] = C.tline - (c[0] * C.PaceAdj + c[1] * C.AvgTempo + c[2])
    ids = sorted(set(C.hid) | set(C.aid) | set(team.index)); ix = {t: i for i, t in enumerate(ids)}; n = len(ids)
    w = 0.5 ** (((C.date.max() + pd.Timedelta(days=1)) - C.date).dt.days.values / MKT_TOT["half_life"])
    X = np.zeros((len(C), n + 1)); rr = np.arange(len(C))
    X[rr, C.hid.map(ix).values] = 1; X[rr, C.aid.map(ix).values] += 1; X[:, n] = 1.0
    A = X.T @ (X * w[:, None]) + np.diag([MKT_TOT["lam"]] * n + [MKT_TOT["lam_shift"]])
    beta = np.linalg.solve(A, X.T @ (C.res.values * w))
    adj = pd.Series(beta[:n], index=ids).reindex(team.index).fillna(0.0)
    log(f"totals correction from {len(C)} games: shift {beta[n]:+.2f}, team sd {adj.std():.2f}")
    return adj, float(beta[n]), len(C)

mkt_adj, mkt_home, mkt_n = market_correction(fits[sp_pick])
tot_adj, tot_shift, tot_n = total_correction(fits[to_pick])
team["TotAdj"] = tot_adj.round(3); team["TotShift"] = round(tot_shift, 3)
team["MktAdj"] = mkt_adj.round(3); team["MktHome"] = round(mkt_home, 3)
team["Miss"] = team["Miss"].fillna(0.0).round(2) if "Miss" in team else 0.0
cols = ["Team", "AdjO", "AdjD", "AdjT", "ppg", "opp", "fg", "fg3", "reb", "to", "gp", "Net", "HCA", "LastGameSerial", "HomeFlag",
        "MktAdj", "MktHome", "Miss", "TotAdj", "TotShift"]
team[cols].sort_values("Team").to_csv(f"{OUT}/teams.csv", index=False)

def recipe(target, fit, slots=10):
    out, extra = [], []
    for x, c, e in zip(fit["xs"], fit["coef"][:-1], fit["se"][:-1]):
        ty, c1, c2, c3, lab = TERM[x]
        (extra if x == "MissDiff" else out).append([target, ty, c1, c2, c3, REST_CAP if ty == "rest_diff" else "", float(c), float(e), lab])
    out.append([target, "const", "", "", "", "", float(fit["coef"][-1]), float(fit["se"][-1]), "Intercept"])
    out += extra
    if target == "margin":   # fixed terms, not fitted: the market's correction to the model (0 until lines exist)
        out.append([target, "diff", "MktAdj", "", "", "", 1.0, "", "Market correction (team difference, from closing lines)"])
        out.append([target, "home", "MktHome", "", "", "", 1.0, "", "Market correction to home court"])
    if target == "total" and fit["xs"] == ["PaceAdj", "AvgTempo"]:
        out.append([target, "sum", "TotAdj", "", "", "", 1.0, "", "Market correction (both teams, from closing totals)"])
        out.append([target, "avg", "TotShift", "", "", "", 1.0, "", "Market correction to the league-wide total"])
    return out + [[target, "none", "", "", "", "", 0.0, "", ""] for _ in range(slots - len(out))]

stat = lambda lab, v: ["stat", "", "", "", "", "", float(v), "", lab]
through = pd.Timestamp(max(finals[y].attrs["through"] for y in finals))
model_rows = recipe("margin", fits[sp_pick]) + recipe("total", fits[to_pick]) + [
    ["param", "", "", "", "", "", fits[sp_pick]["rmse"], "", "sigma_margin"],
    ["param", "", "", "", "", "", fits[to_pick]["rmse"], "", "sigma_total"],
    stat("n_games", len(G)), stat("data_through_serial", (through - EPOCH).days),
    stat("spread_model_is_efficiency", int(sp_pick == "spread_eff")), stat("total_model_is_efficiency", int(to_pick == "total_eff")),
    stat("spread_adjr2_efficiency", fits["spread_eff"]["adjr2"]), stat("spread_adjr2_box", fits["spread_box"]["adjr2"]),
    stat("total_adjr2_efficiency", fits["total_eff"]["adjr2"]), stat("total_adjr2_box", fits["total_box"]["adjr2"]),
    stat("spread_rmse_efficiency", fits["spread_eff"]["rmse"]), stat("spread_rmse_box", fits["spread_box"]["rmse"]),
    stat("total_rmse_efficiency", fits["total_eff"]["rmse"]), stat("total_rmse_box", fits["total_box"]["rmse"]),
    stat("market_correction_games", mkt_n), stat("total_correction_games", tot_n)]
pd.DataFrame(model_rows, columns=["target", "type", "col1", "col2", "col3", "cap", "coef", "se", "label"]).to_csv(f"{OUT}/model.csv", index=False)

lab = lambda y: f"{y-1}-{str(y)[2:]}"
meta = {"seasons": [lab(y) for y in seasons if y > FIRST], "games": int(len(G)),
        "games_by_season": {k: int(v) for k, v in G.groupby("Season").size().items()},
        "data_through": str(through.date()), "stats_season": lab(stats_season), "stats_through": stats_through,
        "teams": int(len(team)), "prior_coef": prior_coef, "engine": "v3m",
        "market_correction_games": int(mkt_n), "total_correction_games": int(tot_n)}
json.dump(meta, open(f"{OUT}/meta.json", "w"), indent=1)
log(json.dumps(meta))
log("model:", sp_pick, np.round(fits[sp_pick]["coef"], 3), "| totals:", to_pick, np.round(fits[to_pick]["coef"], 3),
    "| sigma %.2f / %.2f" % (fits[sp_pick]["rmse"], fits[to_pick]["rmse"]))
