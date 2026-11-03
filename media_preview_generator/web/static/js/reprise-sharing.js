/* Sharing page: the Plex publication switch and the API clients (apps such as Marquee) that read saved results. */
(() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const state = { secret: false };
    let messageTimer = null;
    const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
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
        $('rpWriteState').textContent = settings.publication_available
            ? (settings.writes_enabled ? 'Sending to Plex is on. Applying a change still needs your confirmation each time.' : 'Sending to Plex is off. Reprise cannot change any Plex markers.')
            : 'Sending to Plex is unavailable until a Plex connection is set up under Settings, Connections.';
        window.dispatchEvent(new CustomEvent('rp:settings', { detail: settings }));
    }
    $('rpWritesEnabled').addEventListener('change', async event => {
        const enabled = event.target.checked;
        if (enabled && !confirm('Allow reviewed Reprise changes to be sent to Plex? Nothing is applied yet.')) { event.target.checked = false; return; }
        event.target.disabled = true;
        try { showSettings(await api('/api/reprise/settings', { method: 'PATCH', body: { writes_enabled: enabled } })); }
        catch (error) { fail(error); try { showSettings(await api('/api/reprise/settings')); } catch (reloadError) { $('rpWriteState').textContent = reloadError.message; } }
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
                info.append(el('strong', client.name), document.createElement('br'), el('span', (client.scopes || []).join(', ') + (client.revoked ? ' · Revoked' : ''), 'rp-note')); row.append(info);
                if (!client.revoked) {
                    row.append(button('Replace key', async () => {
                        if (!confirm('Replace ' + client.name + "'s key? The old key will stop working.")) return;
                        try { showSecret(await api('/api/reprise/clients/' + encodeURIComponent(client.id) + '/rotate', { method: 'POST', body: {} })); await loadClients(); } catch (error) { fail(error); }
                    }));
                    row.append(button('Revoke', async () => {
                        if (!confirm('Revoke ' + client.name + "'s access?")) return;
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
    $('rpSecretCopy').addEventListener('click', async () => { try { await navigator.clipboard.writeText($('rpSecretValue').textContent); message('Key copied.'); } catch (error) { fail(error); } });
    $('rpSecretClose').addEventListener('click', () => {
        if (!confirm('Hide this key? It cannot be shown again.')) return;
        $('rpSecret').hidden = true; $('rpSecretValue').textContent = ''; state.secret = false; $('rpClientOpen').disabled = false;
    });
    window.addEventListener('beforeunload', event => { if (state.secret) { event.preventDefault(); event.returnValue = ''; } });

    api('/api/reprise/settings').then(showSettings).catch(error => { $('rpWriteState').textContent = error.message; });
    loadClients();
})();
