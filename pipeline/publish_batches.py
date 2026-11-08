"""Make a folder of board writes self-contained so a Claude task can copy it without running any code from this repo.

usage: python3 publish_batches.py <folder with batch*.json and reply.txt> <publish folder>
Copies every file a batch entry points at into <publish>/files/<collection>__<doc_id>.json, rewrites each entry's
file_path to that relative path, and drops if_version (the Claude task adds the board's current versions itself).
Also copies reply.txt, and writes run.json with the build time.
"""
import sys, os, glob, json, shutil, datetime

SRC, PUB = sys.argv[1], sys.argv[2]
os.makedirs(f"{PUB}/files", exist_ok=True)
for f in glob.glob(f"{PUB}/batch*.json"): os.remove(f)
n = 0
for f in sorted(glob.glob(f"{SRC}/batch*.json")):
    d = json.load(open(f))
    for w in d["writes"]:
        w.pop("if_version", None)
        if "file_path" in w:
            rel = f"files/{w['collection']}__{w['doc_id']}.json"
            shutil.copy(w["file_path"], f"{PUB}/{rel}"); w["file_path"] = rel
        n += 1
    json.dump(d, open(f"{PUB}/{os.path.basename(f)}", "w"), indent=1)
if os.path.exists(f"{SRC}/reply.txt"): shutil.copy(f"{SRC}/reply.txt", f"{PUB}/reply.txt")
json.dump({"built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, open(f"{PUB}/run.json", "w"))
print(f"{n} writes published to {PUB}", file=sys.stderr)
