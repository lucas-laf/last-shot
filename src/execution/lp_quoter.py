"""Polymarket LP (liquidity-reward) quoter — SHADOW core + replay backtest.

Seed of the live pilot bot (docs/LP_PILOT_SPEC.md). Runs against LIVE Polymarket data
but **places nothing** — no client, no money, no arming. Two shadow modes:

  plan <budget>      one live snapshot: the orders it WOULD post right now.
  backtest <budget>  replay each selected market over 31d of real hourly history,
                     simulating re-quote / fills / inventory / reward accrual /
                     jump-halt -> a simulated NET (reward - inventory P&L) per market.

SHADOW CAVEAT: fills and reward share are MODELLED (price-crossing fills, size-share
reward proxy), not on-chain truth. Realized capture-rate / fills / inventory P&L still
require the tiny real Stage 0 (~$150). The replay's value is decomposing net into
reward vs trading-P&L and flagging which markets the inventory risk would sink.
Run:  python -m src.execution.lp_quoter [plan|backtest] [budget]
"""
from __future__ import annotations
import json, statistics, sys, urllib.request

UA = {"User-Agent": "curl/8.5.0"}
CLOB = "https://clob.polymarket.com"

# --- quoting / risk params (the pilot would sweep these) ---
HALF_SPREAD_TICKS = 1      # quote this many ticks inside the mid (tight = more qScore)
INV_CAP_MULT = 3.0         # max inventory = this * size before we skew to flatten
JUMP_HALT = 0.20           # if mid moves >this fraction in 1 step, pull quotes that step
KILL_DRAWDOWN = 0.15       # halt a market if its sim net <= -this * its capital


def _get(url):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=25).read())


def _rate(m):
    return sum(x.get("rewards_daily_rate", 0)
               for x in (m.get("rewards") or {}).get("rates") or [])


def _snap(price, tick):
    return round(round(price / tick) * tick, 10)


def scan_candidates(scan_limit=60, min_pool=10.0, max_min_size=200, hurdle_mo_pct=10.0):
    """Fetch reward markets, keep LP-friendly ones (VR<1) with book + price series."""
    mkts = [m for m in _get(f"{CLOB}/sampling-markets")["data"]
            if m.get("accepting_orders") and not m.get("closed") and m.get("tokens")
            and min_pool <= _rate(m)
            and (m["rewards"].get("min_size") or 1e9) <= max_min_size]
    mkts.sort(key=_rate, reverse=True)
    cands = []
    for m in mkts[:scan_limit]:
        tok = next((t for t in m["tokens"] if t.get("outcome") == "Yes"),
                   m["tokens"][0])["token_id"]
        try:
            b = _get(f"{CLOB}/book?token_id={tok}")
            hist = _get(f"{CLOB}/prices-history?market={tok}&interval=1m&fidelity=60")
        except Exception:
            continue
        bids = [(float(o["price"]), float(o["size"])) for o in b.get("bids", [])]
        asks = [(float(o["price"]), float(o["size"])) for o in b.get("asks", [])]
        series = [pt["p"] for pt in hist.get("history", [])]
        if not bids or not asks or len(series) < 48:
            continue
        bb, ba = max(p for p, _ in bids), min(p for p, _ in asks)
        mid = (bb + ba) / 2
        sp = m["rewards"]["max_spread"] / 100.0
        qual = min(sum(s for p, s in bids if (mid - p) <= sp),
                   sum(s for p, s in asks if (p - mid) <= sp))
        # variance ratio (6h/1h): <1 mean-reverting (LP-friendly)
        r1 = [series[i + 1] - series[i] for i in range(len(series) - 1)]
        rk = [series[i + 6] - series[i] for i in range(len(series) - 6)]
        v1 = statistics.pvariance(r1)
        vr = (statistics.pvariance(rk) / (6 * v1)) if v1 > 0 else 1.0
        ms = m["rewards"]["min_size"]
        est_yield = 30 * _rate(m) / (qual + ms) * 100
        if vr > 1.0 or est_yield < hurdle_mo_pct:
            continue
        no_tok = next((t["token_id"] for t in m["tokens"]
                       if t.get("outcome") == "No"), None)
        cands.append(dict(q=m["question"], pool=_rate(m), min_size=ms,
                          tick=float(m.get("minimum_tick_size", 0.01)),
                          max_spread=m["rewards"]["max_spread"], mid=mid, qual=qual,
                          vr=vr, est_yield=est_yield, series=series,
                          condition_id=m.get("condition_id"), yes_token=tok,
                          no_token=no_tok, end_date=m.get("end_date_iso")))
    return cands


def allocate(cands, budget):
    """Greedy block-entry by est yield; capital ~= $min_size per market."""
    chosen, spent = [], 0.0
    for c in sorted(cands, key=lambda x: x["est_yield"], reverse=True):
        if spent + c["min_size"] <= budget:
            chosen.append(c)
            spent += c["min_size"]
    return chosen, spent


