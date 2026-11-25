"""One-off probe v5: the HDI availability widget's PUBLIC endpoints (the same calls the conference pages make for any
visitor) for each HDI conference: public-load, today's report, this season's archive, past seasons' archive."""
import json, os, time, urllib.request
UA = "Mozilla/5.0 (personal CBB model; availability probe)"
OUT = "probe_out"; os.makedirs(OUT, exist_ok=True)
B = "https://app.hdintelligence.com"
def post(path, body, ref):
    time.sleep(5)
    req = urllib.request.Request(B + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"User-Agent": UA, "Content-Type": "application/json", "Origin": B, "Referer": ref})
    try:
        r = urllib.request.urlopen(req, timeout=60); return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:500]
    except Exception as e:
        return None, "ERR " + repr(e)[:300]
SPORT = "Men's Basketball"; rep = {}
for c in ["A10", "ACC", "B10", "B12", "BE", "MVC", "SEC"]:
    ref = f"{B}/?source={c}&sport=Men%27s%20Basketball&conf={c}&type=report"
    rep[c] = {}
    for path, body in [("/api/public-load", dict(conference_param=c, sport_param=SPORT, type_param="report", referrer="", source=c)),
                       ("/api/get-publish-public", dict(sport=SPORT, organization=c, conference=c)),
                       ("/api/get-archive-public", dict(sport=SPORT, organization=c, conference=c)),
                       ("/api/get-archive-legacy-public", dict(sport=SPORT, organization=c, conference=c))]:
        s, t = post(path, body, ref)
        fn = f"{c}_{path.split('/')[-1]}.json"; open(f"{OUT}/{fn}", "w").write(t)
        rep[c][path] = {"status": s, "len": len(t), "head": t[:300]}
    print(c, {p: (v["status"], v["len"]) for p, v in rep[c].items()}, flush=True)
json.dump(rep, open(f"{OUT}/report.json", "w"), indent=1)
