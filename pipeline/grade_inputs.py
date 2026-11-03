"""Small copies of this season's hoopR schedule and player box, for grading recs and props without the full hoopR download.

usage: python3 grade_inputs.py <hoopR-mbb-data/mbb dir> <out mbb dir>
Writes <out>/schedules/parquet/mbb_schedule_<season>.parquet and <out>/player_box/parquet/player_box_<season>.parquet with
only the columns grade_recs.py and grade_props.py read, so they run unchanged with <out> as their hoopR dir.
"""
import sys, os, re, glob
import pandas as pd

SRC, OUT = sys.argv[1], sys.argv[2]
season = max(int(re.search(r"_(\d{4})\.parquet$", f).group(1)) for f in glob.glob(f"{SRC}/schedules/parquet/mbb_schedule_*.parquet"))
os.makedirs(f"{OUT}/schedules/parquet", exist_ok=True); os.makedirs(f"{OUT}/player_box/parquet", exist_ok=True)
s = pd.read_parquet(f"{SRC}/schedules/parquet/mbb_schedule_{season}.parquet",
                    columns=["game_id", "status_type_completed", "home_id", "home_score", "away_score", "home_display_name", "away_display_name"])
s.to_parquet(f"{OUT}/schedules/parquet/mbb_schedule_{season}.parquet", index=False)
pb = f"{SRC}/player_box/parquet/player_box_{season}.parquet"
n = 0
if os.path.exists(pb):
    p = pd.read_parquet(pb, columns=["game_id", "athlete_id", "points", "minutes", "did_not_play"]); n = len(p)
    p.to_parquet(f"{OUT}/player_box/parquet/player_box_{season}.parquet", index=False)
print(f"grade_inputs: season {season}, {len(s)} schedule rows, {n} player-box rows", file=sys.stderr)
