/* Sharing page: the Plex publication switch and the API clients (apps such as Marquee) that read saved results. */
(() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const state = { secret: false };
    let messageTimer = null;
    const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
    const SCOPE_WORDS = { 'results:read': 'Read results', 'jobs:read': 'See job status', 'analysis:request': 'Ask for analysis' };
    // The in-app dialog (base.html) looks like the rest of Reprise; a native prompt is only the fallback.
    const ask = (text, options) => (window.appConfirm ? window.appConfirm(text, options) : Promise.resolve(window.confirm(text)));
    const button = (text, fn, cls = 'rp-btn') => { const b = el('button', text, cls); b.type = 'button'; b.addEventListener('click', fn); return b; };
    function message(text, error = false) {
        const n = $('rpMessage'); clearTimeout(messageTimer); n.textContent = text; n.classList.toggle('error', error); n.hidden = !text;
        if (text && !error) messageTimer = setTimeout(() => { n.hidden = true; }, 9000);
    }
    async function api(path, options = {}) {
        const init = { ...options, headers: { ...(options.headers || {}) } };
        if (init.body && typeof init.body !== 'string') { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(init.body); }
        const response = await fetch(path, init);
        if (response.status === 204) return {};
        let data; try { data = await response.json(); } catch (_) { throw new Error(`Request failed (${response.status}).`); }
        if (!response.ok) throw new Error(data.error || data.message || `Request failed (${response.status}).`);
        return data;
    }
    const fail = error => message((error && error.message) || 'Something went wrong.', true);

    function showSettings(settings) {
        $('rpWritesEnabled').checked = !!settings.writes_enabled; $('rpWritesEnabled').disabled = !settings.publication_available;
        const note = $('rpWriteState');
        note.textContent = settings.publication_available
            ? (settings.writes_enabled ? 'Sending to Plex is on. Applying a change still needs your confirmation each time.' : 'Sending to Plex is off. Reprise cannot change any Plex markers.')
            : 'Sending to Plex is not available. It needs a Plex connection, which is set up in Connections.';
        if (!settings.publication_available) { const link = el('a', 'Open Connections'); link.href = '/servers'; note.append(link); }
        showAutopilot(settings.autopilot);
        window.dispatchEvent(new CustomEvent('rp:settings', { detail: settings }));
    }
    $('rpWritesEnabled').addEventListener('change', async event => {
        const enabled = event.target.checked;
        if (enabled && !await ask('Reprise will be able to send reviewed changes to Plex. Nothing is sent yet: each change still needs your confirmation.', { title: 'Allow sending to Plex?', confirmText: 'Allow', variant: 'warning' })) { event.target.checked = false; return; }
        event.target.disabled = true;
        try { showSettings(await api('/api/reprise/settings', { method: 'PATCH', body: { writes_enabled: enabled } })); }
        catch (error) {
            fail(error);
            try { showSettings(await api('/api/reprise/settings')); } catch (reloadError) { $('rpWriteState').textContent = reloadError.message; return; }
            $('rpWriteState').textContent = error.message;
        }
    });

    // Automatic work: the three switches of the background job and a line about what it is doing. The panel stays hidden when Reprise has no such job.
    const AUTO_SWITCHES = { rpAutoSend: 'auto_send', rpBackfill: 'backfill', rpAlerts: 'alerts' };
    function ago(iso) {
        const then = Date.parse(iso);
        if (!iso || Number.isNaN(then)) return null;
        const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
        if (seconds < 60) return 'just now';
        const [size, name] = [[86400, 'day'], [3600, 'hour'], [60, 'minute']].find(([unit]) => seconds >= unit);
        const count = Math.floor(seconds / size);
        return `${count} ${name}${count === 1 ? '' : 's'} ago`;
    }
    function showAutopilot(autopilot) {
        $('rpAutoPanel').hidden = !autopilot;
        if (!autopilot) return;
        for (const [id, key] of Object.entries(AUTO_SWITCHES)) $(id).checked = !!autopilot[key];
        const status = autopilot.status || {};
        const blocked = $('rpAutoSendNote');
        blocked.textContent = status.auto_send_blocked || ''; blocked.hidden = !status.auto_send_blocked;
        const last = status.last_alert;
        const lastWhen = last && ago(last.at);
        $('rpAutoStatus').textContent = `${status.queue_length || 0} waiting in the queue · Last check for new files: ${ago(status.last_reconcile) || 'not yet'} · Last alert: ${last ? [last.title, lastWhen].filter(Boolean).join(', ') : 'none yet'}`;
    }
    for (const [id, key] of Object.entries(AUTO_SWITCHES)) {
        $(id).addEventListener('change', async event => {
            const box = event.target, wanted = box.checked;
            box.disabled = true;
            try { showSettings(await api('/api/reprise/settings', { method: 'PATCH', body: { [key]: wanted } })); }
            catch (error) {
                box.checked = !wanted; fail(error);
                try { showSettings(await api('/api/reprise/settings')); } catch (_) { /* the switch already went back to what it was */ }
            } finally { box.disabled = false; }
        });
    }
    $('rpTestAlert').addEventListener('click', async event => {
        const send = event.currentTarget, result = $('rpAlertResult');
        send.disabled = true; result.textContent = 'Sending a test alert…';
        try {
            result.textContent = (await api('/api/reprise/alerts/test', { method: 'POST' })).message || 'The test alert was sent.';
            try { showSettings(await api('/api/reprise/settings')); } catch (_) { /* the status line just keeps its old numbers */ }
        } catch (error) { result.textContent = (error && error.message) || 'The test alert could not be sent.'; }
        finally { send.disabled = false; }
    });

    function showSecret(result) {
        if (!result.secret) throw new Error('The server did not return the new key.');
        $('rpSecretValue').textContent = result.secret; $('rpSecret').hidden = false; state.secret = true; $('rpClientOpen').disabled = true;
    }
    async function loadClients() {
        const root = $('rpClients');
        try {
            const data = await api('/api/reprise/clients'), clients = Array.isArray(data) ? data : data.clients;
            if (!Array.isArray(clients)) throw new Error('The apps response was incomplete.');
            root.replaceChildren(); root.className = '';
            if (!clients.length) { root.className = 'rp-state'; root.textContent = 'No apps have access yet.'; return; }
            clients.forEach(client => {
                const row = el('div', null, 'rp-client'), info = el('div');
                info.append(el('strong', client.name), document.createElement('br'), el('span', (client.scopes || []).map(scope => SCOPE_WORDS[scope] || scope).join(', ') + (client.revoked ? ' · Revoked' : ''), 'rp-note')); row.append(info);
                if (!client.revoked) {
                    row.append(button('Replace key', async () => {
                        if (!await ask("The old key for " + client.name + ' stops working straight away. Anything using it will need the new key.', { title: 'Replace key?', confirmText: 'Replace key', variant: 'warning' })) return;
                        try { showSecret(await api('/api/reprise/clients/' + encodeURIComponent(client.id) + '/rotate', { method: 'POST', body: {} })); await loadClients(); } catch (error) { fail(error); }
                    }));
                    row.append(button('Revoke', async () => {
                        if (!await ask(client.name + ' will no longer be able to read your results.', { title: 'Revoke access?', confirmText: 'Revoke' })) return;
                        try { await api('/api/reprise/clients/' + encodeURIComponent(client.id), { method: 'DELETE' }); await loadClients(); } catch (error) { fail(error); }
                    }, 'rp-btn danger'));
                }
                root.append(row);
            });
        } catch (error) { root.className = 'rp-state'; root.textContent = error.message; }
    }
    $('rpClients').addEventListener('click', event => {
        if (state.secret && event.target.closest('button')) { event.preventDefault(); event.stopImmediatePropagation(); message('Copy and close the current key first.', true); }
    }, true);
    $('rpClientOpen').addEventListener('click', () => { $('rpClientForm').hidden = false; $('rpClientForm').elements.name.focus(); });
    $('rpClientCancel').addEventListener('click', () => { $('rpClientForm').hidden = true; });
    $('rpClientForm').addEventListener('submit', async event => {
        event.preventDefault(); const form = event.currentTarget, scopes = ['results:read', 'jobs:read']; if (form.elements.analysis.checked) scopes.push('analysis:request');
        try { const result = await api('/api/reprise/clients', { method: 'POST', body: { name: form.elements.name.value.trim(), scopes } }); showSecret(result); form.reset(); form.hidden = true; await loadClients(); } catch (error) { fail(error); }
    });
    // The clipboard API only exists on https or localhost; on a plain-http address Reprise selects the key and uses the older copy command.
    async function copyKey() {
        const text = $('rpSecretValue').textContent;
        if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
        const range = document.createRange(); range.selectNodeContents($('rpSecretValue'));
        const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
        return document.execCommand('copy');
    }
    $('rpSecretCopy').addEventListener('click', async () => {
        try { message((await copyKey()) ? 'Key copied.' : 'The key is selected. Press Ctrl+C (or Cmd+C) to copy it.'); } catch (error) { fail(error); }
    });
    $('rpSecretClose').addEventListener('click', async () => {
        if (!await ask('It cannot be shown again, so make sure you have copied it first.', { title: 'Hide this key?', confirmText: 'Hide key', variant: 'warning' })) return;
        $('rpSecret').hidden = true; $('rpSecretValue').textContent = ''; state.secret = false; $('rpClientOpen').disabled = false;
    });
    window.addEventListener('beforeunload', event => { if (state.secret) { event.preventDefault(); event.returnValue = ''; } });

    api('/api/reprise/settings').then(showSettings).catch(error => { $('rpWriteState').textContent = error.message; });
    loadClients();
})();
