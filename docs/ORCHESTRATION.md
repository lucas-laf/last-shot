# Hourly orchestration runbook

How the discovery → matching → tracking → settlement loop runs each hour. The
loop is driven by an external scheduler (a cron-style job that fires the prompt
hourly at :11); the agent then executes the steps below against the project on
the laptop. The EC2 box runs its own copy of the tracker independently via
systemd — the hourly loop here manages the **laptop** instance and the shared
pipeline state in `data/lastshot.db`.

## The cycle, step by step

### 1. Tracker health
```bash
pgrep -fl "src.tracking.run"          # is it alive?
tail logs/tracker.log                  # recent signals / errors / stale feeds
```
If dead, restart:
```bash
nohup .venv/bin/python -m src.tracking.run --status-every 300 >> logs/tracker.log 2>&1 &
```
The tracker holds both live feeds (Betfair Stream API + Polymarket WS), evaluates
the signal engine on every tick, writes ticks to `data/ticks/*.parquet`, and logs
paper trades + shadow orders to the DB. Routine `Polymarket WS error … reconnecting`
lines are normal (auto-recovers in 1-2s); a stale feed or a Python traceback is not.

### 2. Discovery
```bash
.venv/bin/python -m src.discovery.run
```
Scans both platforms and upserts markets into `data/lastshot.db`:
- **Betfair**: `listMarketCatalogue` over the configured event types
  (Soccer, Basketball, Tennis, Am. Football, Baseball, Politics), capped at 200
  markets/type by traded volume (catalogue weight limit). Live app key → real-time.
- **Polymarket**: paginated Gamma API pull of active markets.
Markets no longer seen are marked inactive.

### 3. Candidate matching (the review step — this is the judgment-heavy part)
```bash
.venv/bin/python -m src.matching.export      # -> data/candidates.json (unjudged pairs only)
```
`src.matching.candidates` prefilters Betfair×Polymarket pairs by rapidfuzz name
similarity (≥60) and event-time window, then `export` writes the pairs not yet
judged. **The agent reads each candidate and decides `is_match`**, judging
*market equivalence*, not just name overlap:

- **Same event AND same question.** Reject if the PM question differs from the
  Betfair market type even when names match — e.g. "win the group" ≠ "win the
  tournament", "PM in 2026" ≠ "next PM (by 2029)", "second-most seats" ≠ "win".
- **Runner must map to a real Betfair selection.** If the PM candidate has no
  Betfair runner (common in candidate-level vs party-level political markets),
  reject — no outcome mapping is possible.
- **Different jurisdiction/date/opponent = reject.** The prefilter is deliberately
  loose and surfaces a lot of cross-race political noise and same-player/wrong-match
  tennis pairs; these are the bulk of rejections.
- **`resolution_risk` flag**: set true for tennis (Betfair voids on retirement,
  Polymarket pays the advancer) and KBO/tie-prone formats. Risk-flagged sports
  pairs are still tracked (paper trading makes mismatches measurable, not costly),
  but land in `needs_review` and are promoted to `approved` per standing policy.

Verdicts are written to `data/verdicts_cycle.json` in this shape:
```json
{"bf_market_id": "1.234", "pm_market_id": "0xabc", "is_match": true,
 "betfair_selection_id": "12345", "confidence": 0.95,
 "resolution_risk": false, "reason": "…"}
```
then ingested (selection ids are re-validated mechanically; bad ids auto-reject):
```bash
.venv/bin/python -m src.matching.ingest data/verdicts_cycle.json
```
Matches with no resolution risk land `approved`; risk-flagged ones land
`needs_review` and are promoted with a one-shot SQL update per standing policy.

### 4. Restart tracker if new pairs approved
New approved pairs aren't tracked until the tracker reloads its pair set, so kill
and relaunch it (step 1) whenever the cycle approved anything.

### 5. Settle
```bash
.venv/bin/python -m src.signals.settle
```
Resolves paper trades against real results — **Betfair runner status for Betfair
legs, Polymarket outcome prices for Polymarket legs** — so resolution-rule
mismatches show up as real PnL. (Gotcha baked in: Gamma omits closed markets unless
queried with `closed=true`.) Betfair legs settle within minutes of an event ending;
Polymarket legs lag hours until UMA resolves, which is why lock_arb realized PnL
dips one-sided after each event and recovers when the PM halves land.

### 6. Git
If `git status --porcelain` shows **code** changes (`.env`, `data/`, `logs/` are
gitignored), commit with a descriptive message and push to `origin main`. Most
cycles change no code → nothing to commit. Data/DB is intentionally not versioned.

### 7. Status report
One paragraph: tracker health, new pairs approved/rejected (with notable
diagnoses), trades settled this cycle, realized PnL movement (lock_arb and
retired-convergence tracked separately), and whether anything was committed.
**Discipline: any absurd edge (>15%) means a bad match — diagnose the pair, never
celebrate it.** That rule caught a fake 61% "lock arb" on day one.

## What the loop is NOT
- It does **not** place real orders. Execution is a separate, disarmed module
  (`src/execution/`); see `STATUS.md` for the live-trading gate.
- It does **not** retrain or change strategy parameters. Strategy decisions
  (e.g. retiring convergence, the `min_edge` floor) are made deliberately, not
  inside the cycle.

## Quick reference
```bash
.venv/bin/python -m analysis.edge_report          # realized PnL by signal/category
.venv/bin/python -m analysis.replay               # convergence counterfactuals from ticks
.venv/bin/python -m analysis.capture_report        # shadow capture rates
.venv/bin/python -m analysis.maker_sniper_backtest # maker / sniper backtests
```
