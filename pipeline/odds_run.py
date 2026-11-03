"""One command for the scheduled odds task (and the board's refresh button): steps 3-10 of the old checklist.

usage: ODDS_API_KEY=... python3 odds_run.py <db dir> <out dir> --versions=odds:N,espn:N,alerts:N,injuries:N [--light]
       [--raw=<saved odds json>]   (reuse a download instead of calling The Odds API - for retries and tests)
       [--force-digest] [--force-props]   (tests only)

<db dir> holds what ArtifactData saved: board/*.json, slate/*.json, rec/*.json (recent recs), prop/*.json (1 PM run).
--versions: the versions ArtifactData listed for those board docs; leave a name out if the doc doesn't exist.
--light: odds only (the button's quick refresh) - ESPN lines and PRIME/arb alerts too, using the saved injury list,
         but no new injury pull, no ledger, no digest, no props.

Writes into <out dir>:
  batch1.json, batch2.json, ...   each {"writes": [...]} ready to pass to ArtifactData "batch" as-is
  reply.txt                       the run's reply, already in the required wording
  status.json                     {"stop": bool, "credits": ..., "odds_ok": bool}
Never prints the API key. Makes exactly one /odds request (plus props_snap's on the 1 PM run). Never retries it.
"""
import sys, os, re, glob, json, shutil, subprocess, urllib.request, urllib.error
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
flags = {a.split("=")[0]: (a.split("=", 1)[1] if "=" in a else "") for a in sys.argv[1:] if a.startswith("--")}
DB, OUT = [os.path.abspath(a) for a in sys.argv[1:] if not a.startswith("--")][:2]
LIGHT = "--light" in flags
KEY = os.environ.get("ODDS_API_KEY", "")
VER = {}
for kv in filter(None, flags.get("--versions", "").split(",")):
    k, v = kv.split(":"); VER[k.strip()] = int(v)
os.makedirs(f"{OUT}/rec", exist_ok=True); os.makedirs(f"{OUT}/prop", exist_ok=True)
now = pd.Timestamp.now(tz="UTC"); et = now.tz_convert("America/New_York")
DIGEST = (et.hour == 9 and not LIGHT) or "--force-digest" in flags
PROPS = (et.hour == 13 and not LIGHT) or "--force-props" in flags
notes, status = [], dict(stop=False, credits=None, odds_ok=False)
clean = lambda s: (s or "").replace(KEY, "<key>") if KEY else (s or "")

def run(args, env=None):
    p = subprocess.run([sys.executable] + args, capture_output=True, text=True, env=env, cwd=HERE)
    return p.returncode, p.stdout, clean(p.stderr).strip().splitlines()

def finish(reply):
    open(f"{OUT}/reply.txt", "w").write(reply.rstrip() + "\n")
    json.dump(status, open(f"{OUT}/status.json", "w"))
    print(reply)
    sys.exit(0)

# ---- step 3: anything to price in the next 7 days?
slate = [g for f in glob.glob(f"{DB}/slate/d*.json") for g in json.load(open(f)).get("games", [])]
if not any(g.get("status") == "pre" and now < pd.Timestamp(g["start"]) <= now + pd.Timedelta(days=7) for g in slate):
    status["stop"] = True
    finish("No CBB games in the next week - odds not pulled.")

# ---- step 4: one odds request, never retried
raw, err = f"{OUT}/raw.json", None
if flags.get("--raw"):
    shutil.copy(flags["--raw"], raw); status["credits"] = "?"
else:
    url = ("https://api.the-odds-api.com/v4/sports/basketball_ncaab/odds?regions=us,us2,eu&markets=h2h,spreads,totals"
           f"&oddsFormat=american&apiKey={KEY}")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "CBB-model"}), timeout=60) as r:
            open(raw, "wb").write(r.read()); status["credits"] = r.headers.get("x-requests-remaining", "?")
    except urllib.error.HTTPError as e:
        err = f"HTTP {e.code} {clean(e.read()[:120].decode('utf-8', 'replace')).strip()}"
    except Exception as e:
        err = clean(str(e)).splitlines()[0] if str(e) else type(e).__name__
    if err and os.path.exists(raw): os.remove(raw)

# ---- step 5: odds document
games_n = books_n = pins = 0
if not err:
    rc, _, log = run(["oddspull.py", raw, DB, f"{OUT}/board_odds.json"])
    if rc == 0:
        status["odds_ok"] = True
        d = json.load(open(f"{OUT}/board_odds.json"))
        games_n, books_n = d.get("matched", 0), len(d.get("books", []))
        m = re.search(r"\((\d+) with Pinnacle\)", " ".join(log)); pins = int(m.group(1)) if m else 0
    else:
        err = "oddspull.py failed: " + (log[-1] if log else f"exit {rc}")
odds_file = f"{OUT}/board_odds.json" if status["odds_ok"] else f"{OUT}/board_espn.json"

# ---- step 6: ESPN lines
rc, _, log = run(["upcoming.py", f"{DB}/slate", f"{OUT}/board_espn.json"])
if rc != 0:
    notes.append("ESPN lines failed: " + (log[-1] if log else f"exit {rc}"))
    if os.path.exists(f"{OUT}/board_espn.json"): os.remove(f"{OUT}/board_espn.json")
espn_file = f"{OUT}/board_espn.json" if os.path.exists(f"{OUT}/board_espn.json") else f"{DB}/board/espn.json"
if not status["odds_ok"]: odds_file = espn_file

