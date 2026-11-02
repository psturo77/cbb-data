"""Daily snapshot of Haslametrics team ratings (haslametrics.com, Erik Haslam - free site; source credited, one request a day).

usage: python3 hasla.py <repo dir>
Saves <repo>/hasla/hasla_<YYYY-MM-DD>.csv (ET date): espn_id, team, conf, rank, oe, de (adjusted points per 100
possessions), off/def tempo, net (oe - de). Teams are keyed by ESPN team id (Haslametrics uses ESPN's ids).
Kept so the ratings as they stood before each game can be tested against opening lines later in the season;
past seasons on the site are end-of-season only (would leak the future), so they can't be backtested fairly.
Source: https://haslametrics.com - ratings © Erik Haslam.
"""
import sys, os, urllib.request
import xml.etree.ElementTree as ET
import pandas as pd

REPO = sys.argv[1]
URL = "https://haslametrics.com/ratings.xml"
raw = urllib.request.urlopen(urllib.request.Request(URL, headers={"User-Agent": "CBB-model (personal; once daily)"}), timeout=60).read()
root = ET.fromstring(raw)
rows = []
for k in root.findall("mr"):
    a = k.attrib
    try:
        rows.append(dict(espn_id=int(a["id"]), team=a.get("t"), conf=a.get("c"), rank=int(float(a.get("rk", 0))),
                         oe=round(float(a["oe"]), 3), de=round(float(a["de"]), 3), otempo=round(float(a["ou"]), 3), dtempo=round(float(a["du"]), 3)))
    except (KeyError, ValueError):
        continue
H = pd.DataFrame(rows).drop_duplicates("espn_id")
if len(H) < 300: sys.exit(f"hasla: only {len(H)} teams parsed - not saved")
H["net"] = (H.oe - H.de).round(3)
day = pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d")
os.makedirs(f"{REPO}/hasla", exist_ok=True)
H.sort_values("rank").to_csv(f"{REPO}/hasla/hasla_{day}.csv", index=False)
print(f"hasla: {len(H)} teams saved for {day}; #1 {H.sort_values('rank').iloc[0].team} (net {H.net.max():+.1f})", file=sys.stderr)
