"""Collect ESPN's closing (and opening) lines for finished games of the current season.

usage: python3 lines.py <hoopR-mbb-data/mbb dir> <repo dir>
Reads <repo>/lines.csv if it exists, adds finished games that are missing (or had no line yet), writes it back.
Source: ESPN's public odds feed (the same one the Mac backtest script used); needs sports.core.api.espn.com allowed.
Columns: game_id, date, provider, spread_close, spread_open, total_close, total_open, fetched
spread_* are the HOME team's spread (negative = home favored), home as ESPN's odds feed lists it (home_id);
process.py flips a game whose home_id differs from the box score's home team.
"""
import sys, os, re, glob, json, time, urllib.request
import pandas as pd

SRC, REPO = sys.argv[1], sys.argv[2]
PATH = f"{REPO}/lines.csv"
COLS = ["game_id", "date", "provider", "spread_close", "spread_open", "total_close", "total_open", "fetched", "home_id"]
URL = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/mens-college-basketball/events/{0}/competitions/{0}/odds"
RETRY_DAYS = 3          # keep asking for a game's line for this many days after it was played
log = lambda *a: print(*a, file=sys.stderr, flush=True)

seasons = sorted(int(re.search(r"_(\d{4})\.parquet$", f).group(1)) for f in glob.glob(f"{SRC}/schedules/parquet/mbb_schedule_*.parquet"))
season = seasons[-1]
s = pd.read_parquet(f"{SRC}/schedules/parquet/mbb_schedule_{season}.parquet")
s["game_id"] = s.game_id.astype(str)
s["date"] = pd.to_datetime(s.start_date, utc=True).dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
done = s[(s.status_type_completed == True) | (s.status_type_name == "STATUS_FINAL")]

have = pd.read_csv(PATH, dtype={"game_id": str}) if os.path.exists(PATH) else pd.DataFrame(columns=COLS)
today = pd.Timestamp.now(tz="America/New_York").tz_localize(None).normalize()
known = set(have.game_id[have.spread_close.notna() | have.spread_open.notna()])
stale = set(have.game_id[have.spread_close.isna() & have.spread_open.isna()])
todo = done[~done.game_id.isin(known)]
todo = todo[~todo.game_id.isin(stale) | (pd.to_datetime(todo.date) >= today - pd.Timedelta(days=RETRY_DAYS))]
log(f"season {season}: {len(done)} finished games, {len(known)} with lines saved, fetching {len(todo)}")


def fetch(gid):
    for attempt in range(3):
        try:
            req = urllib.request.Request(URL.format(gid), headers={"User-Agent": "Mozilla/5.0 CBB-model"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404: return {}
        except Exception as e:
            err = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"could not reach ESPN for game {gid}: {err}")


def num(x):
    try: return float(str(x).replace("+", ""))
    except Exception: return None


def pick(items):
    """The pre-game line from the main provider (skip in-game 'Live Odds' entries)."""
    pre = [o for o in items if (o.get("details") or o.get("overUnder") is not None)
           and "live" not in str((o.get("provider") or {}).get("name", "")).lower()]
    if not pre: return None
    o = pre[0]; ho = o.get("homeTeamOdds") or {}
    ps = lambda k: num(((ho.get(k) or {}).get("pointSpread") or {}).get("american"))
    tot = lambda k: num(((o.get(k) or {}).get("total") or {}).get("american", "").lstrip("ou"))
    close = num(o.get("spread")) if o.get("spread") is not None else ps("close")
    m = re.search(r"/teams/(\d+)", str((ho.get("team") or {}).get("$ref", "")))
    return dict(provider=(o.get("provider") or {}).get("name", ""), spread_close=close, spread_open=ps("open"),
                total_close=num(o.get("overUnder")), total_open=tot("open"), home_id=(m.group(1) if m else None))


new, fails = [], 0
for i, g in enumerate(todo.itertuples(index=False)):
    try:
        od = fetch(g.game_id)
    except RuntimeError as e:
        fails += 1; log(e)
        if fails >= 5: log("too many failures - stopping; lines.csv keeps what it has"); break
        continue
    rec = pick(od.get("items", [])) if od else None
    new.append(dict(game_id=g.game_id, date=g.date, **(rec or {}), fetched=str(today.date())))
    time.sleep(0.1)
    if (i + 1) % 100 == 0: log(f"  {i + 1}/{len(todo)}")

out = pd.concat([have[~have.game_id.isin({r["game_id"] for r in new})], pd.DataFrame(new)], ignore_index=True)
out = out.reindex(columns=COLS).sort_values(["date", "game_id"])
out.to_csv(PATH, index=False)
got = sum(1 for r in new if r.get("spread_close") is not None or r.get("spread_open") is not None)
log(f"saved {len(out)} rows ({got} new with a line, {len(new) - got} without); {fails} fetch failures")
if fails and not new: sys.exit(2)
