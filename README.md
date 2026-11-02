# Reprise

Reprise is an in-development, self-hosted media analysis and review application. It builds on Media Preview Generator and incorporates Recurring Content Detector research. Its goal is to find repeated material while keeping unique scenes; **that preservation outcome is not yet validated**. Review results yourself. Do not enable automatic production Plex publication during evaluation.

## Review your library

Start in the library-first interface: connect a Plex server, refresh a library, choose an episode and its exact media version, and explicitly request analysis. Refresh indexes the library; it does not implicitly start analysis. Analysis, video comparison previews and refresh run as queued jobs with pause, resume and cancel controls. CPU-only operation is supported; no GPU or language model is required.

Detection proposes ranges; it never approves them. Inspect the evidence and original video, adjust keep and skip ranges, and approve or lock your decisions. A keep range vetoes overlapping skips; eligible skips remain separate rather than being joined across protected material. Reprise uses conservative audio and video verification, and uncertain or changed evidence is not approval. A changed file or meaningful evidence can require review again.

The [playback API](docs/reprise-api.md) serves saved results using the exact Plex playback identity, including the selected audio stream. Missing, stale, ambiguous or unapproved results yield no eligible skips. Named client credentials are scoped, and only their hashes are stored. The direct integration API is available for a future Marquee client, but this repository does not provide that client or deploy anything to a TV. Exact range ends and the explicit terminal flag must be respected.

## Quick start

An unsigned image is intended for GitHub Container Registry after a successful build and publication. Pin the full source-commit tag shown below, or its digest; there is currently no published image or versioned release, and no `latest` tag is promised. The tag is a usage pattern, not evidence that an image has been built.

1. Export `IMAGE` as a published `ghcr.io/nphil/reprise:sha-<full-commit>` (or an image digest), and replace the host paths in `docker-compose.example.yml`.
2. Run `docker compose -f docker-compose.example.yml up -d`.
3. Visit port 8080. Sign in with the token in the persistent config directory's `auth.json`, or set `WEB_AUTH_TOKEN` securely before starting. Connect your media server and review the configuration.

For example, substitute the actual commit in `export IMAGE="ghcr.io/nphil/reprise:sha-<full-commit>"` before starting Compose. Do not expose the interface publicly without appropriate authentication and transport security.

The image stores settings and results in `/config`, reads media from `/media`, and listens on TCP 8080 by default. The example runs on CPU, mounts media read-only and does not mount Plex's database. Other inherited features can write to server APIs or paths if configured: a read-only media mount is not an authorization boundary for a connected server. A writable Plex database mount is **not** a default; grant one only for an explicitly authorized publishing operation, after backups and validation. GPU passthrough is optional.

The Unraid template in `unraid-templates/reprise.xml` has the same defaults. Choose a published immutable image tag before creating the container. Configure an internal reverse proxy separately.

## Publication safety

Plex publication is off by default. Preparation may be reviewed without enabling writes; applying or undoing a change requires explicit enablement and current, approved, compatible results for every playable version. Unsupported, missing or mismatched versions and unsafe end-of-file ranges are refused rather than guessed. Before a Plex write, Reprise records intent separately; after interruption it compares the recorded before and after states and marks any other state uncertain rather than replaying blindly. Undo refuses when the affected Plex state has changed since publication. Back up Plex and validate on disposable data before considering production use.

Saving or locking review decisions does not publish them. Prepare, inspect and apply are separate steps; incompatible disjoint or protected ranges are refused rather than merged into an unsafe Plex marker. Recovery does not blindly repeat an interrupted write. The retained upstream guides describe Media Preview Generator and can differ from this fork, especially around automatic marker publication: use this README and the [Reprise API guide](docs/reprise-api.md) for current behavior.

## Attribution and provenance

Reprise derives from [Media Preview Generator](https://github.com/stevezau/media_preview_generator), MIT copyright 2025 Steve Adams and contributors; see [LICENSE](LICENSE). The repository preserves its history from commit `49fd9f6592d7b74139d31075a75f83bdf1a3bb28`. [Recurring Content Detector](https://github.com/nielstenboom/recurring-content-detector), MIT copyright 2019 Niels ten Boom, is retained with its [license](third_party/recurring-content-detector/LICENSE) and non-squashed history. The original revision was `b248e17fa9f8d384ae8a146abc7a5d47190aaaa6`; the filtered imported revision is `9905c17c9e17a08a1b99bd90708d867f978015eb`. Two film-derived test videos with unverified redistribution rights were removed from every imported revision, so their original integration tests need independently licensed fixtures.

The bundled Manrope font identifies itself as version 4.504, copyright 2019 The Manrope Project Authors. Its [SIL Open Font License 1.1 and copyright notice](media_preview_generator/web/static/fonts/MANROPE-LICENSE.txt) are distributed alongside the font; the [Manrope project](https://github.com/sharanda/manrope) is its upstream.

To update the detector, make a disposable clone and filter it *before* pulling it into this repository. Replace `<revision>` with the chosen upstream commit. Do not fetch its unfiltered history into Reprise.

```sh
git clone --no-tags https://github.com/nielstenboom/recurring-content-detector.git /tmp/reprise-rcd-update
git -C /tmp/reprise-rcd-update checkout -B filtered <revision>
git -C /tmp/reprise-rcd-update filter-branch --force --index-filter 'git rm --cached --ignore-unmatch tests/data/video0.mp4 tests/data/video1.mp4' --prune-empty -- filtered
git subtree pull --prefix=third_party/recurring-content-detector /tmp/reprise-rcd-update filtered
```

Review new upstream assets and licensing before an update; filtering those two known paths alone does not prove other material is redistributable. Record the original and filtered revision with each update.

The GPU capability probe clip is generated synthetic video, not footage. Reproduce it with FFmpeg:

```sh
ffmpeg -f lavfi -i testsrc2=size=320x240:rate=24:duration=2 -an -c:v libx264 -pix_fmt yuv420p -movflags +faststart -y media_preview_generator/gpu/assets/test_video.mp4
```

No detector accuracy, playback compatibility, or production write safety is claimed until evaluated on representative labeled material.
