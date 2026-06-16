"""H4 net-yield model for Polymarket LP (liquidity-reward) market-making.

For each candidate reward market, estimate NET yield = reward income - adverse
selection, using only point-in-time / historical data (no look-ahead).

Economics (stated assumptions):
  * To quote size S two-sided you post a YES bid (p*S) + NO bid ((1-p)*S) =>
    capital locked ~= $S per S shares quoted each side.
  * Reward share ~= my balanced size / (existing balanced qualifying size + mine)
    (size-share proxy for qScore, which also weights closeness-to-mid & 2-sidedness).
  * Small-maker reward yield/mo ~= 30 * daily_pool / existing_qualifying_shares.
  * Adverse selection: a passive 2-sided maker profits from the spread when flow is
    MEAN-REVERTING and bleeds when it TRENDS (informed). We measure:
       sigma_d = realised daily vol of the mid (price units)
       VR      = variance ratio Var(k-step)/(k*Var(1-step));  >1 trend, <1 revert
    Conservative net haircut (per month, in price-yield terms) only applied when
    VR>1: haircut ~= turnover_factor * sigma_d * (VR-1) * 30 / capital_per_share.
    For VR<=1 the trading P&L is >=0 so reward is ~pure (haircut=0). This is a
    SCREEN, not a validated net number — only paper/live quoting proves the net.

Usage: python analysis/lp_net_yield.py [num_markets]
"""
from __future__ import annotations
import json, math, statistics, urllib.request, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REWARDS = Path("/tmp/pm_rewards.json")
UA = {"User-Agent": "curl/8.5.0"}
CLOB = "https://clob.polymarket.com"


def get(url):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=25).read())


def rate(m):
    return sum(x.get("rewards_daily_rate", 0)
               for x in (m.get("rewards") or {}).get("rates") or [])


def book_competition(token, max_spread):
    """existing balanced qualifying shares (min of bid/ask side within max_spread)."""
    b = get(f"{CLOB}/book?token_id={token}")
    bids = [(float(o["price"]), float(o["size"])) for o in b.get("bids", [])]
    asks = [(float(o["price"]), float(o["size"])) for o in b.get("asks", [])]
    if not bids or not asks:
        return None
    bb, ba = max(p for p, _ in bids), min(p for p, _ in asks)
    mid = (bb + ba) / 2
    sp = max_spread / 100.0
    qb = sum(s for p, s in bids if (mid - p) <= sp)
    qa = sum(s for p, s in asks if (p - mid) <= sp)
    return mid, min(qb, qa), (ba - bb)


def vol_and_vr(token):
    """realised daily vol (price units) + variance ratio (6h vs 1h) from hourly history."""
    h = get(f"{CLOB}/prices-history?market={token}&interval=1m&fidelity=60").get("history", [])
    px = [pt["p"] for pt in h]
    if len(px) < 48:
        return None
    r1 = [px[i + 1] - px[i] for i in range(len(px) - 1)]          # 1h increments
    k = 6
    rk = [px[i + k] - px[i] for i in range(len(px) - k)]          # 6h increments
    v1 = statistics.pvariance(r1)
    if v1 == 0:
        return 0.0, 1.0
    vk = statistics.pvariance(rk)
    vr = vk / (k * v1)
    sigma_hourly = math.sqrt(v1)
    sigma_daily = sigma_hourly * math.sqrt(24)
    return sigma_daily, vr


