"""Fetch a small poster from an actively registered Plex episode, never from its video."""

from __future__ import annotations

import json
import re
import threading
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import HTTPError

from ..nodes.client import answer_pieces, new_session
from .models import PlaybackIdentity

if TYPE_CHECKING:
    from ..config import Config

_RATING_KEY = re.compile(r"[0-9]{1,19}")
_SLOTS = threading.BoundedSemaphore(4)
_MAX_BYTES = 256 * 1024
_BUDGET_S = 5
_IO_TIMEOUT_S = 1.25
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_Binding = tuple[PlaybackIdentity, str, dict]


def episode_binding(bindings: list[_Binding]) -> _Binding | None:
    """Collapse audio aliases but do not guess which server or show a multiply registered file belongs to."""
    if not bindings:
        return None
    identity, server_id, metadata = bindings[0]
    if (
        metadata.get("kind") == "movie"
        or not metadata.get("show_title")
        or metadata.get("season") is None
        or not isinstance(identity.rating_key, str)
        or not _RATING_KEY.fullmatch(identity.rating_key)
    ):
        return None
    episode = (server_id, identity.plex_machine_identifier, identity.rating_key, metadata.get("library_id"))
    if all(
        (server, alias.plex_machine_identifier, alias.rating_key, saved.get("library_id")) == episode
        for alias, server, saved in bindings
    ):
        return bindings[0]
    return None


def _read(
    session: requests.Session,
    base: str,
    path: str,
    *,
    verify: bool,
    deadline: float,
    accept: str,
    params: dict | None = None,
) -> tuple[bytearray, str] | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    prepared = session.prepare_request(requests.Request("GET", base + path, params=params, headers={"Accept": accept}))
    timeout = min(remaining, _IO_TIMEOUT_S)
    # Bypass Session.send: even with redirects disabled it can buffer a redirect's unbounded body.
    with session.get_adapter(prepared.url).send(
        prepared, stream=True, timeout=(timeout, timeout), verify=verify
    ) as response:
        if response.status_code != 200 or response.headers.get("Content-Encoding", "identity").lower() != "identity":
            return None
        content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if content_type not in (_IMAGE_TYPES if params else {"application/json"}):
            return None
        length = response.headers.get("Content-Length")
        if length is not None and not 0 < int(length) <= _MAX_BYTES:
            return None
        body = bytearray()
        for chunk in answer_pieces(response, 8192, deadline):
            if len(body) + len(chunk) > _MAX_BYTES:
                return None
            body.extend(chunk)
        if not body or time.monotonic() >= deadline:
            return None
        return body, content_type


def _poster_path(item: dict, parent: str) -> str | None:
    key, thumb = str(item.get(parent + "RatingKey", "")), item.get(parent + "Thumb")
    if not _RATING_KEY.fullmatch(key) or not isinstance(thumb, str):
        return None
    if re.fullmatch(rf"/library/metadata/{key}/thumb(?:/[0-9]{{1,19}})?", thumb):
        return thumb
    return None


def _episode(container: dict, identity: PlaybackIdentity, metadata: dict) -> dict | None:
    (item,) = container.get("Metadata", [])
    library_id = item.get("librarySectionID", container.get("librarySectionID"))
    show_key = str(metadata.get("show_key") or "")
    if (
        item.get("type") != "episode"
        or str(item.get("ratingKey")) != identity.rating_key
        or str(item.get("parentIndex")) != str(metadata["season"])
        or (library_id is not None and str(library_id) != str(metadata["library_id"]))
        or (_RATING_KEY.fullmatch(show_key) and str(item.get("grandparentRatingKey")) != show_key)
    ):
        return None
    # Rating keys are local to each server. Confirm that this is the episode and file already bound to the version.
    if any(
        str(media.get("id")) == identity.media_id
        and str(part.get("id")) == identity.part_id
        and str(part.get("key", "")).partition("?")[0] == identity.part_key
        and str(part.get("size")) == str(identity.part_size_bytes)
        and part.get("file") == metadata["remote_part_path"]
        for media in item.get("Media", [])
        for part in media.get("Part", [])
    ):
        return item
    return None


def _is_image(body: bytearray, content_type: str) -> bool:
    if content_type == "image/jpeg":
        return body.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return body.startswith(b"\x89PNG\r\n\x1a\n")
    return body.startswith(b"RIFF") and body[8:12] == b"WEBP"


def season_poster(config: Config, identity: PlaybackIdentity, metadata: dict) -> tuple[bytes, str] | None:
    """Return (image bytes, MIME type), or None if Plex has no usable poster or cannot answer promptly."""
    if not config.plex_token or not _RATING_KEY.fullmatch(identity.rating_key) or not _SLOTS.acquire(blocking=False):
        return None
    deadline = time.monotonic() + _BUDGET_S
    try:
        base = (config.plex_url or "").rstrip("/")
        address = urlsplit(base)
        if (
            address.scheme not in {"http", "https"}
            or not address.hostname
            or address.username is not None
            or "?" in base
            or "#" in base
        ):
            return None
        verify = bool(getattr(config, "plex_verify_ssl", True))
        with new_session() as session:
            session.trust_env = False
            session.headers.update({"X-Plex-Token": config.plex_token, "Accept-Encoding": "identity"})
            result = _read(
                session,
                base,
                f"/library/metadata/{identity.rating_key}",
                verify=verify,
                deadline=deadline,
                accept="application/json",
            )
            if not result:
                return None
            item = _episode(json.loads(result[0])["MediaContainer"], identity, metadata)
            if not item:
                return None
            for parent in ("parent", "grandparent"):
                path = _poster_path(item, parent)
                if not path:
                    continue
                image = _read(
                    session,
                    base,
                    "/photo/:/transcode",
                    verify=verify,
                    deadline=deadline,
                    accept="image/jpeg, image/png, image/webp",
                    params={"url": path, "width": 96, "height": 144, "minSize": 1},
                )
                if image and _is_image(*image):
                    return bytes(image[0]), image[1]
    except (HTTPError, OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        # Artwork is optional. Never expose upstream details, URLs or credentials to the browser.
        return None
    finally:
        _SLOTS.release()
    return None
