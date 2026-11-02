"""Complete the hoopR schedule with ESPN's live scoreboard, so every game is in the model.

usage: python3 espn_schedule.py <hoopR-mbb-data/mbb dir> [days back, default 14] [days ahead, default 28]

hoopR's schedule file can lag ESPN by days (before opening night 2026 it had 96 of the 437 Division I games in the next
week). This script reads ESPN's scoreboard (all Division I games, one request per day) for today-back .. today+ahead
(US Eastern) and patches <mbb>/schedules/parquet/mbb_schedule_<latest season>.parquet IN PLACE (the local clone only):
  - adds every game hoopR is missing;
  - for games both have, takes ESPN's start time, time-known flag, neutral flag, status and final score (fresher).
Every later step (lines, dashboard slates, upcoming lines, paper trading, rec grading, props) then sees every game.
Also checks ratings coverage: finished games in the last 3 days between two Division I teams that hoopR's team box
scores don't have yet (the ratings can't learn from those until hoopR adds them).
Prints summary lines to stderr. Best effort per day: a day ESPN can't serve is skipped and named.
"""
import sys, os, re, glob, json, urllib.request
import numpy as np, pandas as pd

SRC = sys.argv[1]
BACK = int(sys.argv[2]) if len(sys.argv) > 2 else 14
AHEAD = int(sys.argv[3]) if len(sys.argv) > 3 else 28
TZ = "America/New_York"
log = lambda *a: print(*a, file=sys.stderr, flush=True)
today = pd.Timestamp.now(tz=TZ).tz_localize(None).normalize()
URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard?dates={}&groups=50&limit=500"

files = sorted(glob.glob(f"{SRC}/schedules/parquet/mbb_schedule_*.parquet"), key=lambda f: int(re.search(r"_(\d{4})\.parquet$", f).group(1)))
PATH = files[-1]; season = int(re.search(r"_(\d{4})\.parquet$", PATH).group(1))
S = pd.read_parquet(PATH)

rows, failed = [], []
for i in range(-BACK, AHEAD + 1):
    d = (today + pd.Timedelta(days=i)).strftime("%Y%m%d")
    js = None
    for attempt in range(3):
        try:
            js = json.load(urllib.request.urlopen(urllib.request.Request(URL.format(d), headers={"User-Agent": "Mozilla/5.0 CBB-model"}), timeout=30)); break
        except Exception as e:
            err = e
    if js is None: failed.append(f"{d} ({err})"); continue
    for ev in js.get("events", []):
        c = (ev.get("competitions") or [{}])[0]; cs = {x.get("homeAway"): x for x in c.get("competitors", [])}
        hm, aw = cs.get("home"), cs.get("away")
        if not hm or not aw or not ev.get("id"): continue
        st = (c.get("status") or ev.get("status") or {}).get("type", {})
        done = bool(st.get("completed"))
        sc = lambda x: float(x["score"]) if done and str(x.get("score", "")).strip() not in ("", "None") else np.nan
        note = ((c.get("notes") or [{}])[0] or {}).get("headline", "") or ""
        rows.append(dict(game_id=int(ev["id"]), start_date=ev.get("date"), time_valid=bool(c.get("timeValid", True)), neutral_site=bool(c.get("neutralSite")),
                         status_type_name=st.get("name"), status_type_completed=done,
                         home_id=int(hm["team"]["id"]), away_id=int(aw["team"]["id"]),
                         home_display_name=hm["team"].get("displayName"), away_display_name=aw["team"].get("displayName"),
                         home_location=hm["team"].get("location"), away_location=aw["team"].get("location"),
                         home_abbreviation=hm["team"].get("abbreviation"), away_abbreviation=aw["team"].get("abbreviation"),
                         home_score=sc(hm), away_score=sc(aw), notes_headline=note, conference_competition=bool(c.get("conferenceCompetition")),
                         season=season))
