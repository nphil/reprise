/* Setup guide: the checklist for a new install. It only shows what GET /api/reprise/setup-guide reports (the service reads the real
   configuration), and each step's button opens the place where that step is done. The page never decides that a step is done. */
(() => {
    'use strict';
    const ENDPOINT = '/api/reprise/setup-guide';
    const POLL_MS = 3000;
    const $ = id => document.getElementById(id);
    const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
    const state = { guide: null, rows: new Map(), seq: 0, pollTimer: null, busyTimer: null, announceTimer: null, completing: false, failed: false };

    const CHIPS = { done: ['Done', 'positive'], todo: ['Not done', null], blocked: ['Waiting', null], working: ['Working', 'busy'] };
    const CHANGED = { done: 'is done.', todo: 'is not done yet.', blocked: 'is waiting for an earlier step.', working: 'is in progress.' };
    const internal = href => typeof href === 'string' && href.startsWith('/') && !href.startsWith('//');
    const external = href => typeof href === 'string' && /^https?:\/\//i.test(href);
    const percentOf = step => {
        const value = step.state === 'working' && step.progress ? Number(step.progress.percent) : NaN;
        return Number.isFinite(value) ? Math.max(0, Math.min(100, Math.round(value))) : null;
    };

    async function api(path, options = {}) {
        const init = { ...options, headers: { Accept: 'application/json', ...(options.headers || {}) } };
        if (init.body && typeof init.body !== 'string') { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(init.body); }
        const response = await fetch(path, init);
        let data = null;
        try { data = await response.json(); } catch (_) { /* reported below */ }
        if (!response.ok || !data || typeof data !== 'object') throw new Error(`Request failed (${response.status}).`);
        return data;
    }

    /* One polite live region: what changed, in words, never a repeat of what is already on screen. */
    function announce(text) {
        const live = $('sgLive'); clearTimeout(state.announceTimer); live.textContent = '';
        if (text) state.announceTimer = setTimeout(() => { live.textContent = text; }, 60);
    }
    const progressSentence = guide => `${guide.required_done} of ${guide.required_total} required steps done`;

    /* ── one step ─────────────────────────────────────────────────────────────────────────────────────── */
    function marker(step) {
        const mark = el('span', null, 'sg-mark'); mark.setAttribute('aria-hidden', 'true');
        if (step.state === 'done') mark.append(el('i', null, 'bi bi-check-lg')); else mark.textContent = String(step.number);
        return mark;
    }
    function chip(step) {
        const [label, tone] = CHIPS[step.state] || CHIPS.todo, percent = percentOf(step);
        const node = el('span', percent == null ? label : `${label} ${percent}%`, 'rp-chip');
        if (tone) node.dataset.tone = tone;
        return node;
    }
    function bar(percent) {
        const track = el('div', null, 'rp-bar sg-bar'), fill = el('span');
        track.setAttribute('role', 'progressbar'); track.setAttribute('aria-label', 'Progress'); track.setAttribute('aria-valuemin', '0'); track.setAttribute('aria-valuemax', '100'); track.setAttribute('aria-valuenow', String(percent));
        fill.style.width = `${percent}%`; track.append(fill);
        return track;
    }
    /* The clipboard API only exists on https or localhost; on a plain-http address Reprise selects the text and uses the older copy command. */
    async function copy(field, button, label) {
        let copied = false;
        if (navigator.clipboard && window.isSecureContext) { try { await navigator.clipboard.writeText(field.value); copied = true; } catch (_) { copied = false; } }
        if (!copied) {
            field.focus(); field.select(); field.setSelectionRange(0, field.value.length);
            try { copied = document.execCommand('copy'); } catch (_) { copied = false; }
        }
        const note = button.closest('.sg-value').querySelector('.sg-copy-note'); clearTimeout(button.resetTimer);
        if (copied) {
            button.textContent = 'Copied'; note.textContent = ''; announce(`${label} copied.`);
            button.resetTimer = setTimeout(() => { button.textContent = 'Copy'; }, 2000);
        } else {
            note.textContent = 'Selected. Press Ctrl+C (or Cmd+C) to copy it.'; announce(`${label} is selected. Press Control C, or Command C, to copy it.`);
        }
    }
    function valueBlock(step, item, index) {
        const wrap = el('div', null, 'sg-value'), id = `sgValue-${step.id}-${index}`, row = el('div', null, 'sg-copy');
        const label = el('label', item.label); label.htmlFor = id;
        const field = el('input'); field.id = id; field.type = 'text'; field.readOnly = true; field.value = item.value; field.spellcheck = false; field.autocomplete = 'off'; field.dataset.focusKey = `value-${index}`;
        /* A field you only copy from: landing in it selects everything, so Ctrl+C (or Cmd+C) is one key away. */
        for (const type of ['focus', 'click']) field.addEventListener(type, () => field.select());
        const button = el('button', 'Copy', 'rp-btn'); button.type = 'button'; button.dataset.focusKey = `copy-${index}`; button.setAttribute('aria-label', `Copy ${item.label}`);
        button.addEventListener('click', () => copy(field, button, item.label));
        row.append(field, button); wrap.append(label, row, el('p', null, 'sg-copy-note'));
        return wrap;
    }
    function buildRow(step, current, primary) {
        const row = el('li', null, 'sg-step'), body = el('div', null, 'sg-body'), head = el('div', null, 'sg-head');
        row.dataset.stepId = step.id; row.dataset.state = step.state; row.dataset.required = String(step.required === true);
        if (current) row.setAttribute('aria-current', 'step');
        head.append(el('h3', step.title, 'sg-title'), chip(step)); body.append(head);
        if (step.reason) body.append(el('p', step.reason, 'sg-reason'));
        const percent = percentOf(step); if (percent != null) body.append(bar(percent));
        const details = (Array.isArray(step.details) ? step.details : []).filter(Boolean);
        if (details.length) { const list = el('ul', null, 'sg-details'); details.forEach(line => list.append(el('li', line))); body.append(list); }
        const values = (Array.isArray(step.values) ? step.values : []).filter(item => item && item.label && item.value);
        if (values.length) { const group = el('div', null, 'sg-values'); values.forEach((item, index) => group.append(valueBlock(step, item, index))); body.append(group); }
        if (step.help && step.help.label && external(step.help.href)) {
            const help = el('a', step.help.label, 'sg-help'), icon = el('i', null, 'bi bi-box-arrow-up-right');
            help.href = step.help.href; help.target = '_blank'; help.rel = 'noopener noreferrer'; help.dataset.focusKey = 'help'; icon.setAttribute('aria-hidden', 'true');
            help.append(icon, el('span', ' (opens in a new tab)', 'rp-sr'));
            body.append(help);
        }
        row.append(marker(step), body);
        if (step.action && step.action.label && internal(step.action.href) && step.state !== 'blocked') {
            const link = el('a', step.action.label, 'rp-btn sg-action' + (primary ? ' primary' : '')); link.href = step.action.href; link.dataset.focusKey = 'action';
            row.append(link);
        }
        return row;
    }
    /* A changed row is replaced, an unchanged one is left alone, so a poll never takes keyboard focus away from a button or a field. */
    function swap(oldRow, newRow) {
        const active = document.activeElement, key = active && oldRow.contains(active) ? active.dataset.focusKey : null;
        oldRow.replaceWith(newRow);
        if (!key) return;
        const target = newRow.querySelector(`[data-focus-key="${key}"]`) || $('sgRecheck');
        target.focus({ preventScroll: true });
    }
    function renderGroup(list, steps, currentId, primaryId) {
        list.querySelectorAll('.sg-skel').forEach(node => node.remove());
        steps.forEach((step, index) => {
            const signature = JSON.stringify([step, step.id === currentId, step.id === primaryId]);
            let entry = state.rows.get(step.id);
            if (!entry || entry.signature !== signature) {
                const node = buildRow(step, step.id === currentId, step.id === primaryId);
                if (entry && entry.node.parentNode === list) swap(entry.node, node);
                entry = { signature, node }; state.rows.set(step.id, entry);
            }
            if (list.children[index] !== entry.node) list.insertBefore(entry.node, list.children[index] || null);
        });
        while (list.children.length > steps.length) list.lastElementChild.remove();
    }

    /* ── the page ─────────────────────────────────────────────────────────────────────────────────────── */
    function render(guide) {
        const steps = guide.steps.slice().sort((a, b) => a.number - b.number);
        const required = steps.filter(step => step.required), optional = steps.filter(step => !step.required);
        const next = required.find(step => step.state !== 'done');
        const primary = next && next.action && next.state !== 'blocked' ? next.id : null;
        renderGroup($('sgRequired'), required, next ? next.id : null, primary);
        renderGroup($('sgOptional'), optional, null, null);
        if (optional.length) $('sgOptional').start = optional[0].number;
        const connect = steps.find(step => step.number === 1);
        $('sgProgress').textContent = progressSentence(guide);
        $('sgChecked').textContent = 'Checked ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); $('sgChecked').hidden = false;
        $('sgDone').hidden = !guide.all_required_done;
        $('sgDismissed').hidden = guide.state !== 'dismissed';
        $('sgSkip').hidden = !(guide.state === 'pending' && connect && connect.state === 'done' && !guide.all_required_done);
        $('sgGroups').removeAttribute('aria-busy');
    }
    function changes(previous, guide) {
        if (!previous) return [];
        const before = new Map(previous.steps.map(step => [step.id, step.state]));
        return guide.steps.filter(step => before.has(step.id) && before.get(step.id) !== step.state).map(step => `${step.title} ${CHANGED[step.state] || ''}`.trim());
    }
    /* All three required steps are done and the owner never said so: remember it once, without a word on the page. A failure is retried on the next check. */
    function remember(guide) {
        if (!guide.all_required_done || guide.state !== 'pending' || state.completing) return;
        state.completing = true;
        api(ENDPOINT, { method: 'POST', body: { state: 'completed' } }).then(saved => { if (state.guide === guide && saved.state) guide.state = saved.state; }, () => { state.completing = false; });
    }

    function showError(text, retry) {
        $('sgErrorText').textContent = text; $('sgRetry').hidden = !retry; $('sgError').hidden = false;
    }
    function setBusy(manual, on) {
        const button = $('sgRecheck'), label = button.querySelector('span');
        clearTimeout(state.busyTimer);
        if (!on) { button.removeAttribute('aria-busy'); label.textContent = 'Check again'; return; }
        if (manual) state.busyTimer = setTimeout(() => { button.setAttribute('aria-busy', 'true'); label.textContent = 'Checking…'; }, 250);
    }
    function schedule() {
        clearTimeout(state.pollTimer);
        if (state.guide && !document.hidden && state.guide.steps.some(step => step.state === 'working')) state.pollTimer = setTimeout(() => load(), POLL_MS);
    }
    async function load({ manual = false } = {}) {
        const mine = ++state.seq, inError = $('sgError').contains(document.activeElement), recovering = state.failed;
        clearTimeout(state.pollTimer); setBusy(manual, true);
        if (!state.guide) { $('sgGroups').hidden = false; $('sgProgress').parentElement.hidden = false; $('sgError').hidden = true; }
        try {
            const guide = await api(ENDPOINT);
            if (mine !== state.seq) return;
            if (!Array.isArray(guide.steps) || !guide.steps.length) throw new Error('The setup response was incomplete.');
            const moved = changes(state.guide, guide);
            if (state.guide && !state.guide.all_required_done && guide.all_required_done) moved.push('Everything Reprise needs is done.');
            state.guide = guide; state.failed = false; $('sgError').hidden = true; render(guide); remember(guide);
            if (inError) $('sgRecheck').focus({ preventScroll: true }); /* the button that was pressed has gone away: its neighbour takes focus */
            if (manual) announce(['Checked.', progressSentence(guide) + '.', ...moved].join(' '));
            else if (moved.length) announce(moved.join(' '));
            else if (recovering) announce('Your setup is up to date.');
        } catch (error) {
            if (mine !== state.seq) return;
            state.failed = true;
            if (!state.guide) { $('sgGroups').hidden = true; $('sgProgress').parentElement.hidden = true; }
            showError("Reprise couldn't check your setup just now.", true); announce("Reprise couldn't check your setup just now.");
            if (inError) $('sgRetry').focus({ preventScroll: true });
        } finally {
            if (mine === state.seq) { setBusy(manual, false); schedule(); }
        }
    }

    $('sgRecheck').addEventListener('click', () => load({ manual: true }));
    $('sgRetry').addEventListener('click', () => load({ manual: true }));
    /* "Skip for now" is the owner's choice to stop this opening by itself: save it, then go to the Library. If it cannot be saved, stay and say so. */
    $('sgSkip').addEventListener('click', async () => {
        const button = $('sgSkip'); button.disabled = true;
        try { await api(ENDPOINT, { method: 'POST', body: { state: 'dismissed' } }); window.location.assign('/reprise'); }
        catch (_) { button.disabled = false; showError("Reprise couldn't save that. Press Skip for now to try again.", false); announce("Reprise couldn't save that. Press Skip for now to try again."); }
    });
    /* What you did in another tab or another page shows up as soon as you are back. */
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') load(); });
    window.addEventListener('pageshow', event => { if (event.persisted) load(); });

    load();
})();
