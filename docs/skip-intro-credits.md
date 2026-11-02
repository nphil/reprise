---
title: Skip Intro and Skip Credits detection for Plex, Emby and Jellyfin
heading: Skip Intro and Skip Credits
description: Set up Skip Intro and Skip Credits for Plex, Jellyfin and Emby. Check subscriptions, node or plugin requirements, then inspect one file.
---

> **Reprise fork notice:** This page preserves the upstream Media Preview Generator guide. Its server setup and legacy marker workflow are not Reprise's review-first workflow. Reprise detector output is a proposal: inspect and approve it before separately preparing and applying a publication; Plex writes are off by default. Detection evidence is exploratory, not a general accuracy or preservation guarantee. See the [README](../README.md#detection-evidence-and-limits) and [playback API guide](reprise-api.md).

In the inherited Media Preview Generator workflow, detection can send each file's intro and end-credit positions to Plex, Emby and Jellyfin, so viewers get a skip button. It detects each file once and sends the same answer to every server that has the file. Jellyfin and Emby use its plugins. This runs in the same Docker container as preview thumbnails and stays off until enabled per server. These instructions do not describe Reprise detection, which produces proposals for review.

## What viewers get, and what each server needs

- **Plex (in the inherited workflow).** Skip Intro and Skip Credits buttons. The server needs Plex Pass, and so do viewers, or a Plex Home whose admin has it: without it, Plex hides skip buttons altogether. Plex has no way for another app to add markers, so the upstream workflow writes them into Plex's own database after a separate one-time server confirmation. That only works on the Plex machine, so either run the app there or run a [Plex database node](remote-nodes.md) next to Plex. Reprise proposals do not write markers automatically.
- **Jellyfin 10.11 or 12.0.** Jellyfin has no intro detection of its own: it shows skip segments that a plugin provides ([Jellyfin docs](https://jellyfin.org/docs/general/server/metadata/media-segments/)). This app's [Media Preview Bridge plugin](guides.md#jellyfin-the-media-preview-bridge-plugin), the same one trickplay uses, is that plugin. Viewers get Skip Intro and Skip Credits, plus Skip Recap and Skip Preview.
- **Emby 4.9 or 4.10.** Skip Intro, and Skip Credits through Emby's Up Next prompt. It works through this app's [Media Preview Bridge for Emby plugin](guides.md#emby-the-media-preview-bridge-for-emby-plugin). Skip Intro also needs Emby Premiere, which is Emby's rule; the credits prompt works without it. Emby has no credits end, so skipping credits goes to the end of the file, past any scene after the credits.

In that inherited workflow, Plex and Emby can have markers of their own. With **Use ours**, the default, its markers replace them. **Keep Plex's** or **Keep Emby's** leaves them alone. For what each server offers by itself, see the [comparison](comparison.md).

## Set up and check the first result

1. [Install Media Preview Generator](getting-started.md) and connect your server. Use the server-specific marker setup here only if you intentionally use that separate inherited workflow; it does not enable or approve Reprise proposals. For Reprise, follow the library-first [review workflow](../README.md#review-your-library) and inspect evidence and the original video before approval.
2. Choose libraries in the same server's **Libraries** tab only for inherited Media Preview Generator features you intend to use. The **Intro & Credits** and **Previews** selections are independent. Use **Setup Health** to check server requirements.
3. The upstream Intro & Credits source and job instructions below describe Media Preview Generator, not Reprise's current detector profile. In Reprise, analysis creates proposals for review; it does not automatically approve or publish them.
4. For inherited Media Preview Generator jobs, open **Tools → Inspector** and inspect a known episode or movie. Compare detected positions with the video and published result; adjust or lock markers there only when using that separate workflow.

## How it detects intros and credits

It looks in four places:

- **Chapters** already in the file.
- **Online skip databases.**
- **The theme song** a season's episodes share (TV only).
- **The credit roll itself**, read from the text on screen near the end of the file.

That describes the inherited Media Preview Generator workflow, not Reprise's current detector. Reprise analysis proposes ranges for review and does not automatically approve or publish markers. Its exploratory results are not validated for general accuracy; see the [evidence and limitations](../README.md#detection-evidence-and-limits).

<details><summary>Optional: native audio verification limitations</summary>

Verification compares all selected native audio channels without downmixing. Before audio matching, it compares exact decoder-printed declaration tokens, including presence, value and scope. Audio-service declarations can be initial-only or uniformly per-frame; these scopes are not interchangeable. An initial-only declaration describes the first decoded frame, not stability across the whole track. Matrix and downmix declarations must be uniform across decoded guard frames and match between the pair; matrix metadata may be absent, and absence is not assumed to mean any particular matrix. Unknown, malformed, duplicate or changing declarations fail closed. Downmix coefficients are retained but never applied. Printed values have limited precision: equality is not proof of bit-exact underlying metadata, identical rendered output or audibility. This work remains experimental and does not establish family-wide fitness or publication safety.

</details>

## When it runs

- **On import.** When Sonarr or Radarr imports a file, the app makes the previews first, then detects the intro and credits.
- **Across a library.** Start an Intro & Credits job for the libraries you choose, or put one on a schedule.
- **On your worker groups.** It runs on any [worker group](guides.md#worker-groups-and-availability) that allows Intro & Credits, in the same job queue and with the same priorities as previews.
- **Only where you [turn it on](guides.md#turning-it-on).** Each server has its own **Send intro & credits markers to this server** switch, off by default, and an **Intro & Credits** column on its Libraries tab.

## Limits

- **Sports libraries start switched off.** No online source covers sports, and a broadcast's on-screen text is easily taken for credits.
- **Movies have little online coverage**, so they mostly rely on the credit roll.
- **Broadcast TV with channel logos or scores on screen is a weak spot.** It sometimes skips into the story.
- **Shows with burned-in captions through the story** may get no credits marker from on-screen text when the credits run over story footage.
