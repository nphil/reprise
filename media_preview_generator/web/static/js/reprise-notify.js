/* Sharing page: the "Phone alerts" panel (message switch and quiet hours). It stays hidden when Reprise has no automatic work to message about. */
(() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const panel = $('rpNotifyPanel');
    if (!panel) return;
    const URL = '/api/reprise/notifications';
    const REASONS = { quiet_hours: 'held back until quiet hours end', limit: 'held back by the three-an-hour limit' };
    const fields = [$('rpNotifyOn'), $('rpQuietFrom'), $('rpQuietUntil')];
    let saving = false;

    async function api(options = {}) {
        const init = { ...options, headers: {} };
        if (init.body) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(init.body); }
        const response = await fetch(URL, init);
        let data; try { data = await response.json(); } catch (_) { throw new Error(`Request failed (${response.status}).`); }
        if (!response.ok) throw new Error(data.error || data.message || `Request failed (${response.status}).`);
        return data;
    }
    function ago(iso) {
        const then = Date.parse(iso);
        if (!iso || Number.isNaN(then)) return null;
        const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
        if (seconds < 60) return 'just now';
        const [size, name] = [[86400, 'day'], [3600, 'hour'], [60, 'minute']].find(([unit]) => seconds >= unit);
        const count = Math.floor(seconds / size);
        return `${count} ${name}${count === 1 ? '' : 's'} ago`;
    }
    function status(text, error = false) {
        const note = $('rpNotifyStatus'); note.textContent = text; note.classList.toggle('error', error);
    }
    function show(data) {
        panel.hidden = false;
        $('rpNotifyOn').checked = !!data.enabled;
        $('rpQuietFrom').value = data.quiet_from; $('rpQuietUntil').value = data.quiet_until;
        $('rpQuietFrom').disabled = $('rpQuietUntil').disabled = !data.enabled;
        const parts = [];
        if (!data.enabled) parts.push('Phone alerts are off.');
        else if (data.last) parts.push(`Last message: ${data.last.title}, ${ago(data.last.at) || 'a while ago'}.`);
        else parts.push('Nothing sent yet.');
        if (data.enabled && data.waiting) parts.push(`${data.waiting} waiting, ${REASONS[data.waiting_reason] || 'sent on the next pass'}.`);
        status(parts.join(' '));
    }
    async function save(values) {
        if (saving) return;
        saving = true; fields.forEach(f => { f.disabled = true; });
        try { show(await api({ method: 'PATCH', body: values })); }
        catch (error) { status((error && error.message) || 'Something went wrong.', true); await load(true); }
        finally { saving = false; fields.forEach(f => { f.disabled = false; }); $('rpQuietFrom').disabled = $('rpQuietUntil').disabled = !$('rpNotifyOn').checked; }
    }
    async function load(keepMessage) {
        try { const data = await api(); const note = $('rpNotifyStatus').textContent; show(data); if (keepMessage) status(note, true); }
        catch (_) { /* no automatic work here: the panel stays hidden */ }
    }
    $('rpNotifyOn').addEventListener('change', event => save({ enabled: event.target.checked }));
    for (const [id, key] of [['rpQuietFrom', 'quiet_from'], ['rpQuietUntil', 'quiet_until']]) {
        $(id).addEventListener('change', event => { if (event.target.value) save({ [key]: event.target.value }); });
    }
    load();
})();
