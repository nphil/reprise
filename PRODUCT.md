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

The accepted product scope is recorded in this brief. Existing upstream results and tools are implementation evidence, not proof that the combined detector is safe or accurate on a particular library. Do not invent statistics, media or measured performance.

## Product Principles

- Keep the story when evidence is uncertain.
- Make the library and the next review easy to find.
- Separate analysis, review and publication.
- Identify the actual file version before using its timings.
- Show real state and make changes reversible.

## Accessibility & Inclusion

Support phone through wide desktop layouts, keyboard navigation, readable contrast and reduced motion; verify large-text reflow and both light and dark themes.
