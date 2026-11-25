"""One-off probe: find each D1 conference's men's basketball availability-report page and the widget/data URLs it loads.
Polite: 5 s between requests, stdlib only. Output: probe_out/report.json + raw HTML of the report pages."""
import json, os, re, time, urllib.request, urllib.parse
UA = "Mozilla/5.0 (personal CBB model; availability probe)"
CONF = ["theacc.com", "bigeast.com", "bigten.org", "big12sports.com", "secsports.com", "theamerican.org", "atlantic10.com",
        "themw.com", "wccsports.com", "pac-12.com", "mvc-sports.com", "conferenceusa.com", "sunbeltsports.org", "mac-sports.com",
        "horizonleague.org", "caasports.com", "bigwest.org", "bigskyconf.com", "americaeast.com", "atlanticsun.org",
        "bigsouthsports.com", "ivyleague.com", "maacsports.com", "meacsports.com", "northeastconference.org", "ovcsports.com",
        "patriotleague.org", "southland.org", "soconsports.com", "thesummitleague.org", "swac.org", "wacsports.com"]
OUT = "probe_out"; os.makedirs(OUT, exist_ok=True)
def get(url):
    time.sleep(5)
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30)
        return r.geturl(), r.read().decode("utf-8", "replace")
    except Exception as e:
        return url, "ERR " + repr(e)[:200]
href = lambda h: re.findall(r'href="([^"]*availab[^"]*)"', h, re.I)
rep = {}
for d in CONF:
    base = "https://" + d
    r = {"pages": {}}
    links = set()
    for p in ("/", "/index.aspx?path=mbball", "/sports/mens-basketball"):
        u, h = get(base + p)
        r["pages"][p] = {"final": u, "len": len(h), "err": h[:200] if h.startswith("ERR") else None}
        links |= {urllib.parse.urljoin(u, x) for x in href(h)}
    r["links"] = sorted(links)
    r["reports"] = {}
    for L in sorted(links)[:8]:
        if "wbb" in L.lower() or "women" in L.lower() or "football" in L.lower(): continue
        u, h = get(L)
        fn = re.sub(r"[^a-z0-9]+", "_", u.lower())[:120] + ".html"
        open(f"{OUT}/{fn}", "w").write(h)
        r["reports"][L] = {"final": u, "file": fn, "len": len(h),
                           "iframes": re.findall(r'<iframe[^>]*src="([^"]+)"', h, re.I),
                           "scripts": [s for s in re.findall(r'<script[^>]*src="([^"]+)"', h, re.I) if "sidearm" not in s.lower() or "avail" in s.lower()][:15],
                           "apis": sorted(set(re.findall(r'https?://[^"\'\s<>]*(?:avail|hdi|hdintel|ic360|report)[^"\'\s<>]*', h, re.I)))[:20]}
    rep[d] = r
    print(d, len(links), "links", flush=True)
json.dump(rep, open(f"{OUT}/report.json", "w"), indent=1)
