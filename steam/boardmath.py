"""The board's Bets-tab math in Python (keep in step with cbb-board.html): fair line, win chances, edge, stake, timing."""
from math import erf, sqrt
import pandas as pd
# ---- the board's constants (keep in step with cbb-board.html)
W = dict(open=0.5, close=0.15, openEarly=0.15, closeEarly=0.0, tot=0.25, totEarly=0.2)
CAP, CAP_TOT, MIN_EV = 6.0, 10.0, 0.02
TOT_STAKE = 0.5    # totals staked at half (same as the board): small, over-heavy test sample; back to 1 if live CLV holds up
KELLY = 0.125      # stake = Kelly fraction (board default 'Cautious' 1/8, cap 1.5u; 'Standard' 1/4, cap 3u)
Phi = lambda x: 0.5 * (1 + erf(x / sqrt(2)))
payout = lambda p: p / 100 if p > 0 else 100 / -p
implied = lambda p: -p / (-p + 100) if p < 0 else 100 / (p + 100)
def novig(h, a):
    if h is None or a is None: return None
    x, y = implied(h), implied(a); return x / (x + y)
def chances(mean, need, sd):
    x = mean - need
    if abs(need - round(need)) < 1e-9: return Phi((x - 0.5) / sd), Phi((-x - 0.5) / sd)
    w = Phi(x / sd); return w, 1 - w


def fair(g, o, sdm=11.2):
    """The board's fair numbers for a game at odds record o: fair home margin, fair total, fair home win chance."""
    seen, start = pd.Timestamp(o["seen"]), pd.Timestamp(g["start"])
    tw = max(0.0, min(1.0, ((start - seen).total_seconds() / 3600 - 1) / 11))
    wo, wc = (W["openEarly"], W["closeEarly"]) if g.get("early") else (W["open"], W["close"])
    wsp, wt = wc + (wo - wc) * tw, (W["totEarly"] if g.get("early") else W["tot"]) * tw
    fm = ph = ft = None
    if o.get("sp") is not None:
        mkt = -o["sp"]; d = max(-CAP, min(CAP, g["pm"] - mkt)); fm = mkt + wsp * d
        nv = o.get("nvh") if o.get("nvh") is not None else novig(o.get("mlh"), o.get("mla"))
        if nv is not None: ph = min(0.995, max(0.005, nv + wsp * (Phi((mkt + d) / sdm) - Phi(mkt / sdm))))
    if o.get("tot") is not None and g.get("pt") is not None:
        ft = o["tot"] + wt * max(-CAP_TOT, min(CAP_TOT, g["pt"] - o["tot"]))
    return fm, ft, ph


def sd_for(o, sdm=11.2, sdt=16.7):
    """per-game uncertainty (same as the board's sdFor): lopsided games and high totals swing more"""
    sp = abs(o["sp"]) if o.get("sp") is not None else 0.0; tot = o["tot"] if o.get("tot") is not None else 150.0
    return sdm * (1 + 0.0063 * max(sp - 10, 0)), sdt * max(0.92, min(1.06, 1 + 0.0042 * (tot - 150)))


def bets_for(g, o, sdm=11.2, sdt=16.7, books=None):
    """Every bet on offer, best first: at the best prices among your books when o has them (o["opt"], from the
    all-books pull; books = that pull's book names), otherwise at o's single-book prices (ESPN)."""
    sdm, sdt = sd_for(o, sdm, sdt)
    fm, ft, ph = fair(g, o, sdm)
    opt, books = o.get("opt") or {}, books or []
    bk = lambda i: books[i] if i is not None and 0 <= i < len(books) else o.get("book", "")
    def opts(k, fb):
        xs = opt.get(k) or ([fb] if fb and None not in fb[:2] else [])
        return [(x[0], x[1], x[2] if len(x) > 2 else None) for x in xs]
    out = []
    if fm is not None:
        for L, P, i in opts("sh", [o.get("sp"), o.get("spp") or -110]):
            w_, l_ = chances(fm, -L, sdm); out.append(dict(market="spread", side="home", line=L, price=P, book=bk(i), p=w_, ev=w_ * payout(P) - l_))
        for L, P, i in opts("sa", [-o["sp"], o.get("spa") or o.get("spp") or -110]):
            w_, l_ = chances(-fm, -L, sdm); out.append(dict(market="spread", side="away", line=L, price=P, book=bk(i), p=w_, ev=w_ * payout(P) - l_))
    if ph is not None:
        for side, x, q in (("home", opt.get("mh") or ([o["mlh"], None] if o.get("mlh") is not None else None), ph),
                           ("away", opt.get("ma") or ([o["mla"], None] if o.get("mla") is not None else None), 1 - ph)):
            if x and -400 <= x[0] <= 300:
                out.append(dict(market="ml", side=side, line=0.0, price=x[0], book=bk(x[1]), p=q, ev=q * payout(x[0]) - (1 - q)))
    if ft is not None:
        for L, P, i in opts("ov", [o.get("tot"), o.get("op") or -110]):
            w_, l_ = chances(ft, L, sdt); out.append(dict(market="total", side="over", line=L, price=P, book=bk(i), p=w_, ev=w_ * payout(P) - l_))
        for L, P, i in opts("un", [o.get("tot"), o.get("up") or -110]):
            w_, l_ = chances(-ft, -L, sdt); out.append(dict(market="total", side="under", line=L, price=P, book=bk(i), p=w_, ev=w_ * payout(P) - l_))
    for b in out:
        b["units"] = (TOT_STAKE if b["market"] == "total" else 1.0) * max(0.0, min(12 * KELLY, 100 * KELLY * b["ev"] / payout(b["price"])))
        if o.get("anchor"):        # a book 1+ pt better for you than Pinnacle
            v = (b["line"] - o["sp"] if b["side"] == "home" else b["line"] + o["sp"]) if b["market"] == "spread" else \
                (o["tot"] - b["line"] if b["side"] == "over" else b["line"] - o["tot"]) if b["market"] == "total" else None
            if v is not None and v >= 1 - 1e-9: b["off"] = v
    return sorted(out, key=lambda b: -b["ev"])


