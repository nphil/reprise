# Reprise interface

## Direction

Reprise is an operating tool, not a promotional page. The user confirmed a **library-first, code-led** interface. There is no approved visual comp and no comp-fidelity claim.

The shipped surface uses the existing Lucent v2 web adaptation, Rosé Pine dark/light roles and locally bundled Manrope. `media_preview_generator/web/static/css/reprise-tokens.css` is its token layer. Library browsing leads into episode review; queue, publication and client access remain separate sections.

## Review behavior

- Existing ranges start collapsed in native `details` elements. Their summaries expose action, type, exact time bounds, decision and lock state. New unsaved manual keep ranges open immediately.
- Range objects retain their own view while editing; typing and native select changes do not replace the focused field. Unsaved identity does not depend on array position.
- The selected episode is marked independently of focus, including after library reloads. Approval and saving remain separate from publication.
- The timeline is a visual overview. Exact timings and decisions remain available in the range list and evidence disclosure.
- The work queue distinguishes analysis, previews and library refresh. A pause request is labelled pending confirmation and offers Resume. Terminal states remove controls. Only refreshes observed active trigger a one-time library reload when they finish.

## Intentional, verified decisions

The master/detail review and lower administration sections scroll normally. They are not forced into a single desktop viewport. On narrow screens the library precedes review in document order; six range editors no longer all expand on arrival.

The rendered Impeccable detector found one advisory: repeated em dashes in the explicitly synthetic episode/server names. These are library-provided names, not authored interface prose, and are preserved rather than rewritten to satisfy the detector. No ignore comments were added.

## Verification boundary

The 2026-11-02 pass used the existing visible Chromium browser and a throwaway local application with generated color-bar video and tone audio. It did not inspect production media or expose client secrets.

Observed checks: real episode/reference playback; review edit saved and restored with the same range ID; the approved, locked 3–4 second keep preserved; native keyboard disclosure/select/input editing; two new unsaved ranges retaining identity after one is removed; filtered empty state; selected episode retained across reload; actual HTTP 202 refresh, pause request, resume, cancel and completion through a real localhost synthetic Plex protocol server. Completion caused one library fetch, with no additional fetch after three more queue refreshes.

At 1440, 390 and 320 CSS-pixel widths in both themes, document width stayed within the viewport. Doubled computed text sizes at those widths produced no overlapping visible text or horizontal overflow. Range summaries measured at least 44px high; expanded fields, buttons and checkbox labels measured at least 44px high. These are browser-emulated widths, **not physical-phone evidence**.

Axe 4.13.0 found no WCAG A/AA rule violations on the exercised review states in dark and light. Its remaining manual check is video captions: the synthetic fixture has no dialogue, and this check does not establish caption support for arbitrary library media. No full accessibility, device-performance or hardware-tier certification is claimed.

PNG captures and the verification summary are local artifacts in `.impeccable/review/`; they are not published. They show the actual implementation, not an approved comp. An independent read-only finish reviewer inspected all 13 captures and returned a SHIP disposition.
