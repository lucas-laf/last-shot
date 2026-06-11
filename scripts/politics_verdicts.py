"""One-off: encode Claude's race-level review decisions (2026-06-11) for the
politics candidate export into verdicts JSON. Every exported pair gets a
verdict (rejects included) so nothing is re-reviewed next run.

Accept rules: (betfair market predicate, polymarket question predicate,
mapping mode). Anything not matched by a rule is rejected as a race mismatch.
"""
from __future__ import annotations

import json
import re
import sys

from rapidfuzz import fuzz

US_STATES = (
    "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|"
    "Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|"
    "Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|"
    "Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|"
    "New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|"
    "Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|"
    "Virginia|West Virginia|Wisconsin|Wyoming"
)


def state_of(text: str) -> str | None:
    m = re.search(US_STATES, text)
    return m.group(0) if m else None


# (rule_name, bf_predicate, pm_predicate, risk_note)
def bf_is(event_sub: str, market_sub: str):
    return lambda r: event_sub in r["bf_event"] and market_sub in r["bf_market"]


RULES = [
    ("pres2028_winner",
     bf_is("USA - Presidential Election 2028", "Election Winner"),
     lambda r: re.fullmatch(r"Will .+ win the 2028 US Presidential Election\?", r["pm_question"])
               and "Democrats" not in r["pm_question"] and "Republicans" not in r["pm_question"],
     "Verify certification-vs-projection settlement timing before live trading"),
    ("pres2028_party",
     bf_is("USA - Presidential Election 2028", "Winning Party"),
     lambda r: re.fullmatch(r"Will the (Democrats|Republicans) win the 2028 US Presidential Election\?", r["pm_question"]),
     "Party-level; verify third-party/independent handling"),
    ("rep_nominee",
     bf_is("USA - Presidential Election 2028", "Republican Presidential Nominee"),
     lambda r: re.fullmatch(r"Will .+ win the 2028 Republican presidential nomination\?", r["pm_question"]),
     "Nominee definition (convention vs primary outcome) may differ"),
    ("dem_nominee",
     bf_is("USA - Presidential Election 2028", "Democratic Presidential Nominee"),
     lambda r: re.fullmatch(r"Will .+ win the 2028 Democratic presidential nomination\?", r["pm_question"]),
     "Nominee definition (convention vs primary outcome) may differ"),
    # NOTE: Alaska gov/senate rules removed — Betfair's Alaska markets are
    # party-level (Democrat/Republican runners) while Polymarket's are
    # candidate-level; structurally incompatible, so they reject by default.
    ("california_gov_candidates",
     bf_is("USA 2026 Midterm Elections", "California Gubernatorial Election Winner"),
     lambda r: re.fullmatch(r"Will .+ win the California Governor Election in 2026\?", r["pm_question"]),
     "Candidate-level state race"),
    ("state_gov_party",
     lambda r: "USA 2026 Midterm Elections" in r["bf_event"]
               and ("Gubernatorial Election Winner" in r["bf_market"] or "Governor Election Winner" in r["bf_market"]),
     lambda r: (m := re.fullmatch(r"Will the (Democrats|Republicans) win the (.+) governor race in 2026\?", r["pm_question"]))
               and state_of(r["bf_market"]) == m.group(2),
     "Party-level; verify independent-candidate handling"),
    ("state_senate_party",
     lambda r: "USA 2026 Midterm Elections" in r["bf_event"] and "Senate Election Winner" in r["bf_market"],
     lambda r: (m := re.fullmatch(r"Will the (Democrats|Republicans) win the (.+) Senate race in 2026\?", r["pm_question"]))
               and state_of(r["bf_market"]) == m.group(2),
     "Party-level; verify independent-candidate handling"),
    ("house_control",
     bf_is("USA 2026 Midterm Elections", "Which party will control the House"),
     lambda r: re.fullmatch(r"Will the (Democrats|Republicans|Democratic Party|Republican Party) control the House after the 2026 Midterm elections\?", r["pm_question"]),
     "Control definition at organization vote vs election result"),
    ("senate_control",
     bf_is("USA 2026 Midterm Elections", "Which party will control the Senate"),
     lambda r: re.fullmatch(r"Will the (Democrats|Republicans|Democratic Party|Republican Party) control the Senate after the 2026 Midterm elections\?", r["pm_question"]),
     "Control definition (VP tiebreak) — verify both sides"),
    ("fl_rep_primary",
     bf_is("2026 Primaries", "Florida Republican Governor Primary Winner"),
     lambda r: re.fullmatch(r"Will .+ be the Republican nominee for Florida Governor\?", r["pm_question"]),
     "Primary winner vs nominee: can diverge on withdrawal/replacement"),
    ("nz_most_seats",
     bf_is("New Zealand", "2026 General Election"),
     lambda r: re.fullmatch(r"Will .+ win the most seats in the New Zealand House of Representatives in the 2026 New Zealand.*", r["pm_question"]),
     "Betfair market basis (most seats vs forms govt) — verify rules"),
    ("sweden_pm",
     bf_is("European Politics", "Next Prime Minister of Sweden"),
     lambda r: re.fullmatch(r"Will .+ be the next Prime Minister of Sweden\?", r["pm_question"]),
     "'Next PM' caretaker/timing definitions may differ"),
    ("berlin_most_seats",
     bf_is("German State Elections", "Berlin State Election Winner"),
     lambda r: re.fullmatch(r"Will .+ win the most seats in the 2026 Berlin state elections\?", r["pm_question"]),
     "Winner = most seats assumption — verify Betfair rules"),
    ("quebec_winner",
     bf_is("Canadian Politics", "Quebec General Election Winner"),
     lambda r: re.fullmatch(r"Will .+ win the most seats in the 2026 Quebec general election\?", r["pm_question"]),
     "Winner = most seats assumption — verify Betfair rules"),
    ("makerfield",
     lambda r: "UK - By-Elections" in r["bf_event"] and r["bf_market"] == "Makerfield by-election",
     lambda r: re.fullmatch(r"Will .+ win the 2026 Makerfield by-election\?", r["pm_question"])
               and "another outcome" not in r["pm_question"],
     "By-election; void/withdrawal rules may differ"),
    ("aberdeen",
     lambda r: "UK - By-Elections" in r["bf_event"] and r["bf_market"] == "Aberdeen South by-election",
     lambda r: re.fullmatch(r"Will .+ win the 2026 Aberdeen South by-election\?", r["pm_question"]),
     "By-election; void/withdrawal rules may differ"),
    ("arbroath",
     lambda r: "UK - By-Elections" in r["bf_event"] and "Arbroath" in r["bf_market"],
     lambda r: re.fullmatch(r"Will .+ win the 2026 Arbroath and Broughty Ferry by-election\?", r["pm_question"]),
     "By-election; void/withdrawal rules may differ"),
    ("la_mayor",
     bf_is("USA - Politics Specials", "Los Angeles Mayoral Election"),
     lambda r: re.fullmatch(r"Will .+ win the 2026 Los Angeles mayoral election\?", r["pm_question"]),
     "Two-round race: verify whether both resolve on the final winner"),
]

