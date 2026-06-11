"""Stage B: LLM verdict on candidate pairs.

One call per pair, forced tool-use for structured output. Bulk model first;
verdicts in the uncertain confidence band are re-checked by a stronger model.
Resolution rules are compared explicitly — identical-looking markets that
settle differently are the main hazard.
"""
from __future__ import annotations

import logging

import anthropic

from ..models import MatchVerdict, OutcomeLink
from .candidates import Candidate

logger = logging.getLogger(__name__)

VERDICT_TOOL = {
    "name": "record_verdict",
    "description": "Record whether these two markets are equivalent bets.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_match": {
                "type": "boolean",
                "description": "True only if a bet on the mapped Betfair runner and a bet on the Polymarket YES outcome are claims on the same real-world result.",
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "betfair_selection_id": {
                "type": "string",
                "description": "selection_id of the Betfair runner equivalent to the Polymarket YES outcome; empty string if no match.",
            },
            "resolution_risk": {
                "type": "boolean",
                "description": "True if the two markets could plausibly settle differently for the same real-world events (different deadlines, data sources, dead-heat/void rules, or settlement criteria).",
            },
            "reason": {"type": "string", "description": "One or two sentences."},
        },
        "required": ["is_match", "confidence", "betfair_selection_id", "resolution_risk", "reason"],
    },
}

SYSTEM = """You compare a Betfair Exchange market with a Polymarket market and decide \
whether they are bets on the same outcome of the same real-world event.

Be strict about:
- The event itself (same teams/people, same competition, same year/season).
- What exactly settles the bet on each side. Read the rules text. Flag \
resolution_risk=true when settlement criteria, deadlines, or void/dead-heat \
handling could diverge (e.g. 90-minute result vs to-qualify, official certification \
vs media calls, different cutoff dates).
- Betfair markets list several runners; pick the single runner that corresponds to \
the Polymarket YES outcome. If none does, it is not a match."""

PAIR_TEMPLATE = """## Betfair market
Event: {bf_event}
Market: {bf_market}
Start time: {bf_start}
Rules: {bf_rules}
Runners (selection_id: name):
{bf_runners}

## Polymarket market
Event: {pm_event}
Question: {pm_question}
YES outcome label: {pm_outcome}
Start/game time: {pm_start}
Resolution rules: {pm_rules}

Call record_verdict."""


def _render(c: Candidate) -> str:
    bf, pm = c.betfair, c.polymarket
    return PAIR_TEMPLATE.format(
        bf_event=bf.event_name,
        bf_market=bf.market_name,
        bf_start=bf.start_time or "unknown",
        bf_rules=bf.resolution_text[:1500] or "(none)",
        bf_runners="\n".join(f"  {o.outcome_id}: {o.name}" for o in bf.outcomes),
        pm_event=pm.event_name,
        pm_question=pm.market_name,
        pm_outcome=pm.outcomes[0].name if pm.outcomes else "?",
        pm_start=pm.start_time or "unknown",
        pm_rules=pm.resolution_text[:1500] or "(none)",
    )


class LLMMatcher:
    def __init__(
        self,
        api_key: str,
        bulk_model: str,
        escalation_model: str,
        escalation_band: tuple[float, float] = (0.4, 0.9),
    ):
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set — Stage B matching needs it")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.bulk_model = bulk_model
        self.escalation_model = escalation_model
        self.band = escalation_band

    def _ask(self, model: str, prompt: str) -> tuple[MatchVerdict, str] | None:
        resp = self.client.messages.create(
            model=model,
            max_tokens=1000,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[VERDICT_TOOL],
            tool_choice={"type": "tool", "name": "record_verdict"},
        )
        for block in resp.content:
            if block.type == "tool_use":
                d = block.input
                return MatchVerdict(
                    is_match=bool(d.get("is_match")),
                    confidence=float(d.get("confidence") or 0),
                    resolution_risk=bool(d.get("resolution_risk", True)),
                    reason=str(d.get("reason") or ""),
                    outcome_mapping=[],  # filled in judge()
                ), str(d.get("betfair_selection_id") or "")
        return None

    def judge(self, c: Candidate) -> MatchVerdict:
        prompt = _render(c)
        result = self._ask(self.bulk_model, prompt)
        if result is None:
            return MatchVerdict(is_match=False, reason="no tool output")
        verdict, selection_id = result

        lo, hi = self.band
        if lo <= verdict.confidence < hi:
            escalated = self._ask(self.escalation_model, prompt)
            if escalated is not None:
                verdict, selection_id = escalated
                verdict.reason = f"[escalated] {verdict.reason}"

        if verdict.is_match and selection_id and c.polymarket.outcomes:
            valid_ids = {o.outcome_id for o in c.betfair.outcomes}
            if selection_id in valid_ids:
                name = next(
                    o.name for o in c.betfair.outcomes if o.outcome_id == selection_id
                )
                verdict.outcome_mapping = [
                    OutcomeLink(
                        betfair_selection_id=selection_id,
                        polymarket_token_id=c.polymarket.outcomes[0].outcome_id,
                        name=name,
                    )
                ]
            else:
                verdict.is_match = False
                verdict.reason += " (hallucinated selection_id rejected)"
        if verdict.is_match and not verdict.outcome_mapping:
            verdict.is_match = False
            verdict.reason += " (no usable outcome mapping)"
        return verdict
