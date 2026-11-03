# Reprise

Reprise is a review-first, self-hosted media analysis application built on Media Preview Generator and Recurring Content Detector research. It finds repeated material and lets you protect unique scenes before approving skips. Detection is a suggestion, not an understanding of the story: tiny visual edits and quiet or brief changes can be missed. Plex writes are off by default.

## Review your library

Start in the library-first interface: connect a Plex server, refresh a library, choose an episode and its exact media version, and explicitly request analysis. Refresh indexes the library; it does not implicitly start analysis. Analysis, video comparison previews and refresh run as queued jobs with pause, resume and cancel controls. CPU-only operation is supported; no GPU or language model is required.

Detection proposes ranges; it does not approve them. Inspect the evidence and original video, adjust keep and skip ranges, and approve or lock decisions yourself. A keep range vetoes overlapping skips; eligible skips remain separate rather than being joined across protected material. Native signal matching tolerates encoding differences and does not guarantee that every unique scene is found. Uncertain or changed evidence is not approval.

The [playback API](docs/reprise-api.md) serves saved results using the exact Plex playback identity, including the selected audio stream. Missing, stale, ambiguous or unapproved results yield no eligible skips. Named client credentials are scoped, and only their hashes are stored. The direct integration API is available for a future Marquee client, but this repository does not provide that client or deploy anything to a TV. Exact range ends and the explicit terminal flag must be respected.

## Detection evidence and limits

Synthetic checks exercise detection and preservation using reproducible generated media. Results are proposals, and ranking scores are not calibrated probabilities.

The detector compares native decoded video and every selected audio channel at fixed offsets, with explicit uncovered gaps and inward range bounds. Unknown evidence, keep vetoes and owner approval remain in force. Unmarked color fields are accepted as-is only under matching native declarations: Reprise does not invent a BT.709 profile, convert color, or claim rendered or semantic equivalence. Tiny visual edits can be missed because the comparison tolerates normal encoding noise; quiet or brief story changes can also be missed. Review every proposed skip and add protected ranges for scenes you want to retain. Subtitles and player overlays are not covered; unsupported formats or stream mappings remain inert. Synthetic checks are not a general narrative-safety guarantee.


## Quick start

An unsigned image is published on GitHub Container Registry. The examples pin the accepted image built from commit `170b633b8e6adeefadf5201106fbfce51a19e8e5`; they do not update automatically. There is no versioned release or `latest` tag.

1. Replace the host paths in `docker-compose.example.yml`. For production, set `IMAGE` to the independently accepted image digest described below.
2. Run `docker compose -f docker-compose.example.yml up -d`.
3. Visit port 8080. Sign in with the token in the persistent config directory's `auth.json`, or set `WEB_AUTH_TOKEN` securely before starting. Connect your media server and review the configuration.

Do not expose the interface publicly without appropriate authentication and transport security.

The image stores settings and results in `/config`, reads media from `/media`, and listens on TCP 8080 by default. The example runs on CPU, mounts media read-only and does not mount Plex's database. Other inherited features can write to server APIs or paths if configured: a read-only media mount is not an authorization boundary for a connected server. A writable Plex database mount is **not** a default; grant one only for an explicitly authorized publishing operation, after backups and validation. The pinned image uses CPU-focused FFmpeg 8.1.2 and matching ffprobe on PATH, with a separate patched loudness binary. Its FFmpeg has no enabled GPU backends. Dolby Vision Profile 5 colour accuracy is not established.

The container media mount target must match the local path in the configured path mapping so Reprise can find the files. When installing with the Unraid template, you can override the media mount Target to match that mapping.

The Unraid template in `unraid-templates/reprise.xml` pins the same accepted image digest as Compose. Configure an internal reverse proxy separately.

## Plex and Sonarr/Radarr setup

For review-only use, configure the Plex API connection, enable the intended libraries and map each Plex media prefix to a read-only mount inside Reprise. Refresh the TV library from Library to index episodes; this does not start analysis. Choose episodes explicitly and review the proposed ranges before approval. Keep Plex publication disabled unless separately authorized.

The inherited Plex configuration validator also expects `PLEX_CONFIG_FOLDER` to name an existing container directory containing `Media/localhost`. Linux paths are case-sensitive: `/Plex` and `/plex` differ. For example, set the environment variable and the Plex server output folder to `/plex`, and expose only the real Plex `Media/localhost` subtree at `/plex/Media/localhost` read-only. A Plex database mount is not needed for API connections or native indexing. Generating legacy Plex preview thumbnails is different: it writes BIF files into that subtree and requires separately authorized write access.

Sonarr/Radarr integration uses authenticated webhooks, not stored arr API clients in Reprise. Save connections to the canonical HTTPS Reprise `/api/webhooks/sonarr` and `/api/webhooks/radarr` endpoints, authenticate through the password/header field and test from each arr app. Configure the webhook path prefixes alongside the media path mappings. In review-only mode leave import/upgrade events and automatic analysis schedules off. A successful Test confirms connectivity but does not enable automation. These inherited import hooks do not trigger native Reprise analysis; see the [thumbnail workflow guide](docs/sonarr-radarr-preview-thumbnails.md) before enabling them.

