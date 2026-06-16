"""H2 scanner — non-gubbing pair: OddsPapi *Pinnacle* (sharp) vs own *Betfair* exchange.

Point-in-time, NO look-ahead: Pinnacle de-vigged 1X2 is the contemporaneous fair
line; Betfair best back/lay (live) is the tradeable side. Closing line never used.

Edge logic (per 1X2 outcome):
  p   = Pinnacle de-vigged fair prob (multiplicative de-vig of market 101)
  back= Betfair best available-to-back odds; lay = best available-to-lay odds
  back-value (BUY the exchange cheaper than sharp-fair, net of commission):
        net_edge = p * (1 + (back-1)*(1-comm)) - 1   ; size = min(pin_limit, back_size)
  lay-value (SELL the exchange dearer than sharp-fair):
        liability-adjusted; reported as lay_edge = (1 - p*lay)/(lay-1) style not needed
        here — we report whether lay < fair_odds (you can lay below true price).

Pinnacle `limit` = max stake the sharp accepts = the depth proxy for the recycle model.

Usage:  python analysis/arb_scan.py 16 20 390        # tournament ids
        (mind the ~1 req/s OddsPapi odds rate limit; few ids per run)
"""
from __future__ import annotations
import os, sys, json, time, re, urllib.request, urllib.parse, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = "https://api.oddspapi.io"
KEY = os.environ["ODDSPAPI_KEY"]
UA = {"User-Agent": "curl/8.5.0", "Accept": "*/*"}
M_1X2 = "101"           # OddsPapi canonical full-time 1X2 (period-0 moneyline)
PART_CACHE = ROOT / "research/cache/participants_soccer.json"


def opp_get(path, **params):
    params["apiKey"] = KEY
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


DEVIG = os.environ.get("DEVIG", "shin")   # "mult" or "shin"


def devig(prices):
    inv = [1.0 / p for p in prices]
    s = sum(inv)
    if DEVIG == "mult":
        return [x / s for x in inv], s
    # Shin (1993) de-vig — corrects favourite-longshot bias via an insider proportion z.
    # p_i(z) = [sqrt(z^2 + 4(1-z) q_i^2 / B) - z] / (2(1-z)),  q_i=1/o_i, B=sum q_i.
    B = s
    def psum(z):
        if z <= 0:
            return sum(q / (B ** 0.5) for q in inv)  # limit form at z->0
        return sum((( (z*z + 4*(1-z)*q*q / B) ** 0.5) - z) / (2*(1-z)) for q in inv)
    lo, hi = 1e-6, 0.999
    for _ in range(60):                      # bisection: psum is decreasing in z
        mid = (lo + hi) / 2
        if psum(mid) > 1:
            lo = mid
        else:
            hi = mid
    z = (lo + hi) / 2
    p = [(((z*z + 4*(1-z)*q*q / B) ** 0.5) - z) / (2*(1-z)) for q in inv]
    tot = sum(p)
    return [x / tot for x in p], s          # renormalise tiny residual


def norm(s: str) -> str:
    s = s.lower()
    for junk in (" fc", " cf", " sc", " afc", " club", ".", "'"):
        s = s.replace(junk, " ")
    return re.sub(r"\s+", " ", s).strip()