if failed: log(f"espn_schedule: ESPN scoreboard unavailable for {len(failed)} day(s): {', '.join(failed[:5])}")
if not rows:
    log("espn_schedule: no ESPN data - hoopR schedule left as is"); sys.exit(0)

E = pd.DataFrame(rows).drop_duplicates("game_id", keep="last").set_index("game_id")
S["game_id"] = pd.to_numeric(S.game_id, errors="coerce")
S = S.dropna(subset=["game_id"]); S["game_id"] = S.game_id.astype("int64")
have = S.game_id.isin(E.index)
upd_cols = ["start_date", "time_valid", "neutral_site", "status_type_name", "status_type_completed", "home_score", "away_score"]
S = S.set_index("game_id")
both = S.index[S.index.isin(E.index)]
changed = 0
for c in upd_cols:
    if c not in S.columns: S[c] = np.nan
    new = E.loc[both, c]
    if c in ("home_score", "away_score"):                        # keep hoopR's score unless ESPN has a final one
        new = new.where(new.notna(), S.loc[both, c])
    if c in ("start_date", "status_type_name", "home_score"):   # count games whose time, status or score moved
        o_, n_ = S.loc[both, c], new
        changed += int(((o_.astype(str) != n_.astype(str)) & ~(o_.isna() & n_.isna())).sum())
    S[c] = S[c].astype(object); S.loc[both, c] = new.astype(object).values
add = E[~E.index.isin(S.index)]
for c in add.columns:
    if c not in S.columns: S[c] = np.nan
out = pd.concat([S, add.reindex(columns=S.columns)], axis=0).reset_index().rename(columns={"index": "game_id"})
# keep column types the other scripts expect
for c in ("time_valid", "neutral_site", "status_type_completed", "conference_competition"):
    if c in out: out[c] = out[c].map(lambda v: bool(v) if pd.notna(v) else False)
for c in ("home_score", "away_score", "home_id", "away_id"):
    if c in out: out[c] = pd.to_numeric(out[c], errors="coerce")
for c in ("start_date", "status_type_name", "home_display_name", "away_display_name", "home_location", "away_location",
          "home_abbreviation", "away_abbreviation", "notes_headline"):
    if c in out: out[c] = out[c].map(lambda v: None if v is None or (isinstance(v, float) and np.isnan(v)) else str(v))
out = out.sort_values("start_date").reset_index(drop=True)
out.to_parquet(PATH, index=False)
win = E[(pd.to_datetime(E.start_date, utc=True).dt.tz_convert(TZ).dt.tz_localize(None) >= today)]
log(f"espn_schedule: ESPN lists {len(E)} games {BACK} days back to {AHEAD} ahead ({len(win)} upcoming); hoopR had {int(have.sum())}; "
    f"added {len(add)}, {changed} start-time/status/score updates; {PATH.split('/')[-1]} now {len(out)} games")

# ---- ratings coverage: recent finals between two Division I teams that hoopR's box scores don't have yet
tb = f"{SRC}/team_box/parquet/team_box_{season}.parquet"
prev_tb = f"{SRC}/team_box/parquet/team_box_{season - 1}.parquet"
if os.path.exists(prev_tb):
    d1 = set(pd.read_parquet(prev_tb, columns=["team_id"]).team_id.dropna().astype(int))
    box = set(pd.read_parquet(tb, columns=["game_id"]).game_id.astype(int)) if os.path.exists(tb) else set()
    st = pd.to_datetime(E.start_date, utc=True).dt.tz_convert(TZ).dt.tz_localize(None)
    fin = E[E.status_type_completed & (st >= today - pd.Timedelta(days=3)) & (st < today) & E.home_id.isin(d1) & E.away_id.isin(d1)]
    miss = fin[~fin.index.isin(box)]
    log(f"espn_schedule: ratings coverage, last 3 days: {len(fin) - len(miss)} of {len(fin)} Division I finals have hoopR box scores"
        + (f" - MISSING {len(miss)}: " + "; ".join(f"{r.away_display_name} at {r.home_display_name}" for r in miss.head(6).itertuples()) if len(miss) else ""))