PRIME_MOVE = 2.5
def is_prime(g, b, o):
    """PRIME: line expected to move 2.5+ pts against this price by tip-off (same as the board) - the bets ahead of the market"""
    tm = timing(g, b, o); return tm is not None and tm >= PRIME_MOVE

def caution(g, b, o):
    """CAUTION: a total where the model is also 4+ pts off the market's spread (those lost in testing); returns the gap or None"""
    if b["market"] != "total" or o.get("sp") is None or g.get("pm") is None: return None
    d = abs(g["pm"] + o["sp"]); return d if d >= 4 else None

ROAD_MIN = 0.06
def min_for(g, b, base):
    """minimum edge for a bet: road-team spread/moneyline bets (not at neutral sites) need 6% (same rule as the board)"""
    return max(base, ROAD_MIN) if b["market"] != "total" and not g.get("neutral") and b["side"] == "away" else base


def top_bets(bets):
    """the best side bet (spread or moneyline) and the best total for a game - the board shows/alerts both"""
    s = next((b for b in bets if b["market"] != "total"), None); t = next((b for b in bets if b["market"] == "total"), None)
    return [b for b in (s, t) if b]


def arbs_for(o, books=None):
    """Arbs (both sides at two books sum to under 100%) and middles (a 1+ pt gap at <= 6% combined hold)."""
    opt, books = o.get("opt") or {}, books or []
    bk = lambda i: books[i] if i is not None and 0 <= i < len(books) else ""
    dec = lambda p: 1 + payout(p); out = []
    def pair(A, B, kind, gap, sa, sb):
        for a in A or []:
            for b in B or []:
                if a[-1] == b[-1]: continue
                w = gap(a[0], b[0]); s = 1 / dec(a[1]) + 1 / dec(b[1])
                if w < -1e-9: continue
                if s < 1 - 1e-9 or (w >= 1 - 1e-9 and s <= 1.06):
                    out.append(dict(kind=kind, a=(sa, a[0], a[1], bk(a[-1])), b=(sb, b[0], b[1], bk(b[-1])), width=w, sum=s, arb=s < 1 - 1e-9))
    pair(opt.get("sh"), opt.get("sa"), "spread", lambda x, y: x + y, "home", "away")
    pair(opt.get("un"), opt.get("ov"), "total", lambda u, v: u - v, "under", "over")
    if opt.get("mh") and opt.get("ma"):
        pair([[0] + list(opt["mh"])], [[0] + list(opt["ma"])], "ml", lambda x, y: 0, "home", "away")
    return sorted(out, key=lambda x: (not x["arb"], x["sum"]))


CL = dict(m=0.601, mE=-0.239, c=-0.056, bm=0.529, bmE=-0.228, bb=0.107, bbE=-0.029, bc=-0.063, t=0.319, tc=0.292)
def timing(g, b, o):
    """(+pts) line expected to move against this price by close -> bet now; (-pts) expected to drift your way."""
    cap = lambda x, c: max(-c, min(c, x)); e = 1 if g.get("early") else 0
    if b["market"] == "spread":
        op = (o.get("open") or {}).get("sp", o.get("sp"))
        if op is None or o.get("sp") is None: return None
        dm = cap(-g["pm"] - op, 8)
        mv = ((CL["bm"] + CL["bmE"] * e) * dm + (CL["bb"] + CL["bbE"] * e) * cap(-o["bpi"] - op, 8) + CL["bc"]) if o.get("bpi") is not None \
             else (CL["m"] + CL["mE"] * e) * dm + CL["c"]
        left = op + mv - o["sp"]
        return -left if b["side"] == "home" else left
    if b["market"] == "total" and g.get("pt") is not None:
        ot = (o.get("open") or {}).get("tot", o.get("tot"))
        if ot is None or o.get("tot") is None: return None
        left = ot + CL["t"] * cap(g["pt"] - ot, 12) + CL["tc"] - o["tot"]
        return left if b["side"] == "over" else -left
    return None
