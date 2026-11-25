"""One-off probe v3: the HD Intelligence availability widget (app.hdintelligence.com) - robots.txt, terms, JS bundle,
and the API endpoints the bundle calls. Polite: 5 s between requests. Output: probe_out/."""
import json, os, re, time, urllib.request
UA = "Mozilla/5.0 (personal CBB model; availability probe)"
OUT = "probe_out"; os.makedirs(OUT, exist_ok=True)
def get(url):
    time.sleep(5)
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30)
        return r.status, r.geturl(), r.read().decode("utf-8", "replace")
    except Exception as e:
        return None, url, "ERR " + repr(e)[:300]
B = "https://app.hdintelligence.com"
rep = {}
for p in ("/robots.txt", "/?source=B10&sport=Men%27s%20Basketball&conf=B10&type=report"):
    s, u, h = get(B + p); rep[p] = {"status": s, "len": len(h)}
    open(f"{OUT}/{re.sub(r'[^a-z0-9]+', '_', p.lower())[:60] or 'root'}.txt", "w").write(h)
    if p.startswith("/?"):
        for js in re.findall(r'src="\./(assets/[^"]+\.js)"', h):
            s2, u2, j = get(f"{B}/{js}"); open(f"{OUT}/bundle.js", "w").write(j)
            rep["bundle"] = {"url": u2, "status": s2, "len": len(j),
                "urls": sorted(set(re.findall(r'https?://[a-zA-Z0-9._-]+(?:/[^"\'`\s)]*)?', j)))[:80],
                "paths": sorted(set(re.findall(r'["\'`](/(?:api|v\d|reports?|availability|public)[^"\'`]*)["\'`]', j)))[:80],
                "terms": sorted(set(re.findall(r'.{0,60}(?:terms|scrap|robot|automated).{0,60}', j, re.I)))[:20]}
json.dump(rep, open(f"{OUT}/report.json", "w"), indent=1)
print(json.dumps({k: (v if k != "bundle" else {x: v[x] for x in ("url", "status", "len")}) for k, v in rep.items()}))
