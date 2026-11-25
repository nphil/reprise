"""An optional, advisory look at an episode's frames by a local vision model (llama-swap, OpenAI chat format).

After an analysis, for each intro or credits stretch Reprise could not confirm by comparing episodes, a few small
frames are shown to the model, which says per frame whether it is titles/credits, an ordinary story scene, a
next-episode preview or black/logo. Runs of "credits" frames give a span; it is offered as a suggestion the owner
approves (source "Local model"), never applied, never sent, and never changes what the comparison found.

The module uses only the standard library (and ffmpeg) so the evaluation tool can run it anywhere. Every failure of the
endpoint (down, busy, another model loaded, an odd answer) ends the check quietly: no advice, nothing else changes.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import subprocess
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SOURCE_NAME = "Local model"
SOURCE_REASON = "a local vision model that looked at frames of the episode"
SETTING = "reprise_local_vision"  # the owner's switch, address and model; off unless turned on
DEFAULT_URL = "http://192.168.1.69:9292/v1"
DEFAULT_MODEL = "qwen3-vl-4b"
SCENES = ("credits", "story", "preview", "black_logo")
FRAME_WIDTH = 320
STEP_MS = 3000  # a frame this often through a stretch, more sparsely when that would pass MAX_FRAMES
MAX_FRAMES = 40
REFINE_MS = 1000  # a boundary between two sampled frames is narrowed to about this
MAX_REFINES = 4  # frames spent narrowing one boundary
PAD_MS = 90000  # how far before a suggested start or after a suggested end a stretch reaches
BRIDGE = 2  # a frame or two of another kind inside a run of credits is noise
PREVIEW_MAX_MS = 40000  # credits that stop this close to the end of the file run on to it: what follows is a preview
MIN_SPAN_MS = {"intro": 10000, "credits": 20000}
TIMEOUT_S = 20
RETRIES = 1

PROMPT = (
    "This is one frame of a TV episode. Say what it shows. scene is exactly one of: "
    '"credits" (opening titles or end credits: staff or cast names, a title card or series logo, rolling or stacked '
    'text over artwork, animation or footage); "preview" (a next-episode preview or "next time" card, or story '
    'footage under a next-episode caption); "black_logo" (an almost black screen, or a studio or network logo); '
    '"story" (an ordinary scene of the story with no credit text). overlay_text is true when any text other than '
    "subtitles is laid over the picture. Answer with only the JSON object."
)
SCHEMA = {
    "type": "object",
    "properties": {"scene": {"type": "string", "enum": list(SCENES)}, "overlay_text": {"type": "boolean"}},
    "required": ["scene", "overlay_text"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Config:
    enabled: bool = False
    base_url: str = DEFAULT_URL
    model: str = DEFAULT_MODEL


@dataclass(frozen=True)
class Verdict:
    kind: str
    start_ms: int
    end_ms: int
    frames: tuple[tuple[int, str, bool], ...]  # (time ms, scene, overlay text), every frame asked about, in time order
    model: str


def config() -> Config:
    from ..web.settings_manager import peek_settings_manager

    manager = peek_settings_manager()
    saved = manager.get(SETTING, None) if manager is not None else None
    if not isinstance(saved, dict):
        return Config()
    return Config(
        saved.get("enabled") is True,
        str(saved.get("base_url") or DEFAULT_URL).rstrip("/"),
        str(saved.get("model") or DEFAULT_MODEL),
    )


def set_config(enabled: bool, base_url: str, model: str) -> Config:
    """Save the switch, address and model. The address must be an http(s) URL, the model a plain name."""
    from ..web.settings_manager import get_settings_manager

    url = base_url.strip().rstrip("/")
    if not (url.startswith(("http://", "https://")) and len(url) < 200 and url.count(" ") == 0):
        raise ValueError("Invalid setting")
    name = model.strip()
    if not name or len(name) > 100 or any(ch in name for ch in " \n\t\"'"):
        raise ValueError("Invalid setting")
    get_settings_manager().set(SETTING, {"enabled": bool(enabled), "base_url": url, "model": name})
    return config()


def _open(request, timeout):
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def model_ready(cfg: Config, timeout: float = 5) -> bool:
    """Whether llama-swap already has this model loaded and ready. Asking never loads anything."""
    root = cfg.base_url[:-3] if cfg.base_url.endswith("/v1") else cfg.base_url
    try:
        running = _open(urllib.request.Request(root + "/running"), timeout).get("running", [])
    except (OSError, ValueError, AttributeError):
        return False
    return any(isinstance(m, dict) and m.get("model") == cfg.model and m.get("state") == "ready" for m in running)


def judge(cfg: Config, jpeg: bytes) -> tuple[str, bool] | None:
    """The model's ``(scene, overlay_text)`` for one frame; None when it could not be asked or answered oddly."""
    body = json.dumps(
        {
            "model": cfg.model,
            "temperature": 0,
            "max_tokens": 40,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()},
                        },
                    ],
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "frame", "strict": True, "schema": SCHEMA},
            },
        }
    ).encode()
    for _ in range(1 + RETRIES):
        try:
            request = urllib.request.Request(
                cfg.base_url + "/chat/completions", body, {"Content-Type": "application/json"}
            )
            answer = json.loads(_open(request, TIMEOUT_S)["choices"][0]["message"]["content"])
            if answer["scene"] in SCENES and isinstance(answer["overlay_text"], bool):
                return answer["scene"], answer["overlay_text"]
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            continue
    return None


