"""Conference availability reports (HD Intelligence widget: A-10, ACC, Big Ten, Big 12, Big East, MVC, SEC) - WATCH ONLY.
Reads the widget's public endpoint (the same call the conference sites make for any visitor), 5 s between requests.
Keeps, in <state dir>: avail_latest.json (current rows per conference), avail_log.csv (one row per player/game/status the
first time it is seen, with the pull time - for measuring how fast lines move after a report), raw/<conf>.json (last response).
No bet math reads it (Nov 24 2026 backtest on 2025-26: books moved ~0.6 of a new absence open->close, no edge vs close).
usage: python3 avail_pull.py <state dir>"""
import sys, os, json, time, csv, urllib.request, datetime as dt
ST = sys.argv[1]; os.makedirs(f"{ST}/raw", exist_ok=True)
B, UA, SPORT = "https://app.hdintelligence.com", "Mozilla/5.0 (personal CBB model)", "Men's Basketball"
CONFS = ["A10", "ACC", "B10", "B12", "BE", "MVC", "SEC"]
KEYS = ("Team", "TeamDisplay", "Player", "Number", "Opponent", "OpponentDisplay", "Initial", "Gameday", "Week", "Kickoff", "GameTime", "Date")
now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def post(path, body, conf):
    time.sleep(5)
    req = urllib.request.Request(B + path, data=json.dumps(body).encode(), method="POST", headers={
        "User-Agent": UA, "Content-Type": "application/json", "Origin": B,
        "Referer": f"{B}/?source={conf}&sport=Men%27s%20Basketball&conf={conf}&type=report"})
    with urllib.request.urlopen(req, timeout=60) as r: return json.load(r)
def rows_of(d):
    """report rows from either response shape: a list of rows, {data: [...]}, or game blocks holding rows"""
    if isinstance(d, dict): d = d.get("data", [])
    out = []
    for x in d if isinstance(d, list) else []:
        if isinstance(x, dict) and "Player" in x: out.append(x)
        elif isinstance(x, dict):
            for v in x.values():
                if isinstance(v, list): out += [y for y in v if isinstance(y, dict) and "Player" in y]
    return out
latest, notes = {"updated": now, "confs": {}}, []
for c in CONFS:
    try:
        d = post("/api/get-publish-public", dict(sport=SPORT, organization=c, conference=c), c)
    except Exception as e:
        notes.append(f"{c}: {e!r}"[:200]); continue
    json.dump(d, open(f"{ST}/raw/{c}.json", "w"))
    R = rows_of(d)
    if d and not R and not (isinstance(d, dict) and set(d) <= {"_nextReportDate"}): notes.append(f"{c}: response has no rows I can read - check raw/{c}.json")
    latest["confs"][c] = {"n": len(R), "next": d.get("_nextReportDate") if isinstance(d, dict) else None,
                          "rows": [{k: r.get(k) for k in KEYS if k in r} for r in R]}
latest["notes"] = notes
json.dump(latest, open(f"{ST}/avail_latest.json", "w"), indent=1)
LOG = f"{ST}/avail_log.csv"; F = ["first_seen", "conf", "team", "player", "number", "opponent", "game", "initial", "gameday"]
seen = set()
if os.path.exists(LOG):
    for r in csv.DictReader(open(LOG)): seen.add(tuple(r[k] for k in F[1:]))
new = []
for c, v in latest["confs"].items():
    for r in v["rows"]:
        k = (c, r.get("TeamDisplay") or r.get("Team") or "", r.get("Player") or "", r.get("Number") or "", r.get("OpponentDisplay") or r.get("Opponent") or "",
             str(r.get("Week") or r.get("Date") or r.get("Kickoff") or ""), r.get("Initial") or "", r.get("Gameday") or "")
        if k not in seen: seen.add(k); new.append((now,) + k)
head = not os.path.exists(LOG)
with open(LOG, "a", newline="") as f:
    w = csv.writer(f)
    if head: w.writerow(F)
    w.writerows(new)
print(f"avail: {sum(v['n'] for v in latest['confs'].values())} rows now ({', '.join(c + ' ' + str(v['n']) for c, v in latest['confs'].items())}); "
      f"{len(new)} new; notes: {notes or 'none'}")
