"""H1 backtest: is the best market price beatable vs Pinnacle's de-vigged closing line?

football-data.co.uk schema:
  PSCH/PSCD/PSCA  Pinnacle CLOSING odds (sharp reference)
  MaxCH/MaxCD/MaxCA  best CLOSING price across tracked books (line-shopped)
  AvgCH/AvgCD/AvgCA  average CLOSING price
  FTR  result H/D/A

Method (value-vs-sharp):
  1. de-vig Pinnacle closing -> fair prob p_i = (1/PSC_i) / sum(1/PSC)
  2. for each outcome, candidate bettable price = MaxC_i (or AvgC_i)
  3. model edge = p_i * price - 1 ; bet flat $1 when edge >= threshold
  4. settle on FTR; report realized ROI, hit rate, #bets, CLV
"""
import csv, sys, math
from pathlib import Path

def load(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    "res": r["FTR"],
                    "PSC": [float(r["PSCH"]), float(r["PSCD"]), float(r["PSCA"])],
                    "Max": [float(r["MaxCH"]), float(r["MaxCD"]), float(r["MaxCA"])],
                    "Avg": [float(r["AvgCH"]), float(r["AvgCD"]), float(r["AvgCA"])],
                    # early (pre-close) prices — the realistic value-bet entry
                    "MaxE": [float(r["MaxH"]), float(r["MaxD"]), float(r["MaxA"])],
                    "AvgE": [float(r["AvgH"]), float(r["AvgD"]), float(r["AvgA"])],
                    "PSE": [float(r["PSH"]), float(r["PSD"]), float(r["PSA"])],
                    "B365E": [float(r["B365H"]), float(r["B365D"]), float(r["B365A"])],
                })
            except (ValueError, KeyError):
                continue
    return rows

OUTCOMES = ["H", "D", "A"]

def devig(psc):
    inv = [1.0/o for o in psc]
    s = sum(inv)
    return [x/s for x in inv], s  # fair probs, overround

def backtest(rows, price_key, thresh, fair_key="PSC"):
    """fair_key = which line to de-vig for 'true' prob. Use 'PSE' (Pinnacle EARLY)
    for a no-look-ahead signal; 'PSC' (closing) is a hindsight/efficiency check.
    CLV is always measured vs Pinnacle CLOSING (PSC) regardless of fair_key."""
    n_bets = wins = 0
    stake = pnl = 0.0
    clv_sum = 0.0  # ln(price / pinnacle_closing) — closing line value proxy
    for r in rows:
        fair, _ = devig(r[fair_key])
        for i, oc in enumerate(OUTCOMES):
            p = fair[i]
            price = r[price_key][i]
            edge = p * price - 1.0
            if edge < thresh:
                continue
            n_bets += 1
            stake += 1.0
            clv_sum += math.log(price / r["PSC"][i])
            if r["res"] == oc:
                wins += 1
                pnl += price - 1.0
            else:
                pnl -= 1.0
    roi = pnl / stake if stake else 0.0
    hit = wins / n_bets if n_bets else 0.0
    clv = clv_sum / n_bets if n_bets else 0.0
    return dict(bets=n_bets, roi=roi, pnl=pnl, hit=hit, clv=clv)

def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "research/data/E0_2324.csv"
    rows = load(path)
    overrounds = [devig(r["PSC"])[1] for r in rows]
    print(f"file={path}  matches={len(rows)}  "
          f"mean Pinnacle overround={sum(overrounds)/len(overrounds):.4f}")
    def run(title, price_key, fair_key):
        print(f"\n== {title} ==")
        print(f"{'price':>6} {'thr':>5} {'bets':>5} {'ROI':>8} {'pnl$':>8} {'hit%':>6} {'CLV':>8}")
        for thr in (0.0, 0.01, 0.02, 0.03, 0.05):
            r = backtest(rows, price_key, thr, fair_key=fair_key)
            print(f"{price_key:>6} {thr:>5.2f} {r['bets']:>5} {r['roi']*100:>7.2f}% "
                  f"{r['pnl']:>8.2f} {r['hit']*100:>5.1f}% {r['clv']*100:>7.2f}%")

    # HINDSIGHT (look-ahead): fair = Pinnacle CLOSING. Not tradeable; reference only.
    run("HINDSIGHT  price=EARLY max   vs fair=Pinnacle CLOSING (look-ahead)", "MaxE", "PSC")

    # IMPLEMENTABLE: fair = Pinnacle EARLY — both sides known at bet time.
    run("LIVE  price=EARLY max   vs fair=Pinnacle EARLY (no look-ahead)", "MaxE", "PSE")
    run("LIVE  price=EARLY avg   vs fair=Pinnacle EARLY (no look-ahead)", "AvgE", "PSE")
    run("LIVE  price=Bet365 early vs fair=Pinnacle EARLY (one real soft book)", "B365E", "PSE")

if __name__ == "__main__":
    main()
