/* Shared shell behaviour (base.html): sidebar open/collapse and the always-visible "can Reprise change Plex?" chip. */
(() => {
    'use strict';
    const root = document.documentElement, body = document.body;
    const toggle = document.getElementById('rpRailToggle');
    const narrow = window.matchMedia('(max-width: 899.98px)');

    function syncToggle() {
        if (!toggle) return;
        const expanded = narrow.matches ? body.classList.contains('rp-rail-open') : !root.classList.contains('rp-rail-collapsed');
        const icon = toggle.querySelector('i');
        toggle.setAttribute('aria-expanded', String(expanded));
        toggle.setAttribute('aria-label', narrow.matches ? (expanded ? 'Close menu' : 'Open menu') : (expanded ? 'Collapse sidebar' : 'Expand sidebar'));
        toggle.title = toggle.getAttribute('aria-label');
        if (icon) icon.className = narrow.matches ? (expanded ? 'bi bi-x-lg' : 'bi bi-list') : (expanded ? 'bi bi-layout-sidebar-inset' : 'bi bi-layout-sidebar');
    }
    if (toggle) {
        toggle.addEventListener('click', () => {
            if (narrow.matches) {
                body.classList.toggle('rp-rail-open');
            } else {
                const collapsed = root.classList.toggle('rp-rail-collapsed');
                try { localStorage.setItem('rpRailCollapsed', collapsed ? '1' : '0'); } catch (_) { /* best effort */ }
            }
            syncToggle();
        });
        narrow.addEventListener('change', () => { body.classList.remove('rp-rail-open'); syncToggle(); });
        document.addEventListener('keydown', event => {
            if (event.key === 'Escape' && narrow.matches && body.classList.contains('rp-rail-open')) { body.classList.remove('rp-rail-open'); syncToggle(); toggle.focus(); }
        });
        syncToggle();
    }
    // Icon-only rows need a name on hover when the sidebar is collapsed.
    document.querySelectorAll('.rp-rail-nav a, .rp-rail-btn').forEach(control => {
        const label = control.querySelector('span:not(.badge)');
        if (label && !control.title) control.title = label.textContent.trim();
    });

    const chip = document.getElementById('rpSafety');
    function showSafety(settings) {
        if (!chip) return;
        const title = document.getElementById('rpSafetyTitle'), note = document.getElementById('rpSafetyNote');
        if (!settings) {
            chip.dataset.state = 'unknown'; title.textContent = 'Plex status unavailable'; note.textContent = 'Open Sharing to check.';
        } else if (settings.writes_enabled) {
            chip.dataset.state = 'on'; title.textContent = 'Plex publishing is on'; note.textContent = 'Each change still needs your confirmation.';
        } else {
            chip.dataset.state = 'off'; title.textContent = 'Review only'; note.textContent = 'Reprise is not changing Plex.';
        }
        // The words are hidden while the sidebar is collapsed to icons: keep them as the tooltip.
        chip.title = title.textContent + '. ' + note.textContent;
    }
    if (chip) {
        fetch('/api/reprise/settings', { headers: { Accept: 'application/json' } })
            .then(response => response.ok ? response.json() : Promise.reject(new Error('status ' + response.status)))
            .then(showSafety, () => showSafety(null));
        window.addEventListener('rp:settings', event => showSafety(event.detail));
    }
})();

// Settings tabs: when they do not all fit (a phone, a narrow tablet) the strip scrolls sideways. Scroll only as far as it takes
// to show the current tab whole (never to centre it, which hid the first tabs on a tablet), and fade whichever edge is cut off.
(() => {
    const strip = document.querySelector('.rp-subnav-scroll');
    if (!strip) return;
    const edge = 24;
    function fade() {
        const more = strip.scrollWidth - strip.clientWidth;
        const start = strip.scrollLeft > 2, end = more - strip.scrollLeft > 2;
        if (start && end) strip.dataset.fade = 'both';
        else if (start) strip.dataset.fade = 'start';
        else if (end) strip.dataset.fade = 'end';
        else delete strip.dataset.fade;
    }
    function showCurrent() {
        const current = strip.querySelector('a.active');
        if (!current) return;
        const left = current.offsetLeft, right = left + current.offsetWidth;
        if (left < strip.scrollLeft + edge) strip.scrollLeft = Math.max(0, left - edge);
        else if (right > strip.scrollLeft + strip.clientWidth - edge) strip.scrollLeft = right - strip.clientWidth + edge;
    }
    showCurrent();
    fade();
    strip.addEventListener('scroll', fade, { passive: true });
    if ('ResizeObserver' in window) new ResizeObserver(() => { showCurrent(); fade(); }).observe(strip);
    else window.addEventListener('resize', fade);
})();