def main():
    nmax = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    data = json.loads(REWARDS.read_text())["data"]
    cand = [m for m in data
            if m.get("accepting_orders") and not m.get("closed")
            and 10 <= rate(m) <= 600          # moderate pools (skip mega-farmed & dust)
            and m.get("tokens")]
    cand.sort(key=rate, reverse=True)
    # spread across the pool range so we see the distribution, not just the top
    step = max(1, len(cand) // nmax)
    sample = cand[::step][:nmax]
    print(f"candidates(10-600$/day)={len(cand)}  sampling {len(sample)}\n")

    S = 200  # test maker size per side (shares); capital ~= $200
    rows = []
    for m in sample:
        tok = next((t for t in m["tokens"] if t.get("outcome") == "Yes"),
                   m["tokens"][0])["token_id"]
        r = m["rewards"]; pool = rate(m)
        try:
            comp = book_competition(tok, r["max_spread"])
            vv = vol_and_vr(tok)
        except Exception as e:
            continue
        if not comp or not vv:
            continue
        mid, qual, raw_spread = comp
        sigma_d, vr = vv
        # reward yield/mo for a maker adding S shares balanced:
        share = S / (qual + S)
        rew_day = share * pool
        cap = S  # ~$S capital for S shares two-sided
        rew_mo_pct = rew_day * 30 / cap * 100
        # adverse haircut (only if trending): turnover ~1 fill/day per side scale
        haircut_mo_pct = 0.0
        if vr > 1:
            haircut_mo_pct = (sigma_d * (vr - 1)) * 30 / 1.0 * 100  # per $ of price move
        net_mo_pct = rew_mo_pct - haircut_mo_pct
        rows.append(dict(q=m["question"][:40], pool=pool, qual=qual, mid=mid,
                         sigma=sigma_d, vr=vr, rew=rew_mo_pct, hc=haircut_mo_pct,
                         net=net_mo_pct, min_size=r.get("min_size", 0)))

    rows.sort(key=lambda x: x["net"], reverse=True)
    print(f"{'pool/d':>6} {'qualSh':>8} {'σ_day':>6} {'VR':>5} {'rew%/mo':>8} "
          f"{'hc%/mo':>7} {'NET%/mo':>8}  market")
    for x in rows:
        flag = "MR" if x["vr"] <= 1.0 else "tr"
        print(f"{x['pool']:6.0f} {x['qual']:8.0f} {x['sigma']:6.3f} {x['vr']:5.2f} "
              f"{x['rew']:8.1f} {x['hc']:7.1f} {x['net']:8.1f} {flag}  {x['q']}")
    lpf = [x for x in rows if x["vr"] <= 1.0 and x["net"] > 0]
    print(f"\nLP-friendly (VR<=1, net>0): {len(lpf)}/{len(rows)} sampled")
    if lpf:
        nets = sorted(x["net"] for x in lpf)
        print(f"  net %/mo among LP-friendly: median {statistics.median(nets):.1f}  "
              f"min {nets[0]:.1f}  max {nets[-1]:.1f}")
        cap_day = sum(x['pool'] * (S/(x['qual']+S)) for x in lpf)
        print(f"  capturable reward at S={S}sh each (~${S*len(lpf)} capital): "
              f"${cap_day:.1f}/day = ${cap_day*30:.0f}/mo gross across {len(lpf)} mkts")

    # ---- portfolio allocation: how much would $B actually earn? ----
    def allocate(rows, budget, step=10):
        # capital ~= $1/share two-sided. min_size is a lumpy ENTRY threshold:
        # entering a market costs $min_size before it earns anything.
        lpf = [i for i, x in enumerate(rows) if x["vr"] <= 1.0 and x["min_size"] > 0]
        alloc = {i: 0.0 for i in lpf}
        def rew(i, a):
            x = rows[i]
            return x["pool"] * (a / (x["qual"] + a)) if a >= x["min_size"] else 0.0
        spent = 0.0
        # 1) enter markets as min_size blocks, best yield-per-$ first
        entries = sorted(lpf, key=lambda i: rew(i, rows[i]["min_size"]) / rows[i]["min_size"],
                         reverse=True)
        for i in entries:
            ms = rows[i]["min_size"]
            if spent + ms <= budget:
                alloc[i] = ms
                spent += ms
        # prudent per-market cap: don't exceed ~50% of the post-entry book (jump risk)
        cap = {i: max(rows[i]["min_size"], rows[i]["qual"]) for i in lpf}
        # 2) spend the remainder in $step increments by marginal reward/$
        while spent + step <= budget:
            best, bestmr = None, 0.0
            for i in lpf:
                if alloc[i] < rows[i]["min_size"] or alloc[i] + step > cap[i]:
                    continue
                mr = (rew(i, alloc[i] + step) - rew(i, alloc[i])) / step
                if mr > bestmr:
                    best, bestmr = i, mr
            if best is None:
                break
            alloc[best] += step
            spent += step
        total_day = sum(rew(i, alloc[i]) for i in lpf)
        used = {i: alloc[i] for i in alloc if alloc[i] > 0}
        return spent, total_day, used

    print(f"\n  capacity curve (sample of {len(rows)} mkts, {len(lpf)} LP-friendly; "
          f"cap=50% of book):")
    for B in (500, 1000, 5000, 25000):
        spent, day, used = allocate(rows, B)
        mo = day * 30
        print(f"  [$ {B:6}] deployed ${spent:7.0f} across {len(used):2} mkts -> "
              f"${mo:6.0f}/mo gross  ({mo/spent*100:5.1f}%/mo on deployed)"
              if spent else f"  [$ {B}] nothing deployed")


if __name__ == "__main__":
    main()
