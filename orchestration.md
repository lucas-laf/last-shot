# Hourly orchestration runbook

How the discovery → matching → tracking → settlement loop runs each hour. The
loop is driven by an external scheduler (a cron-style job that fires the prompt
hourly at :11); the agent then executes the steps below against the project on
the laptop. The EC2 box runs its own copy of the tracker independently via
systemd — the hourly loop here manages the **laptop** instance and the shared
pipeline state in `data/lastshot.db`.

## How the agent is run and managed

The "agent" is **Claude Code running interactively in a terminal session** — not
a script the project calls, and not a background daemon. The mechanics:

- **Scheduler → prompt.** A cron-style job (created with the harness's
  `CronCreate`, firing hourly at :11) enqueues the *same orchestration prompt*
  into the already-running Claude Code session each hour. The job is
  **session-only / in-memory**: it lives as long as that Claude Code session is
  open, is not written to disk, and **auto-expires after 7 days**. If the session
  is closed, the loop simply stops — there is no separate service to kill. (The
  job only fires while the session is idle; it won't interrupt work in progress.)
- **Agent → tools.** On each firing, Claude Code reads the prompt and executes the
  steps below using its own tools (shell, file edits) against the local repo. The
  judgment-heavy step — deciding which market pairs are genuinely equivalent — is
  performed by **the agent itself, reasoning in-session**, then written to
  `data/verdicts_cycle.json` and fed back into the pipeline. There is no
  autonomous Python process making these calls; the model is in the loop directly.
- **State is on disk, not in the agent.** Everything durable (markets, verdicts,
  paper trades, ticks) lives in `data/lastshot.db` + parquet. The agent is
  stateless between firings — it re-reads state each cycle — so a dropped or
  restarted session loses no data, only the schedule.
- **The tracker is the only true daemon.** `src.tracking.run` runs continuously
  (laptop: `nohup`; EC2: systemd `lastshot-tracker`) independent of the agent.
  The agent supervises it (restarts if dead) but does not *be* it.

### Does this need an Anthropic API key? **No — and that's by design.**

The matching pipeline has **two** possible reviewers, and the loop uses the one
that needs no key:

| Path | Reviewer | API key? | Used? |
|---|---|---|---|
| `export` → *agent reasons in-session* → `ingest` | Claude Code (this agent) | **No** | **Yes — this is the loop** |
| `python -m src.matching.run` (`llm_matcher.py`) | Anthropic API (`claude-haiku`/`claude-sonnet`) | **Yes** (`ANTHROPIC_API_KEY`) | No — unused fallback |

Because the agent *is* the Claude session, its reasoning is already "inside" Claude
Code — calling the Anthropic API from the Python code would just be paying to ask
Claude something the agent can answer directly. So `src.matching.export` writes the
unjudged pairs to JSON, the agent reads and judges them in the same turn, and
`src.matching.ingest` records the verdicts. `ANTHROPIC_API_KEY` is referenced only
by the unused `src.matching.run` / `LLMMatcher` path (it raises "ANTHROPIC_API_KEY
not set — Stage B matching needs it" if you ever invoke that path without one).
Nothing in the live loop touches it. The credentials the loop *does* require are
the **Betfair** login (in `.env`) for the live feed and settlement.

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

## Recreating this process from scratch

What an agent needs to do to stand up the identical loop on a fresh machine.

### 1. Prerequisites (one-time)
```bash
git clone git@github.com:lucas-laf/last-shot.git && cd last-shot
uv venv --python 3.12 && uv sync --extra dev      # add --extra exec for live orders
```
Create `.env` with the **Betfair** credentials (the only secret the loop needs):
```
BETFAIR_APP_KEY=...            # live key → real-time Stream API
BETFAIR_USERNAME=...           # interactive login (no cert files needed)
BETFAIR_PASSWORD=...
MIN_EDGE=0.0025                # data-collection threshold (see §min_edge)
```
`ANTHROPIC_API_KEY` is **not** required — the agent is the reviewer (see "How the
agent is run"). Confirm the venv works: `.venv/bin/python -m pytest -q`.

### 2. Bootstrap tracked pairs (one-time, before the loop can track anything)
```bash
.venv/bin/python -m src.discovery.run            # populate markets
.venv/bin/python -m src.matching.export          # -> data/candidates.json
#   agent reads candidates.json, judges each (rules in step 3 of the cycle),
#   writes data/verdicts_cycle.json
.venv/bin/python -m src.matching.ingest data/verdicts_cycle.json
#   promote risk-flagged sports pairs needs_review -> approved (one SQL update)
```

### 3. Launch the tracker daemon
- Laptop / quick: `nohup .venv/bin/python -m src.tracking.run --status-every 300 >> logs/tracker.log 2>&1 &`
- Server / durable: install `deploy/lastshot-tracker.service` as a systemd unit
  (`deploy/setup.sh` does the full server bootstrap incl. clone, uv sync, swap).

### 4. Schedule the hourly agent loop
Using the harness scheduler (`CronCreate`), create a **recurring hourly** job whose
prompt is the orchestration instruction. Pick an off-:00 minute (e.g. `11 * * * *`).
Key properties to set, matching the current job: recurring, fires only while the
session is idle, in-memory (dies with the session) unless you deliberately persist
it. The scheduled prompt is verbatim:

> Hourly last_shot orchestration cycle (project: <path>). Steps: (1) Check the
> tracker is alive (`ps aux | grep src.tracking`) and tail logs/tracker.log;
> restart with the nohup command if dead. (2) `python -m src.discovery.run`.
> (3) `python -m src.matching.export`, review each candidate yourself — judge
> market equivalence incl. question semantics, not just names; reject when the PM
> question doesn't match the Betfair market type; flag resolution_risk for
> tennis-retirement / advancement mismatches — write data/verdicts_cycle.json,
> then `python -m src.matching.ingest data/verdicts_cycle.json`; promote
> risk-flagged sports pairs needs_review→approved per standing policy. (4) If new
> pairs were approved, restart the tracker. (5) `python -m src.signals.settle`.
> (6) Git: if `git status --porcelain` shows code changes (.env/data/logs are
> gitignored), commit + push to origin main. (7) One-paragraph status report:
> tracker health, new pairs, trades settled, realized PnL movement, whether
> anything was committed. Any absurd edge (>15%) means a bad match — diagnose the
> pair, don't celebrate.

### 5. Standing policies the agent must carry across cycles
These are not in the prompt each time but are part of the role:
- **Review discipline** — the equivalence rules in cycle-step 3 (jurisdiction,
  date, opponent, question semantics, runner-mapping existence).
- **Promote risk-flagged sports pairs** (tennis/KBO) from `needs_review` to
  `approved` — paper trading makes the mismatch measurable, not costly.
- **Diagnose, don't celebrate** absurd edges (>15% ⇒ bad match).
- **Strategy decisions are made deliberately, never inside a cycle** (e.g. the
  convergence retirement, the `min_edge` floor). See `STATUS.md`.
- **Never arm the executor** without the explicit live-trading gate in `STATUS.md`
  being resolved.

### 6. Persistence note
The cron job is session-bound: close the Claude Code session and the schedule
stops (tracker + data are unaffected). To make the loop survive restarts, drive it
from a persistent trigger (systemd timer invoking a headless agent run) instead of
an in-memory cron job — a different setup than the interactive session above.

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
