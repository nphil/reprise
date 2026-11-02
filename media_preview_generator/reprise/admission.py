"""Evidence admission from active, machine-scoped episode bindings."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from .identity import is_current
from .verification_profile import ADMISSION_REVISION as _ADMISSION_REVISION

MAX_REFERENCES = 6


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


def pair_provenance(target, target_bindings, reference, reference_bindings):
    """Hash all active provenance only when the two current files prove different episodes."""
    if (
        target.id == reference.id
        or not is_current(target)
        or not is_current(reference)
        or (target.device, target.inode) == (reference.device, reference.inode)
    ):
        return None
    left, right = _episode(target_bindings), _episode(reference_bindings)
    if not left or not right or left[:3] != right[:3] or left[3] == right[3]:
        return None
    left_aliases = {(i.plex_machine_identifier, i.rating_key) for i, _, _ in target_bindings}
    right_aliases = {(i.plex_machine_identifier, i.rating_key) for i, _, _ in reference_bindings}
    if left_aliases & right_aliases:
        return None

    def snapshot(bindings):
        return sorted(
            json.dumps([asdict(i), server, metadata], sort_keys=True, separators=(",", ":"))
            for i, server, metadata in bindings
        )

    payload = [_ADMISSION_REVISION, target.id, snapshot(target_bindings), reference.id, snapshot(reference_bindings)]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def admit_references(target, references, store):
    """Select at most one file per proven episode, in input order, before the cap."""
    target_bindings = store.get_bindings(target.id)
    admitted, episodes = [], set()
    for reference in references:
        bindings = store.get_bindings(reference.id)
        provenance = pair_provenance(target, target_bindings, reference, bindings)
        episode = _episode(bindings)
        if provenance and episode not in episodes:
            admitted.append((reference, provenance))
            episodes.add(episode)
            if len(admitted) == MAX_REFERENCES:
                break
    return admitted