def grab(ffmpeg: str, path: str, at_ms: int) -> bytes:
    """One frame of the file at this time as a small JPEG."""
    out = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-ss",
            f"{max(0, at_ms) / 1000:.3f}",
            "-i",
            path,
            "-frames:v",
            "1",
            "-vf",
            f"scale={FRAME_WIDTH}:-2",
            "-f",
            "image2pipe",
            "-c:v",
            "mjpeg",
            "-",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=True,
    ).stdout
    if not out:
        raise OSError("no frame")
    return out


def region(kind: str, anchors: Sequence[tuple[int, int]], duration_ms: int) -> tuple[int, int]:
    """The stretch to look at for a kind: around the spans suggested or proposed for it, padded; for credits all the
    way to the end of the file (the tail after the credits decides where they end)."""
    lo = max(0, min(a for a, _ in anchors) - PAD_MS)
    hi = duration_ms if kind == "credits" else min(duration_ms, max(b for _, b in anchors) + PAD_MS)
    return lo, hi


def _runs(frames: Sequence[tuple[int, str]]) -> list[tuple[int, int]]:
    """Index ranges ``(first, last)`` of consecutive credits frames, a single other frame inside one bridged."""
    runs = []
    for i, (_, scene) in enumerate(frames):
        if scene != "credits":
            continue
        if runs and i - runs[-1][1] - 1 <= BRIDGE:
            runs[-1] = (runs[-1][0], i)
        else:
            runs.append((i, i))
    return runs


def _edge(at: Callable[[int], str], inside: int, outside: int) -> int:
    """The time between a credits frame (``inside``) and a frame that is not (``outside``), found by halving."""
    for _ in range(MAX_REFINES):
        if abs(inside - outside) <= REFINE_MS:
            break
        middle = (inside + outside) // 2
        if at(middle) == "credits":
            inside = middle
        else:
            outside = middle
    return (inside + outside) // 2


def survey(kind: str, lo: int, hi: int, duration_ms: int, scene_at: Callable[[int], str]) -> tuple[int, int] | None:
    """The span the frames of ``lo..hi`` show for this kind, or None. ``scene_at(ms)`` asks about one frame (the caller
    keeps every answer). The intro is the first run of title frames; the credits are the last run, and they run to the
    end of the file when everything after them is a preview, black or a logo (or is no more than ``PREVIEW_MAX_MS``),
    otherwise to the run's own end."""
    step = max(STEP_MS, math.ceil((hi - lo) / MAX_FRAMES))
    times = list(range(lo, max(lo + 1, hi - 500), step))
    if hi >= duration_ms and times[-1] < duration_ms - step // 2:
        times.append(max(0, duration_ms - 1000))
    frames = [(t, scene_at(t)) for t in times]
    runs = _runs(frames)
    if not runs:
        return None
    first, last = runs[0] if kind == "intro" else runs[-1]
    if last - first < 1:
        return None
    start = frames[first][0] if first == 0 else _edge(scene_at, frames[first][0], frames[first - 1][0])
    after = frames[last + 1 :]
    if (
        kind == "credits"
        and hi >= duration_ms
        and (all(s != "story" for _, s in after) or duration_ms - frames[last][0] <= PREVIEW_MAX_MS)
    ):
        end = duration_ms
    elif last == len(frames) - 1:
        end = hi
    else:
        end = _edge(scene_at, frames[last][0], frames[last + 1][0])
    return (start, end) if end - start >= MIN_SPAN_MS[kind] else None


def check(
    cfg: Config, grab_frame: Callable[[int], bytes], duration_ms: int, anchors: dict[str, Sequence[tuple[int, int]]]
) -> dict[str, Verdict]:
    """A verdict for each kind in ``anchors`` (``{kind: [(start_ms, end_ms), ...]}``) the frames support; ``{}`` when
    the model is not loaded or answers fail (the check then simply did not happen)."""
    if not model_ready(cfg):
        return {}
    verdicts = {}
    for kind, spans in anchors.items():
        if not spans:
            continue
        lo, hi = region(kind, spans, duration_ms)
        asked: dict[int, tuple[str, bool]] = {}

        def scene_at(ms, asked=asked):
            if ms not in asked:
                answer = judge(cfg, grab_frame(ms))
                if answer is None:
                    raise OSError("the model did not answer")
                asked[ms] = answer
            scene, overlay = asked[ms]
            # Story footage with text laid over it is, after the credits, a preview's caption: read it so.
            return "preview" if scene == "story" and overlay else scene

        try:
            span = survey(kind, lo, hi, duration_ms, scene_at)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("Local vision check ended: %s", type(exc).__name__)
            return {}
        if span:
            frames = tuple((t, s, o) for t, (s, o) in sorted(asked.items()))
            verdicts[kind] = Verdict(kind, span[0], span[1], frames, cfg.model)
    return verdicts