# ---- step 7: injuries (light: reuse the saved list)
inj_file, inj_n = None, None
if LIGHT:
    if os.path.exists(f"{DB}/board/injuries.json"): inj_file = f"{DB}/board/injuries.json"
else:
    rc, _, log = run(["injuries.py", f"{DB}/slate", f"{DB}/board/players.json", f"{OUT}/board_injuries.json"])
    if rc == 0:
        inj_file = f"{OUT}/board_injuries.json"; inj_n = json.load(open(inj_file)).get("n", 0)
    else:
        notes.append("Injury pull failed: " + (log[-1] if log else f"exit {rc}"))
inj = [f"--inj={inj_file}"] if inj_file else []

# ---- step 8: alerts (+ 9 AM digest)
alerted = f"{OUT}/alerted.json"
if os.path.exists(f"{DB}/board/alerts.json"): shutil.copy(f"{DB}/board/alerts.json", alerted)
alert_odds = [f"{OUT}/board_odds.json"] if status["odds_ok"] else []
rc, out, log = run(["alerts.py", f"{DB}/slate", f"{DB}/board/meta.json", espn_file, alerted, "0.05"] + alert_odds + inj)
alerts = [l for l in out.splitlines() if l.strip()] if rc == 0 else []
if rc != 0: notes.append("Alerts failed: " + (log[-1] if log else f"exit {rc}"))
digest = None
if DIGEST:
    tmp = f"{OUT}/alerted_digest.json"
    if os.path.exists(alerted): shutil.copy(alerted, tmp)
    rc, out, log = run(["alerts.py", f"{DB}/slate", f"{DB}/board/meta.json", espn_file, tmp, "0.05"] + alert_odds + inj + ["--digest"])
    digest = [l for l in out.splitlines() if l.strip()] if rc == 0 else None
    if rc != 0: notes.append("Morning summary failed: " + (log[-1] if log else f"exit {rc}"))

# ---- step 9: ledger
new_recs = 0
if not LIGHT:
    rc, out, log = run(["recs.py", f"{DB}/slate", f"{DB}/board/meta.json", espn_file, odds_file, f"{DB}/rec", f"{OUT}/rec"] + inj)
    if rc == 0: new_recs = len(glob.glob(f"{OUT}/rec/*.json"))
    else:
        notes.append("Ledger failed: " + (log[-1] if log else f"exit {rc}"))
        for f in glob.glob(f"{OUT}/rec/*.json"): os.remove(f)

# ---- step 10: props (1 PM)
props_line = None
if PROPS and os.path.exists(f"{DB}/board/props.json"):
    env = dict(os.environ, ODDS_API_KEY=KEY)
    rc, _, log = run(["props_snap.py", f"{DB}/board/props.json", f"{DB}/board/teammap.json", f"{DB}/prop", f"{OUT}/prop", "60"], env)
    props_line = (log[-1] if log else "props: no output") if rc == 0 else "Props step failed: " + (log[-1] if log else f"exit {rc}")
    if rc != 0:
        for f in glob.glob(f"{OUT}/prop/*.json"): os.remove(f)

# ---- step 11: batches, ready for ArtifactData "batch"
def pin(name, entry):
    if name in VER: entry["if_version"] = VER[name]
    return entry
first = []
if status["odds_ok"]: first.append(pin("odds", dict(op="set", collection="board", doc_id="odds", file_path=f"{OUT}/board_odds.json")))
if os.path.exists(f"{OUT}/board_espn.json"): first.append(pin("espn", dict(op="set", collection="board", doc_id="espn", file_path=f"{OUT}/board_espn.json")))
if not LIGHT and inj_file == f"{OUT}/board_injuries.json": first.append(pin("injuries", dict(op="set", collection="board", doc_id="injuries", file_path=inj_file)))
if os.path.exists(alerted): first.append(pin("alerts", dict(op="set", collection="board", doc_id="alerts", file_path=alerted)))
extra = [dict(op="set", collection="rec", doc_id=os.path.basename(f)[:-5], file_path=f) for f in sorted(glob.glob(f"{OUT}/rec/*.json"))]
extra += [dict(op="set", collection="prop", doc_id=os.path.basename(f)[:-5], file_path=f) for f in sorted(glob.glob(f"{OUT}/prop/*.json"))]
for f in glob.glob(f"{OUT}/batch*.json"): os.remove(f)
batches = ([first] if first else []) + [extra[i:i + 50] for i in range(0, len(extra), 50)]
for i, b in enumerate(batches, 1): json.dump({"writes": b}, open(f"{OUT}/batch{i}.json", "w"), indent=1)

# ---- step 12: reply
lines = []
if DIGEST:
    lines += ["Today's best bets:"] + digest if digest else ["Today's best bets: none at 3%+ yet."]
if alerts: lines += ["New PRIME bets:"] + alerts
elif not DIGEST: lines.append("No new PRIME bets.")
lines += notes
inj_txt = f"injuries: {inj_n} players listed" if inj_n is not None else ("injuries: saved list reused" if LIGHT else "injuries: not updated")
ledger_txt = "ledger: skipped (quick refresh)" if LIGHT else f"ledger: {new_recs} new recs"
if err: lines.append(f"Odds pull failed: {err} · {inj_txt} · {ledger_txt}")
else: lines.append(f"Odds: {games_n} games from {books_n} books, {pins} with Pinnacle, {status['credits']} credits left · {inj_txt} · {ledger_txt}")
if props_line: lines.append(props_line)
status["batches"] = len(batches)
finish("\n".join(lines))
