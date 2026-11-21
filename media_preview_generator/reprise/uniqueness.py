"""When an intro or credits is one of a kind: compared with enough other episodes, and shared by none of them.

The analysis records what each comparison found for one kind (``COMPARISON_KEY`` in the evidence's metrics); this module
is the one place that says what those facts amount to, so the analysis's note and the report never disagree. It decides
nothing about what is skipped: a range's action, review and eligibility do not depend on it.
"""

from __future__ import annotations

# The evidence metrics key holding one kind's comparison facts:
# ``{kind, episodes_searched, sound_matched, picture_matched, confirmed_ms}`` (episodes counted once, however many
# windows of the kind searched them; ``confirmed_ms`` is how much of the kind's windows other episodes confirmed).
COMPARISON_KEY = "comparison"
# Fewer episodes than the nearest Reprise always searches (``admission.MAX_REFERENCES``) are too few to say "none shares
# it"; a suggested window is searched against up to nine.
MIN_EPISODES = 3
# A few frames that happen to match (a shared logo, a coincidence) do not make an intro shared: up to this much, and
# at most a tenth of the suggested window, still counts as none.
SLIVER_MS = 2000
SLIVER_SHARE = 10


def is_one_of_a_kind(facts, suggested_ms=0):
    """Whether ``facts`` (one kind's comparison facts) show it is shared by none of the episodes searched. The caller
    adds that nothing of the kind was confirmed as a skip. ``suggested_ms`` is the length of the kind's suggested
    windows (Plex's marker, online times), the yardstick for a sliver."""
    if not isinstance(facts, dict) or facts.get("episodes_searched", 0) < MIN_EPISODES:
        return False
    confirmed = facts.get("confirmed_ms", 0)
    if confirmed <= 0:
        return True
    return confirmed <= SLIVER_MS and confirmed * SLIVER_SHARE <= suggested_ms


def _seconds(ms):
    value = ms / 1000
    return f"{value:.0f} s" if value >= 9.95 else f"{value:.1f} s".replace(".0 s", " s")


def explain(facts):
    """What the comparison found, in plain words, to follow "one of a kind": ``it matched none of 9 other episodes``."""
    others = f"{facts['episodes_searched']} other episodes"
    confirmed = facts.get("confirmed_ms", 0)
    if confirmed > 0:
        return f"only {_seconds(confirmed)} of it matched any of {others}"
    if facts.get("sound_matched", 0):
        return (
            f"its sound matches {facts['sound_matched']} of {others} but its picture does not, so it is its own version"
        )
    return f"it matched none of {others}"


def sentence(kind, facts):
    """The whole finding, no full stop: ``The intro is one of a kind: it matched none of 9 other episodes, so nothing
    is skipped``."""
    return f"The {kind} is one of a kind: {explain(facts)}, so nothing is skipped"
