"""File generation identity. Stat identity is explicitly not a content digest.

A generation is the file's path, size, modification time, duration and stream layout. The change time
(ctime), device number and inode are recorded but are not part of it: a permissions, hard-link or mover
pass moves the change time, Unraid's user-share mount comes back from a reboot with a new device number
(every file in one library read as "changed" after one), and its mover gives a file moved between the
cache and the array a new inode, all without touching the content. A real replacement or edit changes
the size or the modification time, and those still start a new generation.
"""

import hashlib
import json
import os

from .models import MediaVersion


def media_version(path: str, duration_ms: int, stream_signature: str, *, digest: str | None = None) -> MediaVersion:
    path = os.path.realpath(path)
    st = os.stat(path)
    if duration_ms <= 0 or not stream_signature:
        raise ValueError("duration and stream timing are required")
    # The digest, when supplied, is historical evidence, not a live verification.
    fields = [
        path,
        st.st_size,
        st.st_mtime_ns,
        duration_ms,
        stream_signature,
        ["sha256", digest] if digest else ["stat-only", None],
    ]
    identifier = hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()
    return MediaVersion(
        identifier,
        path,
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
        duration_ms,
        stream_signature,
        st.st_dev,
        st.st_ino,
        "sha256-recorded" if digest else "stat-only",
        digest,
    )


def is_current(version: MediaVersion) -> bool:
    try:
        st = os.stat(version.canonical_path)
        return os.path.realpath(version.canonical_path) == version.canonical_path and (
            st.st_size,
            st.st_mtime_ns,
        ) == (version.size_bytes, version.mtime_ns)
    except OSError:
        return False


def same_file(left: MediaVersion, right: MediaVersion) -> bool:
    """Whether two current versions are one file on disk right now (a hard link, or the same path twice): the live
    device and inode, compared at the same moment, since the recorded ones may predate a reboot or a mover pass."""
    try:
        return os.path.samefile(left.canonical_path, right.canonical_path)
    except OSError:
        return False
