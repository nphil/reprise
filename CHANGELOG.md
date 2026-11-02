# Changelog

## Unreleased

- Introduced Reprise's library-first workflow for indexing Plex libraries, reviewing evidence and editing, approving or locking keep and skip ranges. Keep ranges veto overlapping skips; eligible skips remain disjoint. Detection proposes decisions, not approvals, and uncertain or changed evidence requires review.
- Added queued CPU-supported analysis, comparison previews and library refresh, with pause, resume and cancellation. Refresh and saved-result lookups do not implicitly start analysis. Conservative audio and video verification requires no GPU or language model.
- Adopted review-first, codec-tolerant native signal matching with mandatory owner approval. Tiny visual edits and quiet or brief changes may be missed; the interface and playback API documentation explain this limit. Changing the verification profile invalidates old approvals while retaining owner keeps and locks.
- Switched new image builds to a native FFmpeg 8.1.2 and matching ffprobe on PATH, while keeping the separate patched loudness binary. GPU acceleration and Dolby Vision Profile 5 colour accuracy are not established for this build.
- Added a playback API using exact media and selected-audio identity, named scoped client credentials and stored credential hashes. It returns eligible skips only for current, approved results. The API can support a future Marquee integration; no Marquee client or TV deployment is included.
- Added guarded Plex publication: writes default off, review is separate from prepare and apply, incompatible projections are refused, interrupted writes are reconciled rather than blindly repeated, and undo refuses conflicting external changes.
- Retained Media Preview Generator and Recurring Content Detector attribution and license notices. Two upstream detector fixture videos with unverified redistribution rights were filtered from the imported history.

Generated-media checks do not establish representative real-library accuracy, narrative safety or production-write safety. Plex writes remain off by default. See the [README](README.md) and [playback API guide](docs/reprise-api.md) for current behavior and limitations.
