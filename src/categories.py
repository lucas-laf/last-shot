"""Normalize platform-specific category labels into shared buckets.

Matching candidates are only generated within the same bucket, so it's better
to lump than to split: an over-specific bucket that differs across platforms
hides real matches.
"""
from __future__ import annotations

_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("soccer", ("soccer", "epl", "premier league", "la liga", "serie a",
                "bundesliga", "ligue 1", "champions league", "uefa", "fifa",
                "world cup", "mls", "football club")),
    ("basketball", ("basketball", "nba", "wnba", "ncaab", "euroleague")),
    ("american_football", ("nfl", "american football", "ncaaf", "super bowl")),
    ("baseball", ("baseball", "mlb", "world series")),
    ("tennis", ("tennis", "wimbledon", "us open tennis", "roland garros",
                "australian open")),
    ("politics", ("politics", "election", "president", "senate", "congress",
                  "parliament", "prime minister", "geopolitics", "white house",
                  "supreme court", "governor", "mayor")),
    ("crypto", ("crypto", "bitcoin", "ethereum", "btc", "eth")),
    ("entertainment", ("entertainment", "oscars", "grammys", "movies", "music",
                       "pop culture", "celebrity")),
]


def normalize_category(*labels: str) -> str:
    text = " ".join(l.lower() for l in labels if l)
    for bucket, keys in _KEYWORDS:
        if any(k in text for k in keys):
            return bucket
    return "other"