Use storage-backed scratch space with at least 1 GiB free: `TMP_FOLDER=/config/tmp` avoids the example's deliberately small `/tmp` tmpfs. Configuration validation creates the configured folder if needed. Python temporary files use the separate `TMPDIR` setting; to redirect those too, create the writable config-volume `tmp` directory with the application user's ownership before starting the container, then set `TMPDIR=/config/tmp`. Do not rely on changing this variable after Python has already selected its temporary directory.

Known follow-on work is tracked separately: [GPU-assisted analysis](https://github.com/nphil/reprise/issues/1) and [consistent Reprise navigation across retained pages](https://github.com/nphil/reprise/issues/2). Neither is claimed as implemented.

## Optional GPU access on Unraid

GPU visibility is separate from GPU acceleration. The accepted image detects NVIDIA and Intel devices, but its FFmpeg reports no hardware accelerators. The inherited preview pipeline has CUDA/VAAPI selection code; it cannot use those backends with this binary. Native intro/credits analysis currently uses software decoding and CPU comparisons, independently of preview worker selections. Passing GPUs through does not change that. Leave GPU workers disabled until a GPU-capable image and the relevant processing path have been verified.

For optional device access, preserve the existing resource/security flags and add `--runtime=nvidia` to Extra Parameters. Set `NVIDIA_VISIBLE_DEVICES` to the chosen GPU UUID and `NVIDIA_DRIVER_CAPABILITIES` to `compute,video,utility` using the dedicated environment-variable fields. The public template defaults to `void` (no NVIDIA device). Never enter a bare `NVIDIA_DRIVER_CAPABILITIES=...` assignment in Extra Parameters: it is not a Docker option.

Add the actual Intel card and render nodes as Device fields after identifying their PCI device on the host. Do not assume `renderD128` is Intel: on the verified dual-GPU host, NVIDIA owns that node and Intel uses `card1`/`renderD129`. Add the nodes' numeric group using `--group-add=<device-group-id>` and verify access as the actual application user, including its supplementary groups. Do not use privileged mode or change host device permissions.

Verify both device discovery and actual FFmpeg hardware decoding separately. Restoring a missing asset/runtime/device mapping cannot add missing FFmpeg capabilities. Accelerating native analysis requires a GPU-enabled FFmpeg build plus an explicit analysis decoding path and end-to-end timing/preservation checks; it does not require an LLM.

## Image delivery and acceptance

The image workflow makes one cold candidate build, pushes it by digest, and checks the bundled source and license artifacts, FFmpeg configuration, and startup health before publishing a full source-commit tag. A tag identifies the source revision; use the immutable image digest to ensure the image deployed is exactly the one independently tested and accepted. These automated checks do not establish legal compliance or replace independent acceptance.

The candidate built from source `170b633b8e6adeefadf5201106fbfce51a19e8e5` with digest `sha256:cfd007e23417dbb8c4dab8ce7e1e5c46b81acb1102bfdd30d4424175a1b2ec79` passed the independent holdout and was accepted on that basis. Full CI remains a launch gate. Production must use that exact digest, not a fresh rebuild or a tag alone. Both deployment examples pin this accepted digest.

An earlier two-cold-build comparison of revision `ac40` found matching runtime configuration and uncompressed layer inventories; source, license and startup checks passed. That comparison concerned a different revision and does not establish that current rebuilds are identical.

## Publication safety

Plex publication is off by default. Preparation may be reviewed without enabling writes; applying or undoing a change requires explicit enablement and current, approved, compatible results for every playable version. Unsupported, missing or mismatched versions and unsafe end-of-file ranges are refused rather than guessed. Before a Plex write, Reprise records intent separately; after interruption it compares the recorded before and after states and marks any other state uncertain rather than replaying blindly. Undo refuses when the affected Plex state has changed since publication. Back up Plex and validate on disposable data before considering production use.

Saving or locking review decisions does not publish them. Prepare, inspect and apply are separate steps; incompatible disjoint or protected ranges are refused rather than merged into an unsafe Plex marker. Recovery does not blindly repeat an interrupted write. The retained upstream guides describe Media Preview Generator and can differ from this fork; use this README and the [Reprise API guide](docs/reprise-api.md) for current behavior.

## Attribution and provenance

Reprise derives from [Media Preview Generator](https://github.com/stevezau/media_preview_generator), MIT copyright 2025 Steve Adams and contributors; see [LICENSE](LICENSE). The repository preserves its history from commit `49fd9f6592d7b74139d31075a75f83bdf1a3bb28`. [Recurring Content Detector](https://github.com/nielstenboom/recurring-content-detector), MIT copyright 2019 Niels ten Boom, is retained with its [license](third_party/recurring-content-detector/LICENSE) and non-squashed history. The original revision was `b248e17fa9f8d384ae8a146abc7a5d47190aaaa6`; the filtered imported revision is `9905c17c9e17a08a1b99bd90708d867f978015eb`. Two film-derived test videos with unverified redistribution rights were removed from every imported revision, so their original integration tests need independently licensed fixtures.

The bundled Manrope font identifies itself as version 4.504, copyright 2019 The Manrope Project Authors. Its [SIL Open Font License 1.1 and copyright notice](media_preview_generator/web/static/fonts/MANROPE-LICENSE.txt) are distributed alongside the font; the [Manrope project](https://github.com/sharanda/manrope) is its upstream.

The FFmpeg built from the current source includes software based in part on the work of the Independent JPEG Group. Its source, build configuration and applicable license notices are included in newly built images.

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
