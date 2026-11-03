# Reprise

Reprise is a review-first, self-hosted media analysis application built on Media Preview Generator and Recurring Content Detector research. It finds repeated material and lets you protect unique scenes before approving skips. Detection is a suggestion, not an understanding of the story: tiny visual edits and quiet or brief changes can be missed. Plex writes are off by default.

## Review your library

Start in the library-first interface: connect a Plex server, refresh a library, choose an episode and its exact media version, and explicitly request analysis. Refresh indexes the library; it does not implicitly start analysis. Analysis, video comparison previews and refresh run as queued jobs with pause, resume and cancel controls. CPU-only operation is supported and no language model is required; a GPU passed to the container decodes the analysed video when it checks out ([Optional GPU decoding](#optional-gpu-decoding)).

Detection proposes ranges; it does not approve them. Inspect the evidence and original video, adjust keep and skip ranges, and approve or lock decisions yourself. A keep range vetoes overlapping skips; eligible skips remain separate rather than being joined across protected material. Native signal matching tolerates encoding differences and does not guarantee that every unique scene is found. Uncertain or changed evidence is not approval.

The [playback API](docs/reprise-api.md) serves saved results using the exact Plex playback identity, including the selected audio stream. Missing, stale, ambiguous or unapproved results yield no eligible skips. Named client credentials are scoped, and only their hashes are stored. The direct integration API is available for a future Marquee client, but this repository does not provide that client or deploy anything to a TV. Exact range ends and the explicit terminal flag must be respected.

## Detection evidence and limits

Synthetic checks exercise detection and preservation using reproducible generated media. Results are proposals, and ranking scores are not calibrated probabilities.

The detector compares native decoded video and every selected audio channel at fixed offsets, with explicit uncovered gaps and inward range bounds. Unknown evidence, keep vetoes and owner approval remain in force. Unmarked color fields are accepted as-is only under matching native declarations: Reprise does not invent a BT.709 profile, convert color, or claim rendered or semantic equivalence. Tiny visual edits can be missed because the comparison tolerates normal encoding noise; quiet or brief story changes can also be missed. Review every proposed skip and add protected ranges for scenes you want to retain. Subtitles and player overlays are not covered; unsupported formats or stream mappings remain inert. Synthetic checks are not a general narrative-safety guarantee.

The one exception to protecting unconfirmed content is Plex's own intro or credits marker for the exact file. When comparing episodes cannot confirm it, Reprise lists it as a clearly labelled Plex suggestion instead of a protective keep. It skips nothing until you approve that exact range yourself, in a separate and deliberate step for each range, and keep ranges still veto it. Reprise compares at most the three nearest episodes of the season.


## Quick start

An unsigned image is published on GitHub Container Registry. The examples pin the accepted image built from commit `0dc658f559f86b5ba8e9d3cab474636efcdf6aca`; they do not update automatically. There is no versioned release or `latest` tag.

1. Replace the host paths in `docker-compose.example.yml`. For production, set `IMAGE` to the independently accepted image digest described below.
2. Run `docker compose -f docker-compose.example.yml up -d`.
3. Visit port 8080. On first run the "Welcome to Reprise" page asks you to choose a username and a password (at least 8 characters); you are signed in right away. Then connect your media server and review the configuration. If you forget the password, see [I forgot my password](docs/faq.md#i-forgot-my-password).

Do not expose the interface publicly without appropriate authentication and transport security.

The image stores settings and results in `/config`, reads media from `/media`, and listens on TCP 8080 by default. The example runs on CPU, mounts media read-only and does not mount Plex's database. Other inherited features can write to server APIs or paths if configured: a read-only media mount is not an authorization boundary for a connected server. A writable Plex database mount is **not** a default; grant one only for an explicitly authorized publishing operation, after backups and validation. Images built from this source use FFmpeg 8.1.2 (GPL, no non-free components) with the matching ffprobe on PATH, NVDEC and VA-API hardware decoding compiled in, and a separate patched CPU-only loudness binary; an image published before GPU decoding was added reports no hardware accelerators in `ffmpeg -hwaccels`. Dolby Vision Profile 5 colour accuracy is not established.

The container media mount target must match the local path in the configured path mapping so Reprise can find the files. When installing with the Unraid template, you can override the media mount Target to match that mapping.

The Unraid template in `unraid-templates/reprise.xml` pins the same accepted image digest as Compose. Configure an internal reverse proxy separately.

## Sign in and your account

The first visit shows a "Welcome to Reprise" page where you choose a username and a password (at least 8 characters). That is the only account, and the page never appears again once it exists. Until then anyone who can open the page can make it, so do it right after the first start. A short tour of the Library, Analyze and Review follows; Skip closes it for good, and **Take the tour** in the sidebar or under Settings → Advanced → Account opens it again. The same Account section changes the password.

Forgot the password? There is no email reset. Open a console in the container (Unraid: Docker tab, the Reprise icon, **Console**; elsewhere `docker exec -it <container> sh`) and run one of these:

```bash
python3 -m media_preview_generator.web.account_cli reset-password   # a new password for the same account
python3 -m media_preview_generator.web.account_cli reset-account    # start over: the welcome page asks again
```

Both apply at once and sign every browser out. Deleting `auth.json` from the config folder does the same as `reset-account`. Neither touches the webhook secret, your servers or your results.

Sonarr, Radarr, Plex and your own scripts use a separate **webhook secret** (Settings → Connections: show, copy, regenerate). Upgrading from the old token login changes nothing for them: the first start copies the old token (`WEB_AUTH_TOKEN` if it was set, otherwise the one in `auth.json`) into the webhook secret unchanged, and the welcome page then asks for your account. `WEB_AUTH_TOKEN` no longer signs anyone in and can be removed. With `AUTH_METHOD=external` (a proxy or VPN does the signing in) there is no account.

## Plex and Sonarr/Radarr setup

For review-only use, configure the Plex API connection, enable the intended libraries and map each Plex media prefix to a read-only mount inside Reprise. Refresh the TV library from Library to index episodes; this does not start analysis. Choose episodes explicitly and review the proposed ranges before approval. Keep Plex publication disabled unless separately authorized.

The inherited Plex configuration validator also expects `PLEX_CONFIG_FOLDER` to name an existing container directory containing `Media/localhost`. Linux paths are case-sensitive: `/Plex` and `/plex` differ. For example, set the environment variable and the Plex server output folder to `/plex`, and expose only the real Plex `Media/localhost` subtree at `/plex/Media/localhost` read-only. A Plex database mount is not needed for API connections or native indexing. Generating legacy Plex preview thumbnails is different: it writes BIF files into that subtree and requires separately authorized write access. For review-only use, turn off **Write preview thumbnails into Plex** (Settings → Connections → Plex → Edit → Processing, stored as `output.preview_thumbnails: false`); the Plex health check then reports the read-only mount as correct instead of a critical error, and no preview job writes there.

Sonarr/Radarr integration uses authenticated webhooks, not stored arr API clients in Reprise. Save connections to the canonical HTTPS Reprise `/api/webhooks/sonarr` and `/api/webhooks/radarr` endpoints, authenticate through the password/header field and test from each arr app. Configure the webhook path prefixes alongside the media path mappings. In review-only mode leave import/upgrade events and automatic analysis schedules off. A successful Test confirms connectivity but does not enable automation. These inherited import hooks do not trigger native Reprise analysis; see the [thumbnail workflow guide](docs/sonarr-radarr-preview-thumbnails.md) before enabling them.

Use storage-backed scratch space with at least 1 GiB free: `TMP_FOLDER=/config/tmp` avoids the example's deliberately small `/tmp` tmpfs. Configuration validation creates the configured folder if needed. Python temporary files use the separate `TMPDIR` setting; to redirect those too, create the writable config-volume `tmp` directory with the application user's ownership before starting the container, then set `TMPDIR=/config/tmp`. Do not rely on changing this variable after Python has already selected its temporary directory.

Known follow-on work is tracked separately: [consistent Reprise navigation across retained pages](https://github.com/nphil/reprise/issues/2) is not claimed as implemented. GPU-assisted analysis ([issue 1](https://github.com/nphil/reprise/issues/1)) is limited to decoding the video that is analysed; see [Optional GPU decoding](#optional-gpu-decoding).

## Optional GPU decoding

A GPU passed into the container decodes the video that analysis compares and the comparison previews. Everything else (audio, the ffprobe audit, the comparisons and every decision) stays on the CPU, so results do not change. With no GPU, or after any GPU failure, the same window decodes on the CPU.

- **What uses the GPU.** H.264 in 8-bit 4:2:0 (Baseline, Main, High) and HEVC in 8-bit or 10-bit 4:2:0 (Main, Main 10), from 144 to 4096 pixels on a side. Everything else (MPEG-2, MPEG-4, VC-1, VP9, AV1, 4:2:2, 4:4:4, H.264 Hi10P) decodes on the CPU.
- **It must match the CPU.** At startup each detected GPU decodes two packaged reference clips and has to match the CPU frame for frame: timestamps, pixel format, colour declarations and every plane checksum. A GPU that differs is not used for that codec and the log says why.
- **It always falls back.** A decode that fails on the GPU is repeated on the CPU, which shows whether the GPU or the file was at fault; a file the CPU cannot decode either fails exactly as it did before. Frames already read are checked against the repeat rather than repeated, so a window is never half one decoder and half another unseen. Five failures in a row pause that GPU for ten minutes.
- **You can see and switch it.** The Library page shows one line under its title, for example `Analysis uses GPU: NVIDIA Tesla P40 (NVDEC decode), then Intel UHD Graphics 630 (VAAPI decode).` or `Analysis uses CPU (no GPU found).`, with a button that turns GPU decoding off and on. The same text is `acceleration.label` in `GET /api/reprise/settings`, and `PATCH /api/reprise/settings` with `{"gpu_decode": false}` does what the button does. A GPU turned off (or given no workers) in the inherited GPU settings is not used here either.
- **What it saves.** Only the video decoder moves. The ffprobe audit decodes the same window again on the CPU and costs about as much as the decoder, so a whole analysis speeds up less than its decode does. Measured on the host above (container limited to one CPU, host busy with other work; 11 episodes from 1080p to 4K, each decoded identically on CPU, NVDEC and VA-API): the P40 cut the decoder's CPU time by 26% (46% for 4K H.264, 10% for 10-bit HEVC) and the windows' wall time by 15%, and one whole analysis finished about 4% sooner. The Intel iGPU did not beat the CPU: 7% more CPU time overall and 12% less at 4K, because every frame is copied back through memory. The P40 is tried first, so the iGPU decodes only what the P40 cannot; to keep the iGPU free for other work, turn that GPU off in the GPU settings.

Settings that work (verified on an Unraid host with a Tesla P40 and an Intel UHD Graphics 630). Preserve the existing resource and security flags and add:

| Setting | Value |
|---|---|
| Extra Parameters | `--runtime=nvidia --group-add=<GID>` (the `GID` is the numeric group of the Intel render node: `ls -ln /dev/dri`; it was 18 here) |
| Variable `NVIDIA_VISIBLE_DEVICES` | the P40's UUID from `nvidia-smi -L` (the template default `void` means no NVIDIA GPU) |
| Variable `NVIDIA_DRIVER_CAPABILITIES` | `compute,video,utility`; `video` supplies the NVDEC library. Never enter a bare `NVIDIA_DRIVER_CAPABILITIES=...` assignment in Extra Parameters: it is not a Docker option |
| Device | `/dev/dri/card1` and `/dev/dri/renderD129`, the Intel nodes on that host. Do not assume `renderD128` is Intel: NVIDIA owns it there |

Do not use privileged mode or change host device permissions. In Docker Compose the same settings are `runtime: nvidia`, the two environment variables, `devices:` for the nodes and `group_add:` (see the commented lines in `docker-compose.example.yml`). Either GPU works alone.

Check it from the host once the container is up:

```sh
docker exec reprise ffmpeg -hide_banner -hwaccels                 # lists cuda and vaapi
docker exec reprise nvidia-smi -L                                 # the NVIDIA GPU
docker logs reprise 2>&1 | grep 'Reprise decodes on the GPU'      # one line per GPU that passed its check
nvidia-smi dmon -s u                                              # on the host: `dec` rises while an analysis runs
```

Intel decoding needs the container's media driver to match its libva; images built from this source carry a matching pair. VA-API on other Intel generations depends on that driver's support for them, and AMD GPUs take the same VA-API path with the image's Mesa driver (not tested here). Whatever the hardware, a GPU is used only after it passes the startup check above. The preview and Intro & Credits workers inherited from the original project keep their own per-GPU worker settings, and they see these GPUs too: a newly detected GPU is enabled there by default with one worker.

## Image delivery and acceptance

The image workflow makes one cold candidate build, pushes it by digest, and checks the bundled source and license artifacts, FFmpeg configuration, and startup health before publishing a full source-commit tag. A tag identifies the source revision; use the immutable image digest to ensure the image deployed is exactly the one independently tested and accepted. These automated checks do not establish legal compliance or replace independent acceptance.

The current image, built from source `0dc658f559f86b5ba8e9d3cab474636efcdf6aca`, is `ghcr.io/nphil/reprise@sha256:4eae9de3b86b69989e1ee1f7e354546c8a7ae07261b93e6dbcaf81d72c432183`. It passed the candidate image checks and [full CI](https://github.com/nphil/reprise/actions/runs/41724763384). After deployment on the reference Unraid host it started healthy, migrated the old sign-in token unchanged to the webhook secret, opened first-run account setup, and its startup checks matched NVIDIA Tesla P40 (NVDEC) and Intel UHD 630 (VAAPI) decoding frame for frame against the CPU on the reference clips. Both deployment examples pin this exact image; use the digest rather than rebuilding or relying on a tag alone.

The earlier detector candidate from source `170b633b8e6adeefadf5201106fbfce51a19e8e5` passed the independent native-engine holdout. This image changes the verification profile (audio timestamp tolerance, 3 nearest references, Plex suggestions), and that holdout has not been repeated for it.

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