def simulate(c):
    """Replay 31d hourly history: re-quote to mid, sim fills on crossings, accrue
    reward, skew inventory, jump-halt. Returns reward/trading/net P&L decomposition."""
    s = c["series"]; S = c["min_size"]; h = HALF_SPREAD_TICKS * c["tick"]
    pool, qual = c["pool"], c["qual"]
    inv = 0.0; cash = 0.0; reward = 0.0; fills = 0; max_abs_inv = 0.0
    rew_hr = pool * S / (qual + S) / 24.0           # hourly reward share (when quoting)
    cap = INV_CAP_MULT * S
    halted = False
    for t in range(len(s) - 1):
        mid, nxt = s[t], s[t + 1]
        if mid <= 0 or mid >= 1:
            continue
        if abs(nxt - mid) / mid > JUMP_HALT:        # jump: we pulled quotes this step
            continue
        reward += rew_hr
        bid, ask = mid - h, mid + h
        quote_bid = inv < cap                       # skew: stop buying if long-capped
        quote_ask = inv > -cap                      # stop selling if short-capped
        if quote_bid and nxt < bid:                 # someone sold to our bid -> we buy
            cash -= bid * S; inv += S; fills += 1
        elif quote_ask and nxt > ask:               # someone bought our ask -> we sell
            cash += ask * S; inv -= S; fills += 1
        max_abs_inv = max(max_abs_inv, abs(inv))
        net_now = cash + inv * nxt + reward
        if net_now <= -KILL_DRAWDOWN * S:           # per-market kill-switch
            halted = True; break
    final = s[-1] if not halted else s[min(t + 1, len(s) - 1)]
    trading = cash + inv * final                    # realized + unrealized
    net = trading + reward
    days = len(s) / 24.0
    return dict(reward=reward, trading=trading, net=net, fills=fills,
                max_inv=max_abs_inv, days=days, halted=halted, final_inv=inv)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].replace(".", "").isdigit() else "backtest"
    budget = float([a for a in sys.argv[1:] if a.replace(".", "").isdigit()][0]) \
        if any(a.replace(".", "").isdigit() for a in sys.argv[1:]) else 500.0
    print(f"=== LP quoter SHADOW [{mode}] budget ${budget:.0f} — NO orders placed ===\n")
    cands = scan_candidates()
    chosen, spent = allocate(cands, budget)
    if not chosen:
        print("no qualifying markets right now."); return

    if mode == "plan":
        print(f"{'pool/d':>6} {'mid':>6} {'VR':>4} {'YESbid':>7} {'NObid':>7} "
              f"{'size':>5} {'cap$':>7}  market")
        cap_tot = rew_tot = 0.0
        for c in chosen:
            hsp = HALF_SPREAD_TICKS * c["tick"]
            yb = _snap(c["mid"] - hsp, c["tick"]); nb = _snap((1 - c["mid"]) - hsp, c["tick"])
            cap = round(c["min_size"] * yb + c["min_size"] * nb, 2)
            rew = c["pool"] * c["min_size"] / (c["qual"] + c["min_size"])
            cap_tot += cap; rew_tot += rew
            print(f"{c['pool']:6.0f} {c['mid']:6.3f} {c['vr']:4.2f} {yb:7.3f} {nb:7.3f} "
                  f"{c['min_size']:5.0f} {cap:7.2f}  {c['q'][:42]}")
        print(f"\nmarkets {len(chosen)}  capital ${cap_tot:.0f}  "
              f"est reward ${rew_tot:.2f}/day = ${rew_tot*30:.0f}/mo (size-share proxy, UNVERIFIED)")
        return

    # backtest replay
    print(f"{'pool/d':>6} {'VR':>4} {'rew$':>7} {'trade$':>7} {'NET$':>7} "
          f"{'fills':>5} {'maxInv':>6} {'halt':>4}  market")
    tot = dict(reward=0.0, trading=0.0, net=0.0)
    for c in chosen:
        r = simulate(c)
        for k in tot:
            tot[k] += r[k]
        print(f"{c['pool']:6.0f} {c['vr']:4.2f} {r['reward']:7.2f} {r['trading']:7.2f} "
              f"{r['net']:7.2f} {r['fills']:5.0f} {r['max_inv']:6.0f} "
              f"{'YES' if r['halted'] else '-':>4}  {c['q'][:40]}")
    days = statistics.mean([simulate(c)["days"] for c in chosen])
    mo = tot["net"] / days * 30
    print(f"\n--- {len(chosen)} markets, ${spent:.0f} deployed, ~{days:.0f}d replay ---")
    print(f"  reward ${tot['reward']:.0f}  +  trading ${tot['trading']:.0f}  "
          f"=  NET ${tot['net']:.0f}  over {days:.0f}d")
    print(f"  => ${mo:.0f}/mo simulated NET ({mo/spent*100:.0f}%/mo on ${spent:.0f})")
    print("\n  SHADOW: fills & reward share MODELLED, not on-chain. Decomposition shows\n"
          "  reward vs inventory(trading) P&L; realized numbers need real Stage 0 (~$150).")


if __name__ == "__main__":
    main()
