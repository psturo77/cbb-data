"""Save ESPN's preseason BPI team ratings once, before the season's first game, for the model's preseason prior.

usage: python3 bpi.py <hoopR-mbb-data/mbb dir> <repo dir>
Writes <repo>/bpi_preseason_YYYY.csv (team_id, team, bpi, bpi_off, bpi_def) for the newest season in the schedule,
only if that file doesn't exist yet AND no game of that season has been played (in-season BPI already reflects results,
which the model counts itself). Needs sports.core.api.espn.com allowed.
"""
import sys, re, glob, os, json, time, urllib.request
import pandas as pd

SRC, REPO = sys.argv[1], sys.argv[2]
log = lambda *a: print(*a, file=sys.stderr, flush=True)
season = max(int(re.search(r"_(\d{4})\.parquet$", f).group(1)) for f in glob.glob(f"{SRC}/schedules/parquet/mbb_schedule_*.parquet"))
out = f"{REPO}/bpi_preseason_{season}.csv"
if os.path.exists(out):
    log(f"{os.path.basename(out)} already saved - keeping it"); sys.exit(0)
s = pd.read_parquet(f"{SRC}/schedules/parquet/mbb_schedule_{season}.parquet")
if (s.status_type_completed == True).any():
    log(f"season {season} already under way - not saving preseason BPI"); sys.exit(0)

URL = "https://site.api.espn.com/apis/fitt/v3/sports/basketball/mens-college-basketball/powerindex?season={}&limit=100&page={}"
def get(u):
    for a in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 CBB-model"}), timeout=30) as r:
                return json.load(r)
        except Exception as e:
            err = e; time.sleep(2 * (a + 1))
    raise RuntimeError(f"ESPN unreachable: {err}")

rows, page = [], 1
while True:
    j = get(URL.format(season, page))
    for t in j.get("teams", []):
        cat = next((c for c in t.get("categories", []) if c.get("name") == "bpi"), None)
        names = next((c.get("names") for c in j.get("categories", []) if c.get("name") == "bpi"), None)
        if not cat or not names: continue
        v = dict(zip(names, cat.get("values", [])))
        if v.get("bpi") is not None:
            rows.append(dict(team_id=int(t["team"]["id"]), bpi=v["bpi"], bpi_off=v.get("bpioffense"), bpi_def=v.get("bpidefense")))
    if page >= (j.get("pagination") or {}).get("pages", 1): break
    page += 1; time.sleep(0.3)
B = pd.DataFrame(rows).drop_duplicates("team_id")
names = pd.concat([s[["home_id", "home_display_name"]].set_axis(["team_id", "team"], axis=1),
                   s[["away_id", "away_display_name"]].set_axis(["team_id", "team"], axis=1)]).drop_duplicates("team_id")
names["team_id"] = names.team_id.astype(int)
B = B.merge(names, on="team_id", how="left")[["team_id", "team", "bpi", "bpi_off", "bpi_def"]]
if len(B) < 300:
    log(f"only {len(B)} teams in ESPN's BPI - not saving"); sys.exit(2)
B.to_csv(out, index=False)
log(f"saved preseason BPI for {len(B)} teams to {os.path.basename(out)}")
