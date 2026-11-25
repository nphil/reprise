"""Still pictures and cut points around one time of an episode, for the approval screen.

A browser cannot always play a file (a codec it lacks), but ffmpeg can always read pictures from it. The review screen
shows a strip of pictures around the start or the end of a skip and lets the owner move that edge, so this module
makes the strip, single pictures of any moment, and finds the cuts (a hard change of scene) an edge can snap to.

Files are read by ffmpeg, a couple at a time at low priority, and every picture is kept in a small cache folder that
never grows past :data:`KEEP` files. A read that fails (a file that moved, a codec ffmpeg lacks) raises ``OSError`` with
a plain sentence; nothing is invented in its place.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections import OrderedDict

WIDTHS = (120, 160, 240, 320, 480, 640, 960)  # picture widths that can be asked for
KEEP = 4000  # pictures kept on disk; the oldest go first
MAX_STRIP = 24  # most pictures in one strip
MIN_STEP_MS = 250
MAX_STEP_MS = 10000
SCENE = 0.3  # how different two pictures must be (0 to 1) to count as a cut
MAX_CUT_WINDOW_MS = 30000
_PRUNE_EVERY = 200  # writes between looks at the cache size
_CUT_MEMORY = 256  # cut lists kept in memory


class Frames:
    """Makes and remembers the pictures of the approval screen. One instance serves the whole app."""

    def __init__(self, ffmpeg, directory, *, slots=2, keep=KEEP):
        self.ffmpeg, self.directory, self.keep = ffmpeg, directory, keep
        self.nice = shutil.which("nice")
        self._slots = threading.BoundedSemaphore(slots)
        self._lock = threading.Lock()
        self._writes = 0
        self._cuts = OrderedDict()

    # -- running ffmpeg -----------------------------------------------------------------------------------------

    def _run(self, args, timeout, verbosity="error"):
        prefix = [self.nice, "-n", "10"] if self.nice else []
        command = [*prefix, self.ffmpeg, "-nostdin", "-v", verbosity, "-threads", "1", *args]
        with self._slots:
            try:
                return subprocess.run(
                    command, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False
                )
            except subprocess.TimeoutExpired as exc:
                raise OSError("Reading the picture took too long.") from exc
            except (OSError, subprocess.SubprocessError) as exc:
                raise OSError("Reprise could not run ffmpeg to read this episode.") from exc

    # -- the cache ----------------------------------------------------------------------------------------------

    def _path(self, version, *parts):
        digest = hashlib.sha1(
            "|".join(str(p) for p in (version.id, version.size_bytes, version.mtime_ns, *parts)).encode()
        ).hexdigest()
        return os.path.join(self.directory, digest[:2], digest + ".jpg")

    @staticmethod
    def _read(path):
        try:
            with open(path, "rb") as handle:
                data = handle.read()
            os.utime(path)  # a picture used lately is the last to go
            return data or None
        except OSError:
            return None

    def _write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".part")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temporary, path)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            return
        with self._lock:
            self._writes += 1
            prune = self._writes % _PRUNE_EVERY == 0
        if prune:
            self._prune()

    def _prune(self):
        """Delete the oldest pictures beyond the limit."""
        found = []
        for folder, _, names in os.walk(self.directory):
            for name in names:
                if name.endswith(".jpg"):
                    path = os.path.join(folder, name)
                    try:
                        found.append((os.stat(path).st_mtime, path))
                    except OSError:
                        continue
        for _, path in sorted(found)[: max(0, len(found) - self.keep)]:
            try:
                os.unlink(path)
            except OSError:
                pass

    # -- pictures -----------------------------------------------------------------------------------------------

    @staticmethod
    def _check_width(width):
        if width not in WIDTHS:
            raise ValueError("Unsupported picture width")

    def still(self, version, at_ms, width):
        """The picture at ``at_ms`` as a JPEG, ``width`` pixels wide."""
        self._check_width(width)
        at_ms = min(max(0, int(at_ms)), max(0, version.duration_ms - 1))
        path = self._path(version, "still", at_ms, width)
        cached = self._read(path)
        if cached:
            return cached
        for seek in (at_ms, max(0, at_ms - 1500)):  # the last picture of a file cannot be reached by seeking to its end
            done = self._run(
                [
                    "-ss",
                    f"{seek / 1000:.3f}",
                    "-i",
                    version.canonical_path,
                    "-an",
                    "-sn",
                    "-dn",
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={width}:-2",
                    "-f",
                    "image2pipe",
                    "-c:v",
                    "mjpeg",
                    "-q:v",
                    "4",
                    "-",
                ],
                60,
            )
            if done.stdout:
                self._write(path, done.stdout)
                return done.stdout
        raise OSError("Reprise could not read a picture from this episode.")

    def strip(self, version, start_ms, step_ms, count, width):
        """``count`` pictures, one every ``step_ms`` from ``start_ms``, side by side in one JPEG (one read of the
        file instead of one per picture). Picture ``i`` shows ``start_ms + i * step_ms``."""
        self._check_width(width)
        if not MIN_STEP_MS <= step_ms <= MAX_STEP_MS or not 1 <= count <= MAX_STRIP:
            raise ValueError("Unsupported strip")
        start_ms = min(max(0, int(start_ms)), max(0, version.duration_ms - 1))
        path = self._path(version, "strip", start_ms, step_ms, count, width)
        cached = self._read(path)
        if cached:
            return cached
        done = self._run(
            [
                "-ss",
                f"{start_ms / 1000:.3f}",
                "-t",
                f"{(count * step_ms) / 1000:.3f}",
                "-i",
                version.canonical_path,
                "-an",
                "-sn",
                "-dn",
                "-vf",
                f"fps=1000/{step_ms},scale={width}:-2,tile={count}x1",
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-c:v",
                "mjpeg",
                "-q:v",
                "5",
                "-",
            ],
            120,
        )
        if not done.stdout:
            raise OSError("Reprise could not read the pictures around this time.")
        self._write(path, done.stdout)
        return done.stdout

    # -- cuts ---------------------------------------------------------------------------------------------------

    def cuts(self, version, start_ms, end_ms):
        """The times (ms, ascending) inside the stretch where the picture changes completely: where a skip would
        start or end cleanly. Empty when the stretch has none."""
        start_ms, end_ms = max(0, int(start_ms)), min(int(end_ms), version.duration_ms)
        if not 0 < end_ms - start_ms <= MAX_CUT_WINDOW_MS:
            raise ValueError("Unsupported stretch")
        key = (version.id, version.size_bytes, version.mtime_ns, start_ms, end_ms)
        with self._lock:
            if key in self._cuts:
                self._cuts.move_to_end(key)
                return list(self._cuts[key])
        done = self._run(
            [
                "-ss",
                f"{start_ms / 1000:.3f}",
                "-t",
                f"{(end_ms - start_ms) / 1000:.3f}",
                "-copyts",
                "-i",
                version.canonical_path,
                "-an",
                "-sn",
                "-dn",
                "-vf",
                f"scale=160:-2,select='gt(scene,{SCENE})',showinfo",
                "-f",
                "null",
                "-",
            ],
            120,
            "info",
        )
        found = sorted(
            {
                t
                for t in (
                    round(float(m.group(1)) * 1000) for m in re.finditer(rb"pts_time:\s*(-?[0-9.]+)", done.stderr)
                )
                if start_ms <= t <= end_ms
            }
        )
        with self._lock:
            self._cuts[key] = tuple(found)
            while len(self._cuts) > _CUT_MEMORY:
                self._cuts.popitem(last=False)
        return found
