# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Nitin manages a self-hosted media library and reviews which repeated material can be skipped without losing unique content. Marquee is a separate playback client that can read approved results.

## Product Purpose

Reprise finds repeated opening and closing material across episodes while protecting special intros, visual or spoken gags, and scenes within or after credits. Success means reviewing trustworthy, precise decisions for the actual media version without skipping story content.

## Operating Context

The home screen starts with the library: browse shows and seasons, select episodes for analysis, and open an episode for detailed comparison and review. Jobs run asynchronously. The app is intended for a managed, internal-only Unraid installation. Source is intended to be public; this does not make the running service public.

## Capabilities and Constraints

- Combine and preserve provenance for Media Preview Generator and Recurring Content Detector using the existing application stack where sound.
- Read media without changing library files. Review analysis before applying changes; Plex writes start disabled.
- Unknown, conflicting, stale or ambiguous evidence must not produce automatic skips. Protected content can occur inside credits or after them.
- Provide an authenticated, documented API for Marquee that returns saved, version-specific skip and protected keep ranges independently of Plex publication. Named client credentials default to read-only.
- Keep useful upstream capabilities, including job handling, server connections, scheduling and diagnostics.
- No cloud video upload or LLM in the initial detection path; CPU operation is required and GPU acceleration is optional.
- Modifying Marquee or its playback behavior is outside this application's implementation without separate authorization.

## Brand Commitments

The name is Reprise; the working line is “Skip the repeat. Keep the story.” HomeLabber is the interface reference, with the active Lucent design language and Rosé Pine palette. An original mark should suggest repeated frames with a deliberately preserved break, rather than a generic play button. No trademark clearance is claimed.

## Evidence on Hand

Evidence remains exploratory and protocol-specific; it does not establish accuracy or preservation safety for a library. Pinned RCD on 18 unlabeled real windows proposed 18 openings in 25 ranges and 15 tails for 15/18 windows (799.17 s and 418.04 s); grouping took 783.58 s across 6 groups on 2 CPUs/2 GiB at nice 19 (peak 222,568,448 bytes). Separate MPG runs found season-audio openings in 5/18 and tails in 0/18, while credits-text returned real-tail candidates for 18/18 in 3,222.407 s at 1 CPU/2 GiB, nice 19. These unlabeled counts are not correctness measures.

An older Reprise exact-match run over four independently encoded labeled synthetic cases found 0/72.8 seconds of repeats, returned unknown for 145/145 checks, and failed its vacuous protected-overlap check. Two separate labeled MPG-credits synthetic cases totaling 32/47 seconds returned no candidate. These protocols must not be combined or described as accuracy; scores are ranking hints, not calibrated probabilities.

Codec-tolerant native-signal verification is under implementation; final empirical gates remain pending. The intended profile compares decoded video and every selected audio channel with piecewise alignment, explicit gaps and inward bounds. It leaves unknown evidence inert and does not approve or publish results. Unmarked color fields are accepted as-is only under matching native declarations: it does not invent BT.709, convert color, or claim rendered/semantic equivalence. A qualified signal match remains a proposal, not proof every brief or quiet story change is absent. Subtitles and player overlays are outside coverage; unsupported formats and stream mappings remain inert.

## Product Principles

- Keep the story when evidence is uncertain.
- Make the library and the next review easy to find.
- Separate analysis, review and publication.
- Identify the actual file version before using its timings.
- Show real state and make changes reversible.

## Accessibility & Inclusion

Support phone through wide desktop layouts, keyboard navigation, readable contrast and reduced motion; verify large-text reflow and both light and dark themes.