PARTY_ALIASES = {
    "democrats": ["democrat", "democrats", "democratic party"],
    "republicans": ["republican", "republicans", "republican party", "gop"],
}


def map_runner(r: dict) -> tuple[str | None, str | None, float]:
    """Map the PM market's subject to a BF runner."""
    subject = r["pm_outcome"]
    # party questions: subject comes from the question, not the outcome label
    m = re.search(r"Will the (\w[\w ]*?)s? (?:win|control)", r["pm_question"])
    if m and m.group(1).lower().rstrip("s") in ("democrat", "republican"):
        subject = m.group(1)
    # acronym labels (CAQ, PQ...): the question carries the full name
    elif len(subject) <= 4 and subject.isupper():
        m = re.match(r"Will (?:the )?(.+?) (?:win|be|control|gain)", r["pm_question"])
        if m:
            subject = m.group(1)
    subject_l = subject.lower()
    best = (None, None, 0.0, 0.0)
    for sid, name in r["bf_runners"].items():
        name_l = name.lower()
        # Generational suffixes distinguish people: "Donald Trump" is NOT
        # "Donald Trump Jr." even though token_set scores them 100.
        if _suffixes(subject_l) != _suffixes(name_l):
            continue
        sc = fuzz.token_set_ratio(subject_l, name_l)
        tie = fuzz.ratio(subject_l, name_l)
        for canon, aliases in PARTY_ALIASES.items():
            if subject_l.rstrip("s") in [a.rstrip("s") for a in aliases] and name_l in aliases:
                sc = tie = 100
        if (sc, tie) > (best[2], best[3]):
            best = (sid, name, sc, tie)
    return best[:3]