def pinnacle_1x2(tournament_ids):
    """-> list of {fid, start, home, away, prices:[H,D,A], limits:[..], fair:[..], over}"""
    names = json.loads(PART_CACHE.read_text())
    out = []
    for i, tid in enumerate(tournament_ids):
        try:
            data = opp_get("/v4/odds-by-tournaments", tournamentIds=str(tid),
                           bookmaker="pinnacle")
        except urllib.error.HTTPError as e:
            print(f"  [oddspapi tid={tid}] HTTP {e.code}: {e.read().decode()[:120]}")
            data = []
        fixtures = data if isinstance(data, list) else data.get("data", [])
        for fx in fixtures:
            pin = (fx.get("bookmakerOdds") or {}).get("pinnacle")
            if not pin or pin.get("suspended"):
                continue
            mk = (pin.get("markets") or {}).get(M_1X2)
            if not mk or not mk.get("marketActive"):
                continue
            px = {"home": None, "draw": None, "away": None}
            lim = {"home": None, "draw": None, "away": None}
            for od in mk["outcomes"].values():
                pl = od.get("players", {}).get("0", {})
                key = pl.get("bookmakerOutcomeId")
                if key in px and pl.get("active") and pl.get("price"):
                    px[key] = pl["price"]
                    lim[key] = pl.get("limit")
            if None in px.values():
                continue
            prices = [px["home"], px["draw"], px["away"]]
            fair, over = devig(prices)
            out.append(dict(
                fid=fx["fixtureId"], start=fx["startTime"],
                home=names.get(str(fx["participant1Id"]), f"#{fx['participant1Id']}"),
                away=names.get(str(fx["participant2Id"]), f"#{fx['participant2Id']}"),
                prices=prices, limits=[lim["home"], lim["draw"], lim["away"]],
                fair=fair, over=over,
            ))
        if i < len(tournament_ids) - 1:
            time.sleep(1.2)
    return out


def betfair_match_odds():
    """-> list of dicts with Betfair Match Odds market + live best back/lay per runner."""
    from betfairlightweight import filters
    from src.settings import load_settings
    from src.betfair_client import make_client
    cfg = load_settings()
    client = make_client(cfg)
    comm = float(cfg.get("signals", {}).get("betfair_commission", 0.05))

    et = client.betting.list_event_types(filter=filters.market_filter())
    soccer = next((r.event_type.id for r in et
                   if r.event_type.name.lower() == "soccer"), None)
    if not soccer:
        return [], comm, client
    cats = client.betting.list_market_catalogue(
        filter=filters.market_filter(event_type_ids=[soccer],
                                      market_type_codes=["MATCH_ODDS"]),
        market_projection=["EVENT", "MARKET_START_TIME", "RUNNER_DESCRIPTION"],
        sort="MAXIMUM_TRADED", max_results=200,
    )
    catalog = {c.market_id: c for c in cats}
    pp = filters.price_projection(price_data=["EX_BEST_OFFERS"])
    markets = []
    ids = list(catalog)
    for i in range(0, len(ids), 25):
        chunk = ids[i:i + 25]
        for book in client.betting.list_market_book(market_ids=chunk,
                                                     price_projection=pp):
            c = catalog[book.market_id]
            runners = {}
            for r in (book.runners or []):
                cr = next((x for x in c.runners
                           if x.selection_id == r.selection_id), None)
                ex = r.ex
                runners[cr.runner_name if cr else str(r.selection_id)] = dict(
                    back=(ex.available_to_back[0].price if ex and ex.available_to_back else None),
                    back_sz=(ex.available_to_back[0].size if ex and ex.available_to_back else 0.0),
                    lay=(ex.available_to_lay[0].price if ex and ex.available_to_lay else None),
                    lay_sz=(ex.available_to_lay[0].size if ex and ex.available_to_lay else 0.0),
                )
            markets.append(dict(
                event=c.event.name if c.event else "",
                start=c.market_start_time,
                matched=float(book.total_matched or 0),
                runners=runners,
            ))
    return markets, comm, client


def find_bf(pin_fx, bf_markets):
    """Match a Pinnacle fixture to a Betfair Match Odds market by team-name tokens."""
    h, a = norm(pin_fx["home"]), norm(pin_fx["away"])
    ht, at = set(h.split()), set(a.split())
    for m in bf_markets:
        ev = norm(m["event"])
        if (ht & set(ev.split())) and (at & set(ev.split())):
            return m
    return None


