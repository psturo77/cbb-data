"""One command for the Claude daily task: grade the ledger and props, then lay out every board write.

usage: python3 daily_write.py <board-data dir> <repo dir> <db dir> <out dir>
<board-data dir>: the repo's board-data branch (built by the "CBB daily data" Action): manifest.json, board_*.json,
                  slate_d*.json and mbb/ (small schedule + player box copies for grading).
<repo dir>:       the main branch (pipeline/ scripts, lines.csv); recs.csv / scorecard.json / props.csv are written here.
<db dir>:         what ArtifactData saved: rec/*.json and prop/*.json (all of them), plus versions_board.txt and
                  versions_slate.txt holding the text of the "list" results for collections board and slate, pasted as-is.
Writes <out>/batch1.json, batch2.json ... ({"writes": [...]} for ArtifactData "batch", in order), <out>/runlog.json and
<out>/reply.txt. If the Action hasn't published today's data, the only write is board/runlog and the reply says so.
"""
import sys, os, re, glob, json, subprocess
import pandas as pd

BD, REPO, DB, OUT = [os.path.abspath(a) for a in sys.argv[1:5]]
HERE = os.path.dirname(os.path.abspath(__file__))
os.makedirs(OUT, exist_ok=True)
for f in glob.glob(f"{OUT}/batch*.json"): os.remove(f)
now = pd.Timestamp.now(tz="UTC"); today = now.tz_convert("America/New_York").strftime("%Y-%m-%d")

def versions(fname):
    """Pull id -> version out of pasted ArtifactData list results (both the long and the compact form)."""
    p = f"{DB}/{fname}"
    if not os.path.exists(p): return {}
    t = open(p).read(); v = {}
    for i, n in re.findall(r'"([A-Za-z0-9_\-.]+)"\s+\d+ bytes\s+version (\d+)', t): v[i] = int(n)
    for i, n in re.findall(r'"([A-Za-z0-9_\-.]+)" (\d+)(?=[,\]])', t): v.setdefault(i, int(n))
    return v
VB, VS = versions("versions_board.txt"), versions("versions_slate.txt")

def entry(coll, doc, path, vers):
    e = dict(op="set", collection=coll, doc_id=doc, file_path=path)
    if doc in vers: e["if_version"] = vers[doc]
    return e

man = json.load(open(f"{BD}/manifest.json")) if os.path.exists(f"{BD}/manifest.json") else {}
lines, writes, board_txt = [], [], "written"
if man.get("today") != today:
    board_txt = f"not written - the GitHub Action hasn't published today's data (latest: {man.get('today', 'none')})"
    lines.append(f"BOARD NOT UPDATED: {board_txt}. Check the \"CBB daily data\" run on GitHub (it fails when its sanity checks fail).")
    checks = "unknown (no data from the Action today)"
else:
    checks = man.get("checks", "passed")
    lines += man.get("lines", [])
    def run(script, args):
        p = subprocess.run([sys.executable, f"{HERE}/{script}"] + args, capture_output=True, text=True)
        out = [l for l in p.stderr.splitlines() if l.strip()]
        return p.returncode, out
    # ledger + scorecard
    sc = None
    if glob.glob(f"{DB}/rec/*.json"):
        rc, out = run("grade_recs.py", [f"{BD}/mbb", REPO, f"{DB}/rec"])
        if rc == 0:
            lines += [l for l in out if l.startswith("board recs CLV:")] + [l for l in out if l.startswith("board recs:")]
            if os.path.exists(f"{REPO}/scorecard.json"): sc = f"{REPO}/scorecard.json"
        else: lines.append("grade_recs.py failed: " + (out[-1] if out else f"exit {rc}"))
    else: lines.append("board recs: none recorded yet")
    if glob.glob(f"{DB}/prop/*.json"):
        rc, out = run("grade_props.py", [f"{BD}/mbb", REPO, f"{DB}/prop"])
        lines.append(([l for l in out if l.startswith("props:")] or ["props grading: no summary"])[-1] if rc == 0
                     else "grade_props.py failed: " + (out[-1] if out else f"exit {rc}"))
    else: lines.append("props: none recorded yet")
    for name in ("meta", "teams", "espn", "players", "props"):
        f = f"{BD}/board_{name}.json"
        if os.path.exists(f): writes.append(entry("board", name, f, VB))
    if sc: writes.append(entry("board", "scorecard", sc, VB))
    have = set()
    for f in sorted(glob.glob(f"{BD}/slate_d*.json")):
        doc = os.path.basename(f)[6:-5]; have.add(doc); writes.append(entry("slate", doc, f, VS))
    cut = (pd.Timestamp(today) - pd.Timedelta(days=14)).strftime("%Y-%m-%d")
    for doc, v in sorted(VS.items()):
        if doc not in have and re.fullmatch(r"d\d{4}-\d\d-\d\d", doc) and doc[1:] < cut:
            writes.append(dict(op="delete", collection="slate", doc_id=doc, if_version=v))

runlog = dict(at=now.strftime("%Y-%m-%dT%H:%M:%SZ"), checks=checks, board=board_txt, lines=lines)
json.dump(runlog, open(f"{OUT}/runlog.json", "w"), indent=1)
writes.append(entry("board", "runlog", f"{OUT}/runlog.json", VB))
batches = [writes[i:i + 50] for i in range(0, len(writes), 50)]
for i, b in enumerate(batches, 1): json.dump({"writes": b}, open(f"{OUT}/batch{i}.json", "w"), indent=1)
reply = "\n".join(lines) + ("\nBoard updated." if board_txt == "written" else "")
open(f"{OUT}/reply.txt", "w").write(reply + "\n")
print(reply)
print(f"[{len(batches)} batch file(s), {len(writes)} writes]", file=sys.stderr)