def _suffixes(name: str) -> set[str]:
    return {t.strip(".") for t in name.split()} & {"jr", "sr", "ii", "iii", "iv"}


# Candidate->party mappings I'm confident in for party-level Betfair
# by-election markets (Claude review 2026-06-11; verify if anything settles oddly).
KNOWN_CANDIDATE_PARTY = {
    ("Makerfield by-election", "Andy Burnham"): "Labour",
}


def main() -> None:
    rows = json.load(open("data/candidates_politics.json"))
    verdicts, accepts, weak = [], [], []
    for r in rows:
        if r["category"] != "politics":
            continue
        hit = next(
            ((name, note) for name, bf_p, pm_p, note in RULES if bf_p(r) and pm_p(r)),
            None,
        )
        if not hit:
            verdicts.append({
                "bf_market_id": r["bf_market_id"], "pm_market_id": r["pm_market_id"],
                "is_match": False, "confidence": 0.9, "resolution_risk": True,
                "reason": "Reviewed by Claude in-session: race/office/geography mismatch "
                          f"({r['bf_event']} / {r['bf_market']} vs {r['pm_question'][:80]!r})",
            })
            continue
        rule, note = hit
        sid, runner, sc = map_runner(r)
        known_party = KNOWN_CANDIDATE_PARTY.get((r["bf_market"], r["pm_outcome"]))
        if known_party and (not sid or sc < 80):
            for k, v in r["bf_runners"].items():
                if v.lower() == known_party.lower():
                    sid, runner, sc = k, v, 80.0
                    note = f"Candidate-to-party mapping ({r['pm_outcome']} = {known_party}); {note}"
                    break
        if not sid or sc < 80:
            # Right race, but the PM subject has no equivalent Betfair runner
            # (candidate not listed, or party-vs-candidate structure mismatch).
            weak.append((rule, r["idx"], r["pm_question"][:70], r["pm_outcome"][:30], runner, round(sc)))
            verdicts.append({
                "bf_market_id": r["bf_market_id"], "pm_market_id": r["pm_market_id"],
                "is_match": False, "confidence": 0.85, "resolution_risk": True,
                "reason": "Reviewed by Claude in-session: race matches but subject "
                          f"{r['pm_outcome']!r} has no equivalent Betfair runner "
                          f"(best: {runner!r} @{sc:.0f})",
            })
            continue
        accepts.append((rule, r["idx"], r["pm_question"][:64], runner, sc))
        verdicts.append({
            "bf_market_id": r["bf_market_id"], "pm_market_id": r["pm_market_id"],
            "is_match": True, "betfair_selection_id": sid,
            "confidence": 0.9 if sc >= 95 else 0.8, "resolution_risk": True,
            "reason": f"Reviewed by Claude in-session ({rule}): {r['pm_outcome']!r} -> runner "
                      f"{runner!r}. RISK: {note}.",
        })

    json.dump(verdicts, open("data/verdicts_politics.json", "w"), indent=1)
    print(f"{len(verdicts)} verdicts ({len(accepts)} accepts), {len(weak)} weak mappings\n")
    print("=== ACCEPTS ===")
    for rule, idx, q, runner, sc in sorted(accepts):
        print(f"  [{rule:24s}] {q:64s} -> {runner} ({sc:.0f})")
    print("\n=== REJECTED AS NO-RUNNER (eyeball these) ===")
    for w in weak:
        print(" ", w)


if __name__ == "__main__":
    main()
