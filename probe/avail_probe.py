"""One-off probe: find each D1 conference's men's basketball availability-report page and the widget/data URLs it loads.
Polite: 5 s between requests, stdlib only. Output: probe_out/report.json + raw HTML of the report pages."""
import json, os, re, time, urllib.request, urllib.parse
UA = "Mozilla/5.0 (personal CBB model; availability probe)"
CONF = ["theacc.com", "bigeast.com", "bigten.org", "big12sports.com", "secsports.com", "theamerican.org", "atlantic10.com",
        "themw.com", "wccsports.com", "pac-12.com", "mvc-sports.com", "conferenceusa.com", "sunbeltsports.org", "mac-sports.com",
        "horizonleague.org", "caasports.com", "bigwest.org", "bigskyconf.com", "americaeast.com", "atlanticsun.org",
        "bigsouthsports.com", "ivyleague.com", "maacsports.com", "meacsports.com", "northeastconference.org", "ovcsports.com",
        "patriotleague.org", "southland.org", "soconsports.com", "thesummitleague.org", "swac.org", "wacsports.com", "asunsports.org"]
KNOWN = {"bigten.org": ["https://bigten.org/sports/2026/11/24/MBB_1124263447.aspx", "https://bigten.org/sports/2026/11/24/MBB_1124263601.aspx"],
         "theacc.com": ["https://theacc.com/sports/2025/11/14/availability-reporting-mbb"],
         "atlantic10.com": ["https://atlantic10.com/sports/2025/12/3/availability-reporting-mbb-current"]}
OUT = "probe_out"; os.makedirs(OUT, exist_ok=True)
def get(url):
    time.sleep(5)
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30)
        return r.geturl(), r.read().decode("utf-8", "replace")
    except Exception as e:
        return url, "ERR " + repr(e)[:200]
def href(h):
    out = []
    for a, t in re.findall(r'<a\b([^>]*)>(.*?)</a>', h, re.I | re.S):
        m = re.search(r'href="([^"]+)"', a)
        if m and re.search(r"availab", m.group(1) + " " + re.sub(r"<[^>]+>", " ", t), re.I): out.append(m.group(1).replace("&amp;", "&"))
    return out
rep = {}
for d in CONF:
    base = "https://" + d
    r = {"pages": {}}
    links = set()
    for p in ("/", "/index.aspx?path=mbball", "/sports/mens-basketball"):
        u, h = get(base + p)
        r["pages"][p] = {"final": u, "len": len(h), "err": h[:200] if h.startswith("ERR") else None}
        links |= {urllib.parse.urljoin(u, x) for x in href(h)}
    links |= set(KNOWN.get(d, []))
    r["links"] = sorted(links)
    r["reports"] = {}
    for L in [x for x in sorted(links) if not re.search(r"wbb|women|football|fb_|vball|baseball|softball", x, re.I)][:6]:
        u, h = get(L)
        fn = re.sub(r"[^a-z0-9]+", "_", u.lower())[:120] + ".html"
        open(f"{OUT}/{fn}", "w").write(h)
        r["reports"][L] = {"final": u, "file": fn, "len": len(h),
                           "iframes": re.findall(r'<iframe[^>]*src="([^"]+)"', h, re.I),
                           "scripts": [s for s in re.findall(r'<script[^>]*src="([^"]+)"', h, re.I) if "sidearm" not in s.lower() or "avail" in s.lower()][:15],
                           "frames": {},
                           "apis": sorted(set(re.findall(r'https?://[^"\'\s<>]*(?:avail|hdi|hdintel|ic360|report)[^"\'\s<>]*', h, re.I)))[:20]}
        for fsrc in r["reports"][L]["iframes"][:2]:
            fu, fh = get(urllib.parse.urljoin(u, fsrc.replace("&amp;", "&")))
            ff = "frame_" + re.sub(r"[^a-z0-9]+", "_", fu.lower())[:110] + ".html"
            open(f"{OUT}/{ff}", "w").write(fh)
            r["reports"][L]["frames"][fu] = {"file": ff, "len": len(fh),
                "urls": sorted(set(re.findall(r'(?:https?:)?//[^"\'\s<>]+|/api/[^"\'\s<>]+', fh)))[:40]}
    rep[d] = r
    print(d, len(links), "links", flush=True)
json.dump(rep, open(f"{OUT}/report.json", "w"), indent=1)
