"""One-off probe v4: the HDI widget's public report/archive screens - fetch their code chunks and list the API calls."""
import json, os, re, time, urllib.request
UA = "Mozilla/5.0 (personal CBB model; availability probe)"
OUT = "probe_out"; os.makedirs(OUT, exist_ok=True)
def get(url):
    time.sleep(5)
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30)
        return r.read().decode("utf-8", "replace")
    except Exception as e:
        return "ERR " + repr(e)[:300]
rep = {}
for c in ["PublishScreen-DuPqR3BC.js", "ArchiveScreen-D3naplgR.js", "sortReportsByKickoff-Dw-8FNwp.js", "SummaryScreen-BUZxPPBF.js"]:
    j = get("https://app.hdintelligence.com/assets/" + c); open(f"{OUT}/{c}", "w").write(j)
    rep[c] = {"len": len(j), "calls": sorted(set(re.findall(r'\.(?:get|post)\(\s*[`"\']([^`"\']+)', j)))}
json.dump(rep, open(f"{OUT}/report.json", "w"), indent=1); print(json.dumps(rep))
