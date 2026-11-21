#!/usr/bin/env bash
# One odds cycle (Nov 21 2026): pull The Odds API, build the board writes with odds_run.py, send new bets to ntfy, publish to odds-raw.
# Run from the repo root by .github/workflows/odds-pull.yml, which repeats it every 5-10 minutes inside one job.
# env: ODDS_API_KEY, NTFY_TOPIC, NTFY_TEST (0/1), MODE (scheduled|manual|quick), PROPS_FORCE (0/1), BUILD (numeric build id), GH_TOKEN, REPO
set -u
rm -rf /tmp/raw /tmp/bd /tmp/prev /tmp/db /tmp/out /tmp/out_failed /tmp/props_day.txt
mkdir -p /tmp/raw/prop /tmp/bd /tmp/prev /tmp/db/board /tmp/db/slate /tmp/db/rec /tmp/db/prop

# previous state first: the pull asks for the books in state/prefs.json
if git fetch -q --depth 1 origin +refs/heads/odds-raw:refs/remotes/origin/odds-raw; then git archive refs/remotes/origin/odds-raw | tar -x -C /tmp/prev; fi

python3 - <<'PY'
import os, json, urllib.request, urllib.error, datetime
key = os.environ["ODDS_API_KEY"]
try: books = (json.load(open("/tmp/prev/state/prefs.json")) or {}).get("books") or []
except Exception: books = []
# your books + Pinnacle (the market line): up to 10 bookmakers cost 1 credit; without a book list, every book in us/us2/eu (3 credits)
sel = "bookmakers=" + ",".join(sorted(set(books) | {"pinnacle"})) if books and len(set(books) | {"pinnacle"}) <= 10 else "regions=us,us2,eu"
url = f"https://api.the-odds-api.com/v4/sports/basketball_ncaab/odds?{sel}&markets=spreads&oddsFormat=american&apiKey={key}"
meta = {"pulled_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "query": sel}
try:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "CBB-model"}), timeout=60) as r:
        open("/tmp/raw/raw.json", "wb").write(r.read())
        meta.update(credits=r.headers.get("x-requests-remaining", "?"), used=r.headers.get("x-requests-used", "?"), cost=r.headers.get("x-requests-last", "?"))
except urllib.error.HTTPError as e:
    meta["error"] = f"HTTP {e.code} " + e.read()[:120].decode("utf-8", "replace").replace(key, "<key>")
except Exception as e:
    meta["error"] = (str(e) or type(e).__name__).replace(key, "<key>")[:160]
json.dump(meta, open("/tmp/raw/pull.json", "w"))
print(json.dumps(meta))
PY

