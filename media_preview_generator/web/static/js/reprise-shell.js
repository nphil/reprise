/* Shared chrome: one light-dismiss popup at a time and the actual Plex publishing state. */
(() => {
    'use strict';
    const shade = document.querySelector('.rp-popup-shade');
    let opener = null;
    let popup = null;

    function closePopup(restore = true) {
        const previous = opener;
        const panel = popup;
        popup = opener = null;
        panel.hidden = true;
        shade.hidden = true;
        previous.setAttribute('aria-expanded', 'false');
        if (restore) previous.focus({ preventScroll: true });
    }

    document.querySelectorAll('[data-rp-popup]').forEach(trigger => {
        const panel = document.getElementById(trigger.getAttribute('aria-controls'));
        trigger.addEventListener('click', () => {
            if (opener === trigger) { closePopup(); return; }
            if (popup) closePopup(false);
            opener = trigger;
            popup = panel;
            panel.hidden = false;
            shade.hidden = false;
            trigger.setAttribute('aria-expanded', 'true');
            panel.dispatchEvent(new Event('rp:popup-open'));
            const first = panel.querySelector('[data-rp-initial-focus]') || panel.querySelector('a, button');
            first?.focus({ preventScroll: true });
        });
        panel.addEventListener('click', event => {
            if (event.target.closest('[data-rp-close-popup]')) closePopup();
        });
    });

    document.addEventListener('click', event => {
        if (!popup || popup.contains(event.target) || event.target.closest('[data-rp-popup]')) return;
        closePopup();
        event.preventDefault();
        event.stopPropagation();
    }, true);
    document.addEventListener('keydown', event => {
        if (event.key !== 'Escape' || !popup) return;
        event.preventDefault();
        event.stopPropagation();
        closePopup();
    }, true);
    document.addEventListener('focusin', event => {
        if (popup && !popup.contains(event.target) && !opener.contains(event.target)) closePopup(false);
    });
    document.addEventListener('show.bs.modal', () => { if (popup) closePopup(); });

    const theme = document.getElementById('rpTheme');
    if (theme) window.RepriseTheme.mountPicker(theme);

    const dock = document.querySelector('.rp-main-nav');
    if (dock) {
        const phone = matchMedia('(max-width: 639.98px)');
        function updateClearance() {
            if (phone.matches) document.body.style.setProperty('--rp-dock-clearance', `${dock.offsetHeight}px`);
            else document.body.style.removeProperty('--rp-dock-clearance');
        }
        new ResizeObserver(updateClearance).observe(dock);
        phone.addEventListener('change', updateClearance);
        updateClearance();
    }

    const settingsHeader = document.getElementById('settingsHeader');
    if (settingsHeader) {
        new ResizeObserver(() => {
            document.documentElement.style.setProperty('--rp-advanced-header-height', `${settingsHeader.offsetHeight}px`);
        }).observe(settingsHeader);
    }

    const chip = document.getElementById('rpSafety');
    if (!chip) return;
    const title = document.getElementById('rpSafetyTitle'), note = document.getElementById('rpSafetyNote');
    const icon = chip.querySelector('.bi');
    function showSafety(settings) {
        if (typeof settings?.writes_enabled !== 'boolean') {
            chip.dataset.state = 'unknown';
            icon.className = 'bi bi-shield-exclamation';
            title.textContent = 'Plex status unavailable';
            note.textContent = 'Open Sharing to check.';
        } else if (settings.writes_enabled) {
            chip.dataset.state = 'on';
            icon.className = 'bi bi-shield-exclamation';
            title.textContent = 'Sending to Plex is on';
            note.textContent = 'Your media files are not changed.';
        } else {
            chip.dataset.state = 'off';
            icon.className = 'bi bi-shield-check';
            title.textContent = 'Review only';
            note.textContent = 'Reprise is not changing Plex.';
        }
        chip.title = `${title.textContent}. ${note.textContent}`;
    }
    let changed = false;
    fetch('/api/reprise/settings', { headers: { Accept: 'application/json' } })
        .then(response => {
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return response.json();
        })
        .then(settings => { if (!changed) showSafety(settings); }, () => { if (!changed) showSafety(null); });
    window.addEventListener('rp:settings', event => {
        changed = true;
        showSafety(event.detail);
    });
})();
