"""Evidence admission from active, machine-scoped episode bindings."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from .identity import is_current, same_file
from .verification_profile import ADMISSION_REVISION as _ADMISSION_REVISION
from .verification_profile import PROFILE_SPEC

MAX_REFERENCES = PROFILE_SPEC["candidate"]["max_references"]
EXTRA_REFERENCES = PROFILE_SPEC["candidate"]["extra_references"]

# What Plex says about an episode that changes with no change to the file or its place in the library: the intro and
# credits markers it holds (Reprise's own publication rewrites them), the titles it shows and the address it was read
# at. A binding is the rest. A change in these is no reason to doubt an analysis or a decision, so a binding's change
# and a comparison's provenance both leave them out.
VOLATILE_METADATA = frozenset(("candidates", "title", "show_title", "server_url"))


def stable_metadata(metadata):
    """A binding's metadata without ``VOLATILE_METADATA``: what says which file this is and where it sits."""
    return {key: value for key, value in (metadata or {}).items() if key not in VOLATILE_METADATA}


def _episode(bindings):
    """Return one unambiguous episode, or None when any active alias is unknown."""
    episodes = set()
    if not bindings:
        return None
    for identity, _server_id, metadata in bindings:
        machine, show = identity.plex_machine_identifier, metadata.get("show_key")
        season, episode = metadata.get("season"), metadata.get("episode")
        if (
            not isinstance(machine, str)
            or not machine
            or not isinstance(show, str)
            or not show
            or type(season) is not int
            or season < 0
            or type(episode) is not int
            or episode < 0
        ):
            return None
        episodes.add((machine, show, season, episode))
    return next(iter(episodes)) if len(episodes) == 1 else None


def _provenance(target_id, target_bindings, reference_id, reference_bindings, metadata_of):
    def snapshot(bindings):
        return sorted(
            json.dumps([asdict(i), server, metadata_of(metadata)], sort_keys=True, separators=(",", ":"))
            for i, server, metadata in bindings
        )

    payload = [_ADMISSION_REVISION, target_id, snapshot(target_bindings), reference_id, snapshot(reference_bindings)]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def pair_provenance(target, target_bindings, reference, reference_bindings):
    """Hash all active provenance only when the two current files prove different episodes."""
    if target.id == reference.id or not is_current(target) or not is_current(reference) or same_file(target, reference):
        return None
    left, right = _episode(target_bindings), _episode(reference_bindings)
    if not left or not right or left[:3] != right[:3] or left[3] == right[3]:
        return None
    left_aliases = {(i.plex_machine_identifier, i.rating_key) for i, _, _ in target_bindings}
    right_aliases = {(i.plex_machine_identifier, i.rating_key) for i, _, _ in reference_bindings}
    if left_aliases & right_aliases:
        return None
    return _provenance(target.id, target_bindings, reference.id, reference_bindings, stable_metadata)


def restamp_provenance(stamp, target_id, target_bindings, reference_id, reference_bindings):
    """What a comparison made before ``VOLATILE_METADATA`` left the provenance should be stamped with now: the stable
    provenance of these bindings when ``stamp`` is the whole-metadata one of them, so nothing about either episode has
    changed since, else None. Only the store's one-time repair uses it."""
    if stamp != _provenance(target_id, target_bindings, reference_id, reference_bindings, dict):
        return None
    return _provenance(target_id, target_bindings, reference_id, reference_bindings, stable_metadata)


def admit_references(target, references, store):
    """Every other episode that may be a reference, one file per proven episode (the first given), nearest to the
    target first. The comparison uses the first MAX_REFERENCES of them that it can compare exactly with the target, so
    an episode in another picture format, which can't be, is passed over for the next nearest. A window the first ones
    leave less than half confirmed goes on to the next ones, MAX_REFERENCES at a time, up to EXTRA_REFERENCES more.

    Nearest is by episode number, the lower number first on a tie, so the same library always yields the same
    references in the same order. Admission needs the same show and season, so every candidate is of the target's season.
    """
    target_bindings = store.get_bindings(target.id)
    admitted, episodes = [], set()
    for reference in references:
        bindings = store.get_bindings(reference.id)
        provenance = pair_provenance(target, target_bindings, reference, bindings)
        episode = _episode(bindings)
        if provenance and episode not in episodes:
            admitted.append((reference, provenance, episode[3]))
            episodes.add(episode)
    if admitted:
        number = _episode(target_bindings)[3]
        admitted.sort(key=lambda entry: (abs(entry[2] - number), entry[2]))
    return [(reference, provenance) for reference, provenance, _ in admitted]
