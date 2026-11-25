"""One-off probe v6: ESPN open/close lines for every finished 2025-26 game (for the availability-report backtest).
Same parsing as claude/backtest/fetch_season_lines.py. Output: probe_out/lines_2026.csv"""
import os, re, json, time, subprocess, urllib.request, concurrent.futures as cf, pandas as pd
OUT = "probe_out"; os.makedirs(OUT, exist_ok=True)
T = os.environ.get("RUNNER_TEMP", "/tmp") + "/hoopr"
subprocess.run(f"GIT_LFS_SKIP_SMUDGE=1 git clone -q --depth 1 --filter=blob:none --no-checkout https://github.com/sportsdataverse/hoopR-mbb-data {T} && "
               f"cd {T} && git checkout -q HEAD -- mbb/schedules/parquet/mbb_schedule_2026.parquet", shell=True, check=True)
s = pd.read_parquet(f"{T}/mbb/schedules/parquet/mbb_schedule_2026.parquet")
s = s[(s.status_type_completed == True)]; s["game_id"] = s.game_id.astype(str)
URL = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/mens-college-basketball/events/{0}/competitions/{0}/odds"
def safe(x):
    try: return float(str(x).lstrip("ou").replace("+", "")) if x not in (None, "") else None
    except Exception: return None
def one(gid):
    for a in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(URL.format(gid), headers={"User-Agent": "Mozilla/5.0 CBB-model"}), timeout=30) as r: d = json.load(r)
            pre = [o for o in d.get("items", []) if (o.get("details") or o.get("overUnder") is not None) and "live" not in str((o.get("provider") or {}).get("name", "")).lower()]
            if not pre: return dict(game_id=gid)
            o = pre[0]; ho = o.get("homeTeamOdds") or {}
            ps = lambda k: safe(((ho.get(k) or {}).get("pointSpread") or {}).get("american"))
            m = re.search(r"/teams/(\d+)", str((ho.get("team") or {}).get("$ref", "")))
            return dict(game_id=gid, provider=(o.get("provider") or {}).get("name"), spread_close=safe(o.get("spread")) if o.get("spread") is not None else ps("close"),
                        spread_open=ps("open"), total_close=safe(o.get("overUnder")), total_open=safe(((o.get("open") or {}).get("total") or {}).get("american")),
                        home_id=m.group(1) if m else None)
        except urllib.error.HTTPError as e:
            if e.code == 404: return dict(game_id=gid)
        except Exception: time.sleep(1 + a)
    return dict(game_id=gid, err=1)
with cf.ThreadPoolExecutor(6) as ex: rows = list(ex.map(one, s.game_id.tolist()))
L = pd.DataFrame(rows); L.to_csv(f"{OUT}/lines_2026.csv", index=False)
print(len(L), "games;", L.spread_close.notna().sum(), "close;", L.spread_open.notna().sum(), "open;", L.get("err", pd.Series()).notna().sum(), "errors")
