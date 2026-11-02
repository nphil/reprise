"""File generation identity. Stat identity is explicitly not a content digest."""
import hashlib
import json
import os
from .models import MediaVersion


def media_version(path: str, duration_ms: int, stream_signature: str, *, digest: str | None = None) -> MediaVersion:
    path = os.path.realpath(path)
    st = os.stat(path)
    if duration_ms <= 0 or not stream_signature:
        raise ValueError('duration and stream timing are required')
    # The digest, when supplied, is historical evidence, not a live verification.
    fields = [path, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_dev, st.st_ino,
              duration_ms, stream_signature, ['sha256', digest] if digest else ['stat-only', None]]
    identifier = hashlib.sha256(json.dumps(fields, separators=(',', ':')).encode()).hexdigest()
    return MediaVersion(identifier, path, st.st_size, st.st_mtime_ns, st.st_ctime_ns, duration_ms, stream_signature, st.st_dev, st.st_ino, 'sha256-recorded' if digest else 'stat-only', digest)


def is_current(version: MediaVersion) -> bool:
    try:
        st = os.stat(version.canonical_path)
        return (version.device is not None and version.inode is not None and
                os.path.realpath(version.canonical_path) == version.canonical_path and
                (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_dev, st.st_ino) ==
                (version.size_bytes, version.mtime_ns, version.ctime_ns, version.device, version.inode))
    except OSError:
        return False
