---
title: HDR and Dolby Vision preview thumbnails for Plex, Jellyfin and Emby
heading: HDR and Dolby Vision preview thumbnails
description: HDR tone mapping and Dolby Vision Profile 5 limitations in the CPU-focused FFmpeg build.
---

Tone mapping converts HDR brightness and colour to the range a JPEG thumbnail can show. The standard path handles HDR10, HLG and compatible Dolby Vision base layers. Profile 5 has no such base layer; colours are not guaranteed with the bundled CPU-focused FFmpeg.

## Why the built-ins get it wrong

- **Plex.** Plex staff have said preview thumbnails come from software frame grabbing and that tone mapping "doesn't work on all systems yet" ([forum](https://forums.plex.tv/t/hdr-tonemapping-for-video-thumbnails/755821)). Users report washed-out HDR and green Dolby Vision thumbnails. A 2023 report says it is still happening ([forum](https://forums.plex.tv/t/hdr-tone-mapping-for-video-thumbnails-on-synology-again/830582), user report).
- **Jellyfin.** Trickplay is only tone-mapped when tone mapping is on in Jellyfin's transcoding settings. Otherwise HDR trickplay looks grey. A core developer confirmed this in [#13142](https://github.com/jellyfin/jellyfin/issues/13142). A Dolby Vision colour fix landed via [#8049](https://github.com/jellyfin/jellyfin/issues/8049), but users [still report](https://www.reddit.com/r/jellyfin/comments/1spfb8q/trickplay_images_with_incorrect_colors_for_dolby/) green Dolby Vision trickplay in 2026. If you stay on Jellyfin's built-in, turn transcoding tone mapping on first.
- **Emby.** Per forum reports, Emby's core tone-maps BIF thumbnails ([forum](https://emby.media/community/topic/118095-suggestion-for-bif-file-generation/), user report).

Checked 23 September 2026. See the [comparison page](comparison.md) for sources.

## What this app does, per format

- **HDR10, HLG, HDR10+:** tone-mapped to SDR. The algorithm is set in **Settings → Processing options → Thumbnails → HDR tone mapping**. The default is `hable`; the others are `reinhard`, `mobius`, `clip`, `gamma` and `linear`. If thumbnails look too dark, try `reinhard`.
- **Dolby Vision Profile 7 and 8:** compatible HDR10 or HLG base layers use the standard path.
- **Dolby Vision Profile 5:** no HDR10 fallback exists. Thumbnails may have wrong colours, including a green and purple tint. The previous Jellyfin-specific patch is not included in the bundled FFmpeg. GPU passthrough or a working Vulkan driver alone does not establish support.

## The one setup step: NVIDIA and Dolby Vision Profile 5

No GPU setup is currently established to guarantee Profile 5 colour accuracy in this build.

### NVIDIA graphics files missing from the container

A Vulkan driver is not sufficient to establish Profile 5 support with the bundled FFmpeg.

## Checking the result

- The **Inspector** (`/inspector`) shows the published thumbnails for Plex, Emby or Jellyfin, frame by frame ([Inspector](multi-server.md#inspector-multi-server)).
- To redo files after fixing a setting, start a job with **Processing Mode → Regenerate all**. Otherwise files with a current preview are skipped.
- A worker may report CPU fallback if its GPU path fails; the bundled FFmpeg is CPU-focused and GPU acceleration is not established.

## Speed

Performance depends on the media and hardware; GPU acceleration is not established for the bundled build.

## Limits

- Dolby Vision Profile 5 thumbnails may have wrong colours, including a green and purple tint, even if a GPU is available.
- The thumbnails are SDR by design. Preview thumbnails are small JPEGs, not HDR images.

## Related

- [Generate Plex preview thumbnails with a GPU](plex-preview-thumbnails-gpu.md)
- [Faster Jellyfin trickplay generation](jellyfin-trickplay-gpu.md)
- [Emby BIF thumbnails with a GPU](emby-bif-thumbnails-gpu.md)