# board inputs
git fetch -q --depth 1 origin +refs/heads/board-data:refs/remotes/origin/board-data
git archive refs/remotes/origin/board-data | tar -x -C /tmp/bd
for n in meta players espn props teams; do [ -f /tmp/bd/board_$n.json ] && cp /tmp/bd/board_$n.json /tmp/db/board/$n.json; done
cp pipeline/teammap.json /tmp/db/board/teammap.json
for f in /tmp/bd/slate_d*.json; do cp "$f" "/tmp/db/slate/$(basename "$f" | sed 's/^slate_//')"; done
mkdir -p /tmp/prev/state/rec /tmp/prev/state/prop
for n in alerts injuries odds prefs; do [ -f /tmp/prev/state/$n.json ] && cp /tmp/prev/state/$n.json /tmp/db/board/$n.json; done
cp /tmp/prev/state/rec/*.json /tmp/db/rec/ 2>/dev/null || true
cp /tmp/prev/state/prop/*.json /tmp/db/prop/ 2>/dev/null || true
echo "slates: $(ls /tmp/db/slate | wc -l), recs known: $(ls /tmp/db/rec | wc -l)"

# props snapshot: once a day, first cycle in the 1 PM ET hour (or forced)
today=$(TZ=America/New_York date +%F)
if [ "${PROPS_FORCE:-0}" = "1" ] || { [ "$MODE" = "scheduled" ] && [ "$(TZ=America/New_York date +%H)" = "13" ] && [ "$(cat /tmp/prev/state/props_day.txt 2>/dev/null)" != "$today" ]; }; then
  echo "$today" > /tmp/props_day.txt
  if [ -f /tmp/db/board/props.json ]; then python3 pipeline/props_snap.py /tmp/db/board/props.json /tmp/db/board/teammap.json /tmp/db/prop /tmp/raw/prop 60 || echo "props snapshot failed"; fi
fi

# build the board writes
flag=""; [ "$MODE" = "manual" ] && flag="--manual"; [ "$MODE" = "quick" ] && flag="--light"
if [ "$(cat /tmp/prev/state/digest_day.txt 2>/dev/null)" = "$today" ]; then flag="$flag --no-digest"; fi   # one morning summary a day
python3 pipeline/odds_run.py /tmp/db /tmp/out --raw-dir=/tmp/raw $flag || echo "odds_run failed" > /tmp/out_failed
mkdir -p /tmp/raw/out /tmp/raw/state/rec /tmp/raw/state/prop
cp -r /tmp/out/. /tmp/raw/out/ 2>/dev/null || true
rm -f /tmp/raw/out/raw.json
python3 - <<'PY'
import glob, json
for f in glob.glob("/tmp/raw/out/batch*.json"):
    d = json.load(open(f))
    for w in d["writes"]:
        if "file_path" in w: w["file_path"] = w["file_path"].replace("/tmp/out/", "out/")
    json.dump(d, open(f, "w"), indent=1)
PY
# carry the state forward: alert history, injuries, and every rec / prop made so far
cp /tmp/prev/state/rec/*.json /tmp/raw/state/rec/ 2>/dev/null || true
cp /tmp/prev/state/prop/*.json /tmp/raw/state/prop/ 2>/dev/null || true
cp /tmp/out/rec/*.json /tmp/raw/state/rec/ 2>/dev/null || true
cp /tmp/out/prop/*.json /tmp/raw/state/prop/ 2>/dev/null || true
for n in alerts injuries odds; do
  src=/tmp/out/board_$n.json; [ "$n" = "alerts" ] && src=/tmp/out/alerted.json
  if [ -f "$src" ]; then cp "$src" /tmp/raw/state/$n.json; elif [ -f /tmp/prev/state/$n.json ]; then cp /tmp/prev/state/$n.json /tmp/raw/state/; fi
done
[ -f /tmp/prev/state/prefs.json ] && cp /tmp/prev/state/prefs.json /tmp/raw/state/ || true
# once-a-day stamps (the digest goes out on the first scheduled cycle in the 9 AM hour)
if [ "$MODE" = "scheduled" ] && [ "$(TZ=America/New_York date +%H)" = "09" ]; then echo "$today" > /tmp/raw/state/digest_day.txt; else cp /tmp/prev/state/digest_day.txt /tmp/raw/state/ 2>/dev/null || true; fi
if [ -f /tmp/props_day.txt ]; then cp /tmp/props_day.txt /tmp/raw/state/props_day.txt; else cp /tmp/prev/state/props_day.txt /tmp/raw/state/ 2>/dev/null || true; fi
# alert log: every build's new alerts (last 120 builds, ~12+ hours), so the copy task can send alerts from builds it never copied
python3 - "$BUILD" <<'PY'
import json, os, sys, datetime
prev = "/tmp/prev/state/alert_log.json"
log = json.load(open(prev)) if os.path.exists(prev) else []
lines = [l.rstrip("\n") for l in open("/tmp/out/alerts.txt")] if os.path.exists("/tmp/out/alerts.txt") else []
log.append({"run": sys.argv[1], "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "alerts": [l for l in lines if l.strip()]})
log = log[-120:]
for f in ("/tmp/raw/state/alert_log.json", "/tmp/raw/out/alert_log.json"): json.dump(log, open(f, "w"), indent=1)
PY
echo "{\"mode\":\"$MODE\", \"built_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"run\": \"$BUILD\"}" > /tmp/raw/out/run.json
[ -f /tmp/raw/out/alerted.json ] && python3 -c "import json,sys; f=sys.argv[1]; d=json.load(open(f)); d['build']=sys.argv[2]; json.dump(d, open(f,'w'))" /tmp/raw/out/alerted.json "$BUILD" || true
cat /tmp/raw/out/reply.txt 2>/dev/null || cat /tmp/out_failed

# new bets to ntfy
python3 - <<'PY'
import os, urllib.request
topic = os.environ.get("NTFY_TOPIC", "").strip()
if not topic: print("no NTFY_TOPIC secret - skipped"); raise SystemExit(0)
lines = [l.strip() for l in open("/tmp/out/alerts.txt")] if os.path.exists("/tmp/out/alerts.txt") else []
lines = [l for l in lines if l]
if os.environ.get("NTFY_TEST") == "1" and not lines:
    lines = ["Test: CBB spread alerts are connected. New 5%+ spread bets will arrive here."]
if not lines: print("no new bets"); raise SystemExit(0)
title = f"{len(lines)} new spread bet{'s' if len(lines) > 1 else ''} over 5%" if not lines[0].startswith("Test:") else "CBB alerts test"
req = urllib.request.Request(f"https://ntfy.sh/{topic}", data="\n\n".join(lines[:8]).encode(), method="POST",
                             headers={"Title": title, "Priority": "high", "Tags": "basketball",
                                      "Click": "https://claude.ai/artifact/SgPtmedZ2wVPiYHaJoRNZh"})
try: urllib.request.urlopen(req, timeout=20).read(); print(f"sent {len(lines)} line(s)")
except Exception as e: print("ntfy send failed:", type(e).__name__)
PY

# publish
cd /tmp/raw
rm -rf .git; git init -q -b odds-raw
git config user.name "cbb-odds-bot"; git config user.email "actions@users.noreply.github.com"
git add -A && git commit -q -m "Odds build $BUILD $(head -c 60 pull.json)"
git push -q -f "https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git" odds-raw && echo "published build $BUILD"