def main():
    tids = sys.argv[1:] or ["16"]
    print(f"[1] Pinnacle 1X2 via OddsPapi for tournaments {tids} ...")
    pins = pinnacle_1x2(tids)
    print(f"    {len(pins)} priced fixtures.")
    if not pins:
        print("    nothing to scan (off-season / no Pinnacle coverage).")
        return
    print(f"[2] Betfair Soccer Match Odds (live best back/lay) ...")
    try:
        bf, comm, _ = betfair_match_odds()
    except Exception as e:
        print(f"    Betfair fetch failed: {type(e).__name__}: {e}")
        print("    -> dumping Pinnacle side only (sharp fair + depth):")
        for p in pins:
            print(f"    {p['start']} {p['home']} v {p['away']}  "
                  f"fair={[round(x,3) for x in p['fair']]} "
                  f"over={p['over']:.3f} limits={p['limits']}")
        return
    print(f"    {len(bf)} Betfair markets.  commission={comm:.3f}")

    print(f"\n[3] Value vs sharp (Betfair back > Pinnacle fair, net of commission):")
    LABELS = ["HOME", "DRAW", "AWAY"]
    hits, scanned = [], 0
    for p in pins:
        m = find_bf(p, bf)
        if not m:
            continue
        scanned += 1
        names = [p["home"], "The Draw", p["away"]]
        for i, lbl in enumerate(LABELS):
            r = m["runners"].get(names[i]) or (
                m["runners"].get("The Draw") if i == 1 else None)
            if not r or not r["back"]:
                continue
            prob = p["fair"][i]
            back = r["back"]
            net_edge = prob * (1 + (back - 1) * (1 - comm)) - 1
            if net_edge > 0:
                size = min(p["limits"][i] or 0, r["back_sz"])
                hits.append((net_edge, p, lbl, prob, back, size, m["matched"]))
    hits.sort(reverse=True)
    if not hits:
        print(f"    scanned {scanned} matched fixtures — 0 back-value outcomes vs sharp.")
    for ne, p, lbl, prob, back, size, matched in hits[:25]:
        print(f"    +{ne*100:5.2f}%  {p['home'][:16]:16} v {p['away'][:16]:16} {lbl:4} "
              f"fair={1/prob:6.2f} bfback={back:6.2f} size~£{size:6.0f} "
              f"(bf_matched£{matched:.0f})")
    print(f"\n[summary] fixtures_priced={len(pins)} matched_to_betfair={scanned} "
          f"back_value_outcomes={len(hits)}")

    # --- credibility cut: where is the 'edge' concentrated? ---
    # Real, deployable edge should survive on LIQUID Betfair markets (its price is
    # then meaningful, not a thin top-of-book) and at SANE odds (de-vig is least
    # biased for favourites/mid). Longshot-only edge on thin markets = artifact.
    def bucket(odds):
        return "fav<2.5" if odds < 2.5 else "mid2.5-6" if odds < 6 else "long>6"
    from collections import defaultdict
    agg = defaultdict(lambda: [0, 0.0, 0.0])  # band -> [n, sum_edge, sum_deployable£]
    liq_hits = []
    for ne, p, lbl, prob, back, size, matched in hits:
        b = bucket(1 / prob)
        agg[b][0] += 1
        agg[b][1] += ne
        agg[b][2] += ne * (size or 0)        # edge-weighted deployable £
        if matched > 50_000 and 1 / prob < 6 and (size or 0) >= 20:
            liq_hits.append((ne, p, lbl, prob, back, size, matched))
    print("    edge by odds band:  band  n  mean_edge  Σ(edge×£size)")
    for b in ("fav<2.5", "mid2.5-6", "long>6"):
        n, se, sd = agg[b]
        if n:
            print(f"      {b:9} n={n:2}  mean={se/n*100:5.2f}%  Σedge£={sd:8.1f}")
    print(f"\n[credible] liquid(>£50k) & odds<6 & size≥£20 — the deployable subset:")
    if not liq_hits:
        print("      NONE. All apparent edge is longshot/thin-liquidity → likely artifact.")
    for ne, p, lbl, prob, back, size, matched in sorted(liq_hits, reverse=True):
        print(f"      +{ne*100:5.2f}%  {p['home'][:14]:14} v {p['away'][:14]:14} {lbl:4} "
              f"fair={1/prob:5.2f} bfback={back:5.2f} £{size:5.0f} matched£{matched/1000:.0f}k")


if __name__ == "__main__":
    main()
