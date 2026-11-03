// The first-run tour (templates/_onboarding.html): a modal <dialog>, so the browser traps focus, makes the page behind it
// inert and closes it on Esc. Five steps, Skip on every one. Skip and Done are remembered on the server for the account
// (POST /api/account/onboarding), so the tour stays closed in every browser; Skip closes it even when that can't be
// saved. Esc or a click outside only closes it for this browser session. Anything with [data-tour-open] (Settings, the
// sidebar) opens it again from step 1.
(function () {
    'use strict';

    var dialog = document.getElementById('repriseTour');
    if (!dialog || typeof dialog.showModal !== 'function') return;

    var SKIPPED_KEY = 'repriseTourSkipped';
    var stateUrl = dialog.getAttribute('data-state-url');
    var steps = Array.prototype.slice.call(dialog.querySelectorAll('#tourSteps > .tour-step'));
    var body = document.getElementById('tourBody');
    var count = document.getElementById('tourCount');
    var error = document.getElementById('tourError');
    var dots = Array.prototype.slice.call(dialog.querySelectorAll('.tour-dots li'));
    var back = dialog.querySelector('[data-tour-back]');
    var next = dialog.querySelector('[data-tour-next]');
    var index = 0;
    var saving = false;

    function skippedThisSession() {
        try { return sessionStorage.getItem(SKIPPED_KEY) === '1'; } catch (e) { return false; }
    }

    function rememberSkip() {
        try { sessionStorage.setItem(SKIPPED_KEY, '1'); } catch (e) { /* storage blocked: it may open again on the next page */ }
    }

    function show(i) {
        index = Math.max(0, Math.min(steps.length - 1, i));
        // The live region announces the swapped-in step: its heading and text.
        body.replaceChildren(steps[index].cloneNode(true));
        body.scrollTop = 0;
        count.textContent = 'Step ' + (index + 1) + ' of ' + steps.length;
        dots.forEach(function (dot, n) { dot.className = n < index ? 'done' : n === index ? 'now' : ''; });
        back.disabled = index === 0;
        next.textContent = index === steps.length - 1 ? 'Done' : 'Next';
        error.hidden = true;
        // A button that was just disabled can't keep focus; the primary action is always a sensible place for it.
        if (!dialog.contains(document.activeElement) || document.activeElement.disabled) next.focus();
    }

    function open() {
        if (dialog.open) return;
        show(0);
        dialog.showModal();
    }

    function save(state) {
        if (saving) return Promise.resolve(false);
        saving = true;
        error.hidden = true;
        return fetch(stateUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ state: state }),
            credentials: 'same-origin'
        }).then(function (response) {
            if (!response.ok) throw new Error('HTTP ' + response.status);
            return true;
        }).catch(function () {
            error.hidden = false;
            return false;
        }).then(function (saved) {
            saving = false;
            return saved;
        });
    }

    function finish(state) {
        save(state).then(function (saved) {
            if (!saved) return;
            rememberSkip();
            dialog.close();
        });
    }

    // Skip is a choice, so it is remembered; but it must always work, so it closes the tour even if saving failed.
    dialog.querySelector('[data-tour-skip]').addEventListener('click', function () {
        save('dismissed').then(function () { dialog.close(); });
    });
    back.addEventListener('click', function () { show(index - 1); });
    next.addEventListener('click', function () {
        if (index === steps.length - 1) finish('completed');
        else show(index + 1);
    });

    // "Open Sharing" ends the tour too: the page is about to change, so the answer is sent without waiting for it.
    dialog.addEventListener('click', function (event) {
        var link = event.target.closest && event.target.closest('[data-tour-finish]');
        if (link) {
            rememberSkip();
            fetch(stateUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ state: 'completed' }),
                credentials: 'same-origin',
                keepalive: true
            });
        } else if (event.target === dialog) {
            dialog.close(); // a click on the dimmed page behind the card
        }
    });

    // The page behind a modal <dialog> is inert, but Tab from its last control would still escape to the browser's own
    // toolbar: wrap it round so keyboard focus stays on the card until it is closed.
    function focusable() {
        return Array.prototype.filter.call(dialog.querySelectorAll('a[href], button, input, [tabindex]'), function (el) {
            return !el.disabled && el.tabIndex >= 0 && el.getClientRects().length > 0;
        });
    }

    dialog.addEventListener('keydown', function (event) {
        if (event.altKey || event.ctrlKey || event.metaKey) return;
        if (event.key === 'Tab') {
            var items = focusable();
            var edge = event.shiftKey ? items[0] : items[items.length - 1];
            if (items.length && document.activeElement === edge) {
                event.preventDefault();
                (event.shiftKey ? items[items.length - 1] : items[0]).focus();
            }
        } else if (!event.shiftKey && event.key === 'ArrowRight' && index < steps.length - 1) {
            show(index + 1);
        } else if (!event.shiftKey && event.key === 'ArrowLeft' && index > 0) {
            show(index - 1);
        }
    });

    // Esc, Skip, the backdrop and a saved answer all end here: this browser won't offer the tour again this session.
    dialog.addEventListener('close', rememberSkip);

    document.addEventListener('click', function (event) {
        var opener = event.target.closest && event.target.closest('[data-tour-open]');
        if (opener) {
            event.preventDefault();
            open();
        }
    });

    if (dialog.getAttribute('data-auto') === 'true' && !skippedThisSession()) open();
})();
