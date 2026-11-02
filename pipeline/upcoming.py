"""Current betting lines for upcoming games, from ESPN's public odds feed (DraftKings lines, with the true opener).

usage: python3 upcoming.py <hoopR-mbb-data/mbb dir | folder of board slate documents> <out json> [days ahead, default 7]
Writes {updated, provider, n, games: {game_id: {start, book, nb, seen, sp, spp, tot, op, up, mlh, mla, open: {sp, tot}}}}
in the same shape the dashboard uses for odds (sp = HOME spread). Needs sports.core.api.espn.com allowed.
The dashboard stores it as board/espn and uses it when your own odds pull for a game is older or missing.
"""
import sys, re, glob, json, time, urllib.request
import pandas as pd

SRC, OUT = sys.argv[1], sys.argv[2]
AHEAD = int(sys.argv[3]) if len(sys.argv) > 3 else 7
URL = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/mens-college-basketball/events/{0}/competitions/{0}/odds"
PRED = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/mens-college-basketball/events/{0}/competitions/{0}/predictor"
log = lambda *a: print(*a, file=sys.stderr, flush=True)

now = pd.Timestamp.now(tz="UTC")
slates = glob.glob(f"{SRC}/slate_d*.json") + glob.glob(f"{SRC}/d20*.json")
if slates:      # light mode: the board's own slate documents (as saved by ArtifactData) instead of the hoopR data
    rows = [dict(game_id=g["id"], start=pd.Timestamp(g["start"]), home_id=g.get("hid"), status=g.get("status"))
            for f in slates for g in json.load(open(f)).get("games", [])]
    s = pd.DataFrame(rows).drop_duplicates("game_id")
    s["home_id"] = pd.to_numeric(s.home_id, errors="coerce")
    s = s.dropna(subset=["home_id"])          # can't confirm home/away orientation without it - skip rather than guess
    todo = s[(s.status == "pre") & (s.start > now) & (s.start <= now + pd.Timedelta(days=AHEAD))]
    log(f"board slates: {len(todo)} upcoming games in the next {AHEAD} days")
else:
    season = max(int(re.search(r"_(\d{4})\.parquet$", f).group(1)) for f in glob.glob(f"{SRC}/schedules/parquet/mbb_schedule_*.parquet"))
    s = pd.read_parquet(f"{SRC}/schedules/parquet/mbb_schedule_{season}.parquet")
    s["start"] = pd.to_datetime(s.start_date, utc=True)
    todo = s[(s.status_type_completed != True) & (s.start > now) & (s.start <= now + pd.Timedelta(days=AHEAD))]
    log(f"season {season}: {len(todo)} upcoming games in the next {AHEAD} days")


def num(x):
    try: return float(str(x).lstrip("ou").replace("+", ""))
    except Exception: return None


def fetch(gid):
    url = gid if str(gid).startswith("http") else URL.format(gid)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 CBB-model"}), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404: return {}
            err = e
        except Exception as e:
            err = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"ESPN unreachable for {gid}: {err}")


games, fails, provider = {}, 0, ""
seen = now.strftime("%Y-%m-%dT%H:%M:%SZ")
for g in todo.itertuples(index=False):
    gid = str(g.game_id)
    try:
        d = fetch(gid)
    except RuntimeError as e:
        fails += 1; log(e)
        if fails >= 5: log("too many failures - stopping"); break
        continue
    items = [o for o in (d or {}).get("items", []) if o.get("spread") is not None
             and "live" not in str((o.get("provider") or {}).get("name", "")).lower()]
    if not items: continue
    items.sort(key=lambda o: (o.get("provider") or {}).get("priority", 99))
    o = items[0]; ho = o.get("homeTeamOdds") or {}; ao = o.get("awayTeamOdds") or {}
    op = o.get("open") or {}
    # ESPN sometimes lists a neutral-site game the other way round from the schedule the model uses: flip to match
    tid = lambda t: (re.search(r"/teams/(\d+)", str((t.get("team") or {}).get("$ref", ""))) or [None, None])[1]
    flip = int(g.home_id) > 0 and tid(ho) is not None and str(tid(ho)) != str(int(g.home_id)) and str(tid(ao)) == str(int(g.home_id))
    if flip: ho, ao = ao, ho
    sgn = -1 if flip else 1
    rec = dict(start=g.start.strftime("%Y-%m-%dT%H:%M:%SZ"), book=(o.get("provider") or {}).get("name", "ESPN"), nb=1, seen=seen,
               sp=(num(o.get("spread")) * sgn if num(o.get("spread")) is not None else None), spp=num(ho.get("spreadOdds")), spa=num(ao.get("spreadOdds")), tot=num(o.get("overUnder")),
               op=num(o.get("overOdds")), up=num(o.get("underOdds")), mlh=num(ho.get("moneyLine")), mla=num(ao.get("moneyLine")),
               open=dict(sp=num(((ho.get("open") or {}).get("pointSpread") or {}).get("american")),
                         tot=num(((op.get("total") or {}).get("american")))))
    # ESPN's BPI forecast for the game (home margin) - lines tend to drift toward it, so it feeds the expected close
    try:
        pj = fetch(PRED.format(gid))
        h = (pj or {}).get("homeTeam") or {}
        st = {x.get("name"): x.get("value") for x in h.get("statistics", [])}
        bt = re.search(r"/teams/(\d+)", str((h.get("team") or {}).get("$ref", "")))
        if st.get("teampredmov") is not None and bt:
            same = int(g.home_id) <= 0 or bt.group(1) == str(int(g.home_id))
            rec["bpi"] = round(float(st["teampredmov"]) * (1 if same else -1) * (-1 if (int(g.home_id) <= 0 and flip) else 1), 2)
    except RuntimeError:
        pass
    games[gid] = {k: v for k, v in rec.items() if v is not None}
    provider = provider or rec["book"]
    time.sleep(0.1)

json.dump({"updated": seen, "provider": provider, "n": len(games), "games": games}, open(OUT, "w"), separators=(",", ":"))
log(f"saved lines for {len(games)} of {len(todo)} upcoming games ({provider or 'none'}); {fails} fetch failures")
if fails and not games: sys.exit(2)
