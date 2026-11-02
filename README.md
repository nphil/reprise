# Reprise

Reprise is an in-development, self-hosted media analysis and review application built on Media Preview Generator and Recurring Content Detector research. It aims to find repeated material while preserving unique scenes, but that outcome is **not validated for general use**. Treat every detection as a proposal and review it before any change. Do not enable production Plex writes during evaluation.

## Review your library

Start in the library-first interface: connect a Plex server, refresh a library, choose an episode and its exact media version, and explicitly request analysis. Refresh indexes the library; it does not implicitly start analysis. Analysis, video comparison previews and refresh run as queued jobs with pause, resume and cancel controls. CPU-only operation is supported; no GPU or language model is required.

Detection proposes ranges; it does not approve them. Inspect the evidence and original video, adjust keep and skip ranges, and approve or lock decisions yourself. A keep range vetoes overlapping skips; eligible skips remain separate rather than being joined across protected material. Codec-tolerant verification is still under implementation (see [current evidence and limits](#detection-evidence-and-limits)); uncertain or changed evidence is not approval.

The [playback API](docs/reprise-api.md) serves saved results using the exact Plex playback identity, including the selected audio stream. Missing, stale, ambiguous or unapproved results yield no eligible skips. Named client credentials are scoped, and only their hashes are stored. The direct integration API is available for a future Marquee client, but this repository does not provide that client or deploy anything to a TV. Exact range ends and the explicit terminal flag must be respected.

## Detection evidence and limits

Current measurements are separate exploratory protocols, not accuracy scores. With pinned RCD, 18 unlabeled real windows produced 18 opening proposals totaling 25 ranges; tails were proposed for 15/18 windows, totaling 15 ranges. Opening and tail passes took 799.17 and 418.04 seconds; grouping took 783.58 seconds across 6 groups on 2 CPUs/2 GiB at nice 19 (peak 222,568,448 bytes). MPG's season-audio pass found openings in 5/18 and tails in 0/18; its separate credits-text pass returned real-tail candidates for 18/18 in 3,222.407 seconds at 1 CPU/2 GiB, nice 19. These real windows have no ground truth, so proposals/candidates are not verified detections.

Separate synthetic checks are also limited: an older exact-match Reprise run on four independently encoded labeled cases found 0/72.8 seconds of repeats and returned unknown for 145/145 checks; its protected-overlap check failed vacuously, so it is not evidence of preservation quality. Two labeled synthetic MPG-credits cases totaling 32/47 seconds returned no candidate. These protocols cannot be combined or called accuracy. Scores are ranking hints, not calibrated probabilities.

The codec-tolerant redesign is still under implementation, and final empirical gates are pending. It aims to compare native decoded video and every selected audio channel with stable piecewise alignment, explicit uncovered gaps and inward range bounds; unknown evidence, keep vetoes and owner approval remain in force. Unmarked color fields are accepted as-is only under matching native declarations: Reprise does not invent a BT.709 profile, convert color, or claim rendered or semantic equivalence. Even a fully checked signal match remains a proposal, not proof every brief or quiet story change is absent. Subtitles and player overlays are not covered; unsupported formats or stream mappings remain inert. This is not production-quality assurance.


## Quick start

An unsigned image is published on GitHub Container Registry. The examples pin the image built from commit `2e6e9c183e886283d5539f4fc423a98bdb074605`; they do not update automatically. There is no versioned release or `latest` tag.

1. Replace the host paths in `docker-compose.example.yml`. Optionally set `IMAGE` to a different published immutable image tag or digest.
2. Run `docker compose -f docker-compose.example.yml up -d`.
3. Visit port 8080. Sign in with the token in the persistent config directory's `auth.json`, or set `WEB_AUTH_TOKEN` securely before starting. Connect your media server and review the configuration.

To use a different published build, set `IMAGE` to its full source-commit tag or digest before starting Compose. Do not expose the interface publicly without appropriate authentication and transport security.

The image stores settings and results in `/config`, reads media from `/media`, and listens on TCP 8080 by default. The example runs on CPU, mounts media read-only and does not mount Plex's database. Other inherited features can write to server APIs or paths if configured: a read-only media mount is not an authorization boundary for a connected server. A writable Plex database mount is **not** a default; grant one only for an explicitly authorized publishing operation, after backups and validation. GPU passthrough is optional.

The Unraid template in `unraid-templates/reprise.xml` uses the same pinned image. Select a newer published immutable image to update. Configure an internal reverse proxy separately.

## Publication safety

Plex publication is off by default. Preparation may be reviewed without enabling writes; applying or undoing a change requires explicit enablement and current, approved, compatible results for every playable version. Unsupported, missing or mismatched versions and unsafe end-of-file ranges are refused rather than guessed. Before a Plex write, Reprise records intent separately; after interruption it compares the recorded before and after states and marks any other state uncertain rather than replaying blindly. Undo refuses when the affected Plex state has changed since publication. Back up Plex and validate on disposable data before considering production use.

Saving or locking review decisions does not publish them. Prepare, inspect and apply are separate steps; incompatible disjoint or protected ranges are refused rather than merged into an unsafe Plex marker. Recovery does not blindly repeat an interrupted write. The retained upstream guides describe Media Preview Generator and can differ from this fork; use this README and the [Reprise API guide](docs/reprise-api.md) for current behavior.

## Attribution and provenance

Reprise derives from [Media Preview Generator](https://github.com/stevezau/media_preview_generator), MIT copyright 2025 Steve Adams and contributors; see [LICENSE](LICENSE). The repository preserves its history from commit `49fd9f6592d7b74139d31075a75f83bdf1a3bb28`. [Recurring Content Detector](https://github.com/nielstenboom/recurring-content-detector), MIT copyright 2019 Niels ten Boom, is retained with its [license](third_party/recurring-content-detector/LICENSE) and non-squashed history. The original revision was `b248e17fa9f8d384ae8a146abc7a5d47190aaaa6`; the filtered imported revision is `9905c17c9e17a08a1b99bd90708d867f978015eb`. Two film-derived test videos with unverified redistribution rights were removed from every imported revision, so their original integration tests need independently licensed fixtures.

The bundled Manrope font identifies itself as version 4.504, copyright 2019 The Manrope Project Authors. Its [SIL Open Font License 1.1 and copyright notice](media_preview_generator/web/static/fonts/MANROPE-LICENSE.txt) are distributed alongside the font; the [Manrope project](https://github.com/sharanda/manrope) is its upstream.

To update the detector, make a disposable clone and filter it *before* pulling it into this repository. Replace `<revision>` with the chosen upstream commit. Do not fetch its unfiltered history into Reprise.

```sh
git clone --no-tags https://github.com/nielstenboom/recurring-content-detector.git /tmp/reprise-rcd-update
git -C /tmp/reprise-rcd-update checkout -B filtered <revision>
git -C /tmp/reprise-rcd-update filter-branch --force --index-filter 'git rm --cached --ignore-unmatch tests/data/video0.mp4 tests/data/video1.mp4' --prune-empty -- filtered
git subtree pull --prefix=third_party/recurring-content-detector /tmp/reprise-rcd-update filtered

Review new upstream assets and licensing before an update; filtering those two known paths alone does not prove other material is redistributable. Record the original and filtered revision with each update.

The GPU capability probe clip is generated synthetic video, not footage. Reproduce it with FFmpeg:

```sh
ffmpeg -f lavfi -i testsrc2=size=320x240:rate=24:duration=2 -an -c:v libx264 -pix_fmt yuv420p -movflags +faststart -y media_preview_generator/gpu/assets/test_video.mp4
```

Real-window measurements are exploratory and unlabeled, and synthetic checks are limited and protocol-specific; none establishes representative real-library accuracy, preservation quality or production write safety.
