/* Library page: browse episodes, analyze them, review what Reprise found. Publication switches and API clients live on
   the Sharing page (reprise-sharing.js). Every list/status shown here comes from /api/reprise/*. */
(() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const state = {
        items: [], selected: new Set(), item: null, result: null, edits: [], cursor: null, request: 0, dirty: false,
        changes: [], libraries: null, settings: null, jobs: [], jobMedia: new Map(), seenJobs: new Map(), activityHidden: false, showOlder: false,
    };
    const activeRefreshes = new Set(), rangeViews = new WeakMap();
    let reviewRequest = 0, referenceRequest = 0, queueTimer = null, messageTimer = null, uid = 0;
    const narrow = window.matchMedia('(max-width: 899.98px)');

    /* ── small helpers ─────────────────────────────────────────────────────────────────────────────────── */
    const terminalJob = status => ['completed', 'failed', 'cancelled'].includes(status);
    const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
    const button = (text, fn, cls = 'rp-btn') => { const b = el('button', text, cls); b.type = 'button'; b.addEventListener('click', fn); return b; };
    const clear = node => node.replaceChildren();
    const plural = (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`;
    const joinAnd = parts => { const list = parts.filter(Boolean); return list.length > 1 ? list.slice(0, -1).join(', ') + ' and ' + list[list.length - 1] : list[0] || ''; };
    /* A suggestion is a range someone else proposed (Plex or an online database) that Reprise did not confirm. */
    const isSuggestion = r => r.origin === 'plex' || r.origin === 'online';
    const suggesters = r => (Array.isArray(r.suggested_by) && r.suggested_by.length ? r.suggested_by : ['Plex']);
    const suggesterNames = ranges => [...new Set(ranges.flatMap(suggesters))];
    const suggestionBadge = r => suggesters(r).join(' + ') + ' suggestion';
    const option = (value, label) => { const n = el('option', label); n.value = value; return n; };
    const pad = n => String(n).padStart(2, '0');
    /* Exact time as m:ss.mmm (or h:mm:ss.mmm): the unit of every range, so it is what the editors show. */
    function ms(value) {
        const n = Math.max(0, Math.floor(Number(value) || 0)), h = Math.floor(n / 3600000), m = Math.floor(n / 60000) % 60, s = Math.floor(n / 1000) % 60;
        return `${h ? `${h}:` : ''}${h ? pad(m) : m}:${pad(s)}.${String(n % 1000).padStart(3, '0')}`;
    }
    const minutes = value => { const n = Number(value); return Number.isFinite(n) && n > 0 ? `${Math.max(1, Math.round(n / 60000))} min` : ''; };
    const seasonEpisode = item => item.season != null && item.episode != null ? `S${item.season} E${item.episode}` : '';
    const episodeName = item => item.title || 'Untitled episode';
    const longName = item => [item.show_title, seasonEpisode(item), episodeName(item)].filter(Boolean).join(' · ');
    const context = item => [item.show_title, seasonEpisode(item), minutes(item.duration_ms)].filter(Boolean).join(' · ');
    const kindLabel = kind => ({ intro: 'intro', credits: 'credits', recap: 'recap', preview: 'next-episode preview' }[kind] || kind || 'section');
    const decisionLabel = status => ({ approved: 'Approved', rejected: 'Rejected' }[status] || 'Needs review');
    const isCode = text => typeof text === 'string' && /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/.test(text);

    function message(text, error = false) {
        const n = $('rpMessage'); clearTimeout(messageTimer);
        n.textContent = text; n.classList.toggle('error', error); n.hidden = !text;
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

    /* Reasons arrive as sentences or as snake_case codes; show people the plain meaning and keep the raw text for Details. */
    const REASONS = [
        [/^unapproved_or_unverified$/, 'Nothing is approved yet, so nothing will be skipped.'],
        [/^Media analysis failed$/i, "Reprise couldn't read this file well enough to compare it."],
        [/^Media is unsupported$/i, "Reprise can't analyze this kind of file yet."],
        [/^Analysis cancelled$/i, 'The analysis was cancelled before it finished.'],
        [/^Analysis failed or media changed$/i, 'The analysis failed, or the file changed while it was running.'],
        [/^Analysis did not complete$/i, 'The analysis did not finish.'],
        [/^Binding changed; reanalysis required$/i, 'This file changed. Analyze it again.'],
        [/^Plex mapping or local media has changed or is unavailable$/i, "Reprise can't find this file where Plex says it is. Check that the media folder is still available, then refresh the library."],
        [/^Reference media is stale or unavailable$/i, 'An episode used for comparison has changed or is unavailable. Analyze again.'],
        [/^Reference unsupported/i, "Another episode couldn't be used for comparison because it uses a different video or audio format."],
        [/^No exact registered Plex playback identity$/i, "Reprise hasn't read this episode from Plex yet. Refresh the library."],
        [/^Playback identity is ambiguous$/i, 'Plex lists this file more than once, so Reprise cannot tell which copy to use.'],
    ];
    function plainReason(reason) {
        const text = typeof reason === 'string' ? reason : (reason && (reason.message || reason.code)) || '';
        for (const [pattern, plain] of REASONS) if (pattern.test(text)) return plain;
        return isCode(text) ? 'Reprise needs attention here. Open Details for the technical reason.' : text;
    }
    const rawReasons = result => (result.reasons || []).filter(reason => reason !== 'unapproved_or_unverified');

    /* ── status of an episode (list rows and review header) ─────────────────────────────────────────────── */
    function jobMediaIds(job) { return job.media_version_ids || state.jobMedia.get(job.id) || null; }
    function activeJobFor(id) {
        return state.jobs.find(job => job.operation === 'analysis' && !terminalJob(job.status) && (jobMediaIds(job) || []).includes(id)) || null;
    }
    function episodeStatus(item) {
        const job = activeJobFor(item.id);
        if (job) {
            const pct = typeof job.progress?.percent === 'number' && job.status === 'running' ? ` ${Math.round(job.progress.percent)}%` : '';
            return { label: job.status === 'running' ? `Analyzing${pct}` : 'Waiting to analyze', tone: 'busy' };
        }
        if (item.freshness === 'stale') return { label: 'File changed', tone: 'warning' };
        switch (item.analysis_status) {
            case 'complete': {
                const r = item.ranges;
                if (r && typeof r === 'object') {
                    if (!(r.skip + r.keep)) return { label: 'Nothing found', tone: 'positive' };
                    if (!r.skip) return { label: 'Nothing to skip', tone: 'positive' };
                    const review = typeof r.to_review === 'number' ? r.to_review : r.proposed, suggested = r.suggested || 0;
                    const who = r.suggested_by, plexOnly = Array.isArray(who) && who.length === 1 && who[0] === 'Plex';
                    const shown = [review ? `${review} to review` : '', suggested ? plural(suggested, plexOnly ? 'Plex suggestion' : 'suggestion') : ''].filter(Boolean);
                    if (shown.length) return { label: shown.join(' · '), tone: 'accent' };
                    return { label: 'Reviewed', tone: 'positive' };
                }
                return { label: 'Analyzed', tone: 'positive' };
            }
            case 'running': return { label: 'Analyzing', tone: 'busy' };
            case 'failed': return { label: 'Analysis failed', tone: 'danger' };
            case 'cancelled': return { label: 'Cancelled', tone: 'warning' };
            case 'unsupported': return { label: 'Not supported', tone: 'warning' };
            default: return { label: 'Not analyzed', tone: null };
        }
    }
    function paintChip(chip, status) { chip.textContent = status.label; if (status.tone) chip.dataset.tone = status.tone; else delete chip.dataset.tone; }

    /* ── library list ───────────────────────────────────────────────────────────────────────────────────── */
    function selectOptions(select, values, all) {
        const previous = select.value; select.replaceChildren(option('', all));
        values.forEach(v => select.append(option(String(v), String(v))));
        if (values.some(v => String(v) === previous)) select.value = previous;
    }
    function showSkeleton() { const box = $('rpLibraryList'); clear(box); const s = el('div', null, 'rp-skeleton'); s.setAttribute('aria-hidden', 'true'); for (let i = 0; i < 6; i++) s.append(el('i')); box.append(s); }
    function libraryQuery(extra = {}) {
        const q = new URLSearchParams({ limit: '100', ...extra });
        if ($('rpSearch').value.trim()) q.set('search', $('rpSearch').value.trim());
        if ($('rpShow').value) q.set('show', $('rpShow').value);
        if ($('rpSeason').value) q.set('season', $('rpSeason').value);
        return q;
    }
    function applyFilters(result) {
        if (Array.isArray(result.shows)) selectOptions($('rpShow'), result.shows, 'All shows');
        if (Array.isArray(result.seasons)) selectOptions($('rpSeason'), result.seasons, 'All seasons');
    }
    async function loadLibrary(append = false) {
        const seq = ++state.request;
        if (!append) { showSkeleton(); $('rpLibraryState').hidden = true; }
        try {
            const q = libraryQuery(); if (append && state.cursor) q.set('cursor', state.cursor);
            const result = await api('/api/reprise/media?' + q); if (seq !== state.request) return;
            if (!Array.isArray(result.items)) throw new Error('The library response was incomplete.');
            state.items = append ? state.items.concat(result.items) : result.items; state.cursor = result.next_cursor || null;
            applyFilters(result); renderLibrary(); renderReferences();
        } catch (error) { if (seq === state.request) renderLibraryError(error); }
    }
    /* After a job finishes, re-read the pages already on screen so every status is current without losing the user's place. */
    async function reloadLoaded() {
        const seq = ++state.request, want = Math.max(100, state.items.length), box = $('rpLibraryList'), scroll = box.scrollTop;
        try {
            let items = [], cursor = null, last = null;
            do {
                const q = libraryQuery(); if (cursor) q.set('cursor', cursor);
                last = await api('/api/reprise/media?' + q); if (seq !== state.request) return;
                items = items.concat(last.items || []); cursor = last.next_cursor || null;
            } while (cursor && items.length < want);
            state.items = items; state.cursor = cursor; applyFilters(last);
            if (state.item) state.item = items.find(i => i.id === state.item.id) || state.item;
            renderLibrary(); box.scrollTop = scroll;
        } catch (_) { /* the next poll tries again */ }
    }
    function renderLibraryError(error) {
        const box = $('rpLibraryState'); clear($('rpLibraryList')); box.hidden = false; box.replaceChildren(el('span', error.message || 'The library could not be loaded.'), document.createElement('br'), button('Try again', () => loadLibrary()));
        $('rpMore').hidden = true;
    }
    function renderEmpty(filtered) {
        const box = $('rpLibraryState'); clear(box); box.hidden = false;
        if (filtered) { box.append(el('span', 'No episodes match these filters.'), document.createElement('br'), button('Clear filters', () => { $('rpSearch').value = ''; $('rpShow').value = ''; $('rpSeason').value = ''; loadLibrary(); })); return; }
        box.append(el('strong', 'No episodes yet'), document.createElement('br'), el('span', 'Reprise lists episodes after it reads them from Plex.'));
        const slot = el('div'); box.append(slot);
        const offer = libs => {
            if (libs.length) slot.append(button('Refresh library', startRefresh, 'rp-btn primary'));
            else { slot.append(el('p', 'No Plex library is connected yet.', 'rp-note')); const a = el('a', 'Open Connections', 'rp-btn primary'); a.href = '/servers'; slot.append(a); }
        };
        loadLibraries().then(offer, () => offer([{}]));
    }
    function renderLibrary() {
        const target = $('rpLibraryList'); clear(target);
        const filtered = !!($('rpSearch').value.trim() || $('rpShow').value || $('rpSeason').value);
        $('rpLibraryState').hidden = state.items.length > 0; $('rpLibraryHint').hidden = !state.items.length;
        if (!state.items.length) renderEmpty(filtered);
        for (const item of state.items) {
            const row = el('div', null, 'rp-episode'), label = el('label');
            const check = el('input'); check.type = 'checkbox'; check.checked = state.selected.has(item.id); check.setAttribute('aria-label', `Select ${longName(item)} for analysis`);
            check.addEventListener('change', () => { check.checked ? state.selected.add(item.id) : state.selected.delete(item.id); renderSelection(); });
            const open = button('', () => openItem(item), 'rp-episode-open'); open.dataset.mediaId = item.id;
            if (state.item?.id === item.id) open.setAttribute('aria-current', 'true');
            const copy = el('span', null, 'rp-episode-copy'); copy.append(el('span', episodeName(item), 'rp-episode-name'), el('span', context(item), 'rp-episode-context'));
            const chip = el('span', null, 'rp-chip'); paintChip(chip, episodeStatus(item));
            open.setAttribute('aria-label', `${longName(item)}, ${chip.textContent}`); open.append(copy, chip);
            label.append(check); row.append(label, open); target.append(row);
        }
        $('rpMore').hidden = !state.cursor; renderSelection();
    }
    function paintStatuses() {
        const byId = new Map(state.items.map(i => [i.id, i]));
        document.querySelectorAll('.rp-episode-open').forEach(open => {
            const item = byId.get(open.dataset.mediaId); if (!item) return;
            const chip = open.querySelector('.rp-chip'); paintChip(chip, episodeStatus(item));
            open.setAttribute('aria-label', `${longName(item)}, ${chip.textContent}`);
        });
    }
    function renderSelection() {
        const n = state.selected.size; $('rpSelectBar').hidden = !n; $('rpLibraryHint').hidden = !!n || !state.items.length;
        $('rpSelectCount').textContent = `${plural(n, 'episode')} selected`; $('rpAnalyze').textContent = n === 1 ? 'Analyze episode' : `Analyze ${n} episodes`;
    }
    async function loadLibraries() {
        if (state.libraries) return state.libraries;
        const data = await api('/api/libraries');
        return (state.libraries = (data.libraries || []).filter(l => l.server_id && l.server_type === 'plex'));
    }

    /* ── analysis ───────────────────────────────────────────────────────────────────────────────────────── */
    async function analyze(ids) {
        const usable = ids.filter(id => { const item = state.items.find(i => i.id === id) || (state.item?.id === id ? state.item : null); return !item || item.freshness !== 'stale'; });
        if (!usable.length) return message('These files have changed since Reprise last read them. Refresh the library, then analyze them again.', true);
        try {
            const job = await api('/api/reprise/analyses', { method: 'POST', body: { media_version_ids: usable } });
            if (job.id) state.jobMedia.set(job.id, job.media_version_ids || usable);
            usable.forEach(id => state.selected.delete(id));
            const skipped = ids.length - usable.length;
            message(`Analyzing ${plural(usable.length, 'episode')}. You can leave this page; results appear here when it finishes.` + (skipped ? ` ${plural(skipped, 'file')} changed and ${skipped === 1 ? 'was' : 'were'} skipped.` : ''));
            await loadQueue(); renderLibrary(); if (state.item) renderOutcome();
        } catch (error) {
            message(/Invalid request|Request failed \(400\)/.test(error.message) ? "Reprise couldn't start the analysis. The file may have changed or moved: refresh the library and try again." : error.message, true);
        }
    }

    /* ── opening an episode ─────────────────────────────────────────────────────────────────────────────── */
    async function openItem(item, force = false) {
        if (!force && state.dirty && !confirm('Discard unsaved review changes?')) return;
        const request = ++reviewRequest; ++referenceRequest;
        state.item = item; state.result = null; state.edits = []; state.dirty = false; state.changes = [];
        document.querySelectorAll('.rp-episode-open').forEach(row => { if (row.dataset.mediaId === item.id) row.setAttribute('aria-current', 'true'); else row.removeAttribute('aria-current'); });
        const ref = $('rpReferenceVideo'); ref.pause(); ref.removeAttribute('src'); ref.dataset.mediaId = ''; ref.hidden = true; $('rpReferencePreview').hidden = true; $('rpReference').value = '';
        $('rpReferenceAudio').replaceChildren(option('', 'Select audio')); $('rpReferenceState').textContent = 'Choose an episode to compare.'; $('rpCompare').open = false;
        $('rpReviewEmpty').hidden = true; $('rpReview').hidden = false;
        $('rpReviewHeading').textContent = episodeName(item); $('rpReviewContext').textContent = context(item);
        const status = episodeStatus(item); paintChip($('rpStatusChip'), status);
        setOutcome('running', 'Loading this episode…', 'Reading the saved results.');
        const video = $('rpVideo'); video.pause(); $('rpVideoAudio').replaceChildren(option('', 'Select audio')); $('rpVideoAudioWrap').hidden = true; clear($('rpPublication'));
        Object.assign(video.dataset, { mediaId: item.id, duration: item.duration_ms || '0', origin: '0', preview: 'false', desired: '0' });
        $('rpVideoOrigin').textContent = ''; $('rpVideoPreview').hidden = true; $('rpVideoError').hidden = true;
        video.src = '/api/reprise/media/' + encodeURIComponent(item.id) + '/stream'; video.load();
        renderReferences(); clear($('rpRanges')); clear($('rpTimeline')); clear($('rpEvidence')); $('rpSave').disabled = true; $('rpAddKeep').disabled = true; $('rpAnalyzeOne').disabled = true;
        if (narrow.matches) document.querySelector('.rp-workspace').scrollIntoView({ block: 'start' });
        try {
            const result = await api(`/api/reprise/media/${encodeURIComponent(item.id)}/result`); if (request !== reviewRequest) return;
            state.result = result; setAudio($('rpVideoAudio'), result.identities);
            state.edits = (result.ranges || [...(result.proposed_ranges || []), ...(result.protected_keep_ranges || []), ...(result.eligible_skip_ranges || [])])
                .filter((r, i, a) => a.findIndex(x => x.id === r.id) === i).sort((a, b) => a.start_ms - b.start_ms || a.end_ms - b.end_ms).map(r => ({ ...r, originalAction: r.action }));
            renderReview(); await loadHistory(item.id, request);
        } catch (error) {
            if (request !== reviewRequest) return;
            setOutcome('error', "This episode's results couldn't be loaded.", error.message); fail(error);
        }
    }
    async function loadHistory(id, request) {
        if (!state.settings?.publication_available) return;
        try { const history = await api('/api/reprise/publications?media_version_id=' + encodeURIComponent(id)); if (request === reviewRequest && Array.isArray(history.items)) { state.changes = history.items; renderPublication(); } }
        catch (error) { if (request === reviewRequest) message('Publication history unavailable: ' + error.message, true); }
    }

    /* What the open episode's analysis amounts to, in one place a person looks first. */
    function setOutcome(stateName, title, body, extra) {
        const box = $('rpOutcome'); clear(box); box.dataset.state = stateName; box.append(el('strong', title));
        if (body) box.append(el('p', body)); if (extra) extra(box);
    }
    function renderOutcome() {
        const item = state.item, r = state.result; if (!item) return;
        const job = activeJobFor(item.id), analyzeBtn = $('rpAnalyzeOne');
        if (r) {
            // The open episode's own result is the freshest truth: keep its list row and header chip in step with it.
            item.analysis_status = r.analysis_status; item.freshness = r.freshness;
            const count = test => state.edits.filter(test).length;
            if (r.analysis_status === 'complete' && !state.dirty) { const open = state.edits.filter(x => isSuggestion(x) && x.review_status === 'proposed'); item.ranges = { skip: count(x => x.action === 'skip'), keep: count(x => x.action === 'keep'), proposed: count(x => x.review_status === 'proposed'), approved: count(x => x.review_status === 'approved'), to_review: count(x => x.action === 'skip' && x.review_status === 'proposed' && !isSuggestion(x)), suggested: open.length, suggested_by: suggesterNames(open) }; }
        }
        paintChip($('rpStatusChip'), episodeStatus(item)); paintStatuses();
        const stale = !!r && r.freshness === 'stale', again = !!r && ['failed', 'cancelled', 'unsupported'].includes(r.analysis_status);
        analyzeBtn.hidden = stale; analyzeBtn.disabled = !r || !!job;
        analyzeBtn.textContent = job ? 'Analyzing…' : r && r.analysis_status === 'complete' ? 'Analyze again' : again ? 'Try again' : 'Analyze';
        analyzeBtn.classList.toggle('primary', !r || r.analysis_status !== 'complete');
        if (job) {
            const pct = typeof job.progress?.percent === 'number' ? Math.max(0, Math.min(100, job.progress.percent)) : null;
            return setOutcome(job.status === 'running' ? 'running' : 'queued', job.status === 'running' ? 'Analyzing this episode…' : 'Waiting for its turn to be analyzed…',
                'A season with many episodes can take several minutes. You can leave this page; the results will be here when it finishes.', box => {
                    if (pct != null && job.status === 'running') { const bar = el('div', null, 'rp-bar'); bar.append(el('span')); bar.firstChild.style.width = pct + '%'; bar.setAttribute('role', 'progressbar'); bar.setAttribute('aria-valuenow', String(Math.round(pct))); box.append(bar); }
                    if (job.progress?.current_item) box.append(el('p', job.progress.current_item));
                });
        }
        if (!r) return;
        const reasons = rawReasons(r), plain = [...new Set((r.reasons || []).filter(x => x !== 'unapproved_or_unverified').map(plainReason).filter(Boolean))];
        const details = box => { if (reasons.length) { const d = el('details'); d.append(el('summary', 'Details'), el('ul')); reasons.forEach(x => d.lastChild.append(el('li', x))); box.append(d); } };
        if (r.freshness === 'stale') {
            return setOutcome('stale', 'This file has changed since Reprise last read it.', 'Refresh the library so Reprise reads the new file, then analyze it again.', box => {
                const actions = el('div', null, 'rp-actions'); actions.append(button('Refresh library', startRefresh)); box.append(actions); details(box);
            });
        }
        switch (r.analysis_status) {
            case 'complete': {
                const suggestions = state.edits.filter(isSuggestion), suggestionsOpen = suggestions.filter(x => x.review_status === 'proposed').length;
                const names = suggesterNames(suggestions), plexOnly = names.length === 1 && names[0] === 'Plex', noun = plexOnly ? 'Plex suggestion' : 'suggestion';
                const skip = state.edits.filter(x => x.action === 'skip' && !isSuggestion(x)).length, keep = state.edits.filter(x => x.action === 'keep').length;
                const pending = state.edits.filter(x => x.action === 'skip' && x.review_status === 'proposed' && !isSuggestion(x)).length;
                if (!state.edits.length) {
                    return setOutcome('none', 'No repeated intro or credits found.',
                        plain[0] || 'Reprise compared this episode with other episodes of the same season, but nothing matched closely enough to suggest a skip.',
                        box => { plain.slice(1).forEach(sentence => box.append(el('p', sentence))); details(box); });
                }
                const parts = joinAnd([skip ? plural(skip, 'section') + ' it can skip' : '', suggestions.length ? plural(suggestions.length, noun) : '', keep ? plural(keep, 'protected scene') : '']);
                const plexNote = suggestionsOpen ? ` ${plexOnly ? 'Plex suggestions are' : 'Suggestions are'} not confirmed by Reprise; approve each one yourself only if it is right.` : '';
                const next = !skip && !suggestions.length ? 'There is nothing to skip here. Protected scenes are never skipped.' : pending
                    ? `${plural(pending, 'section')} still need${pending === 1 ? 's' : ''} your review. Nothing is skipped until you approve it and save.${plexNote}` : suggestionsOpen
                        ? `Nothing is skipped until you approve it and save.${plexNote}` : 'Every section has a decision. Save the review to keep your choices.';
                if (!skip && suggestions.length) {
                    const headline = plain[0] && !names.some(n => plain[0].startsWith(n)) ? plain[0] + ' ' : '';
                    return setOutcome('suggested', `${joinAnd(names)} suggested ${plural(suggestions.length, 'section')} to skip, but Reprise could not confirm ${suggestions.length === 1 ? 'it' : 'them'}.`, headline + next, details);
                }
                return setOutcome('found', `Reprise found ${parts}.`, next, details);
            }
            case 'failed': case 'unsupported': case 'cancelled': {
                const title = r.analysis_status === 'cancelled' ? 'The analysis was cancelled.' : r.analysis_status === 'unsupported' ? "Reprise can't analyze this file." : 'The analysis could not finish.';
                return setOutcome('failed', title, plain[0] || 'Try again. If it keeps failing, Settings, Logs shows what went wrong.', details);
            }
            case 'running': return setOutcome('running', 'An analysis is in progress for this episode.', 'The results will appear here when it finishes.');
            default:
                return setOutcome('idle', "This episode hasn't been analyzed yet.", 'Choose Analyze to find repeated intros and credits. Reprise compares it with other episodes of the same season.', details);
        }
    }

    function renderFacts() {
        const dl = $('rpFacts'); clear(dl); const item = state.item, r = state.result || {}; if (!item) return;
        const rows = [['Show', item.show_title], ['Episode', seasonEpisode(item)], ['Duration', r.duration_ms || item.duration_ms ? ms(r.duration_ms || item.duration_ms) : ''],
            ['Analysis', r.analysis_status || item.analysis_status], ['File', r.freshness || item.freshness]];
        (r.identities || item.identities || []).forEach((id, n) => {
            const tag = (r.identities || item.identities).length > 1 ? ` (copy ${n + 1})` : '';
            rows.push([`Plex item${tag}`, id.rating_key], [`Media / part / audio${tag}`, [id.media_id, id.part_id, id.audio_stream_id].join(' / ')]);
            if (Number(id.part_size_bytes) > 0) rows.push([`File size${tag}`, (Number(id.part_size_bytes) / 1e6).toFixed(2) + ' MB']);
        });
        if (r.result_revision != null) rows.push(['Result revision', String(r.result_revision)]);
        rows.filter(([, v]) => v !== '' && v != null).forEach(([k, v]) => dl.append(el('dt', k), el('dd', String(v))));
    }

    /* ── references and playback ────────────────────────────────────────────────────────────────────────── */
    function renderReferences() {
        const select = $('rpReference'), previous = select.value; select.replaceChildren(option('', 'Choose an episode'));
        const candidates = state.items.filter(i => i.id !== state.item?.id && (!state.item?.show_title || i.show_title === state.item.show_title));
        const names = candidates.map(i => [seasonEpisode(i), episodeName(i)].filter(Boolean).join(' · '));
        candidates.forEach((i, n) => {
            const copies = names.filter(x => x === names[n]).length, sizes = (i.identities || []).map(id => Number(id.part_size_bytes)).filter(x => x > 0);
            select.append(option(i.id, names[n] + (copies > 1 && sizes.length ? ` · ${(sizes[0] / 1e6).toFixed(1)} MB copy` : '')));
        });
        if ([...select.options].some(o => o.value === previous)) select.value = previous;
    }
    function fileTime(video) { return Math.round((Number(video.dataset.origin) || 0) + (video.currentTime || 0) * 1000); }
    function playBounded(video, start, end) {
        video.dataset.desired = start; const origin = Number(video.dataset.origin) || 0;
        if (video.dataset.preview === 'true' && (start < origin || end > origin + Number(video.dataset.previewDuration))) {
            message('This section is outside the current preview. Create another preview.', true); $(video.id === 'rpVideo' ? 'rpVideoPreview' : 'rpReferencePreview').hidden = false; return;
        }
        if (video._rpStop) video.removeEventListener('timeupdate', video._rpStop);
        video.pause(); video.currentTime = Math.max(0, (start - origin) / 1000);
        const stop = () => { if (fileTime(video) >= end || video.ended) { video.pause(); video.removeEventListener('timeupdate', stop); } };
        video._rpStop = stop; video.addEventListener('timeupdate', stop); video.play().catch(error => { video.removeEventListener('timeupdate', stop); fail(error); });
    }
    function setAudio(select, identities) {
        const ids = [...new Set((identities || []).map(i => i.audio_stream_id).filter(Boolean))]; select.replaceChildren(option('', 'Select audio'));
        ids.forEach((id, n) => select.append(option(id, `Audio track ${n + 1}`))); if (ids.length === 1) select.value = ids[0];
        if (select.id === 'rpVideoAudio') $('rpVideoAudioWrap').hidden = ids.length < 2;
        $(select.id === 'rpVideoAudio' ? 'rpVideoPreview' : 'rpReferencePreview').hidden = !select.value || !$(select.id === 'rpVideoAudio' ? 'rpVideoError' : 'rpReferenceVideo').dataset.failed && select.id === 'rpReferenceAudio';
        if (select.id === 'rpVideoAudio') $('rpVideoPreview').hidden = $('rpVideoError').hidden;
    }
    async function loadReference(id) {
        const request = ++referenceRequest, video = $('rpReferenceVideo'), audio = $('rpReferenceAudio');
        video.pause(); video.hidden = true; audio.replaceChildren(option('', 'Select audio')); audio.disabled = true;
        Object.assign(video.dataset, { mediaId: id, duration: '0', origin: '0', preview: 'false', desired: '0' });
        $('rpReferenceOrigin').textContent = ''; $('rpReferencePreview').hidden = true;
        video.src = '/api/reprise/media/' + encodeURIComponent(id) + '/stream'; video.load();
        try {
            const result = await api('/api/reprise/media/' + encodeURIComponent(id) + '/result');
            if (request !== referenceRequest) return false;
            video.dataset.duration = result.duration_ms || '0'; setAudio(audio, result.identities); audio.disabled = false; video.hidden = false; return true;
        } catch (error) { if (request !== referenceRequest) return false; throw error; }
    }
    async function makePreview(video, audio, trigger, note) {
        const mediaId = video.dataset.mediaId, duration = Number(video.dataset.duration), desired = Number(video.dataset.desired || fileTime(video));
        if (!mediaId || !duration || !audio.value) { message('Choose an audio track and a valid episode before creating a preview.', true); return; }
        const start = Math.max(0, Math.min(duration - 1, desired - 15000)), end = Math.min(duration, start + 90000); trigger.disabled = true; note.textContent = 'Preparing a playable preview…';
        try {
            let job = await api('/api/reprise/media/' + encodeURIComponent(mediaId) + '/preview', { method: 'POST', body: { start_ms: start, end_ms: end, audio_stream_id: audio.value } });
            while (!['completed', 'complete', 'failed', 'cancelled'].includes(job.status)) { await new Promise(resolve => setTimeout(resolve, 1500)); if (video.dataset.mediaId !== mediaId) return; job = await api('/api/reprise/jobs/' + encodeURIComponent(job.id)); }
            if (video.dataset.mediaId !== mediaId) return;
            if (!job.preview || !job.preview.url || job.status === 'failed' || job.status === 'cancelled') throw new Error(job.error || 'The preview could not be created.');
            const url = new URL(job.preview.url, location.origin); if (url.origin !== location.origin) throw new Error('Invalid preview address.');
            if (!Number.isInteger(job.preview.origin_ms) || !Number.isInteger(job.preview.duration_ms) || job.preview.duration_ms <= 0) throw new Error('Preview timing is unavailable.');
            Object.assign(video.dataset, { origin: String(job.preview.origin_ms), previewDuration: String(job.preview.duration_ms), preview: 'true' });
            video.src = url.pathname + url.search; video.addEventListener('loadedmetadata', () => { video.currentTime = Math.max(0, (desired - job.preview.origin_ms) / 1000); }, { once: true }); video.load();
            note.textContent = 'This preview starts at ' + ms(job.preview.origin_ms) + ' in the episode and covers about 90 seconds.'; trigger.hidden = true;
        } catch (error) { note.textContent = error.message; } finally { trigger.disabled = false; }
    }

    /* ── the review ─────────────────────────────────────────────────────────────────────────────────────── */
    function renderReview(rebuild = true) {
        const r = state.result; if (!r) return;
        const duration = Number(r.duration_ms || state.item?.duration_ms || 0); $('rpDuration').textContent = duration ? 'Length ' + ms(duration) : '';
        const timeline = $('rpTimeline'); clear(timeline);
        const bar = (cls, a, b, title) => { const s = el('span', null, `rp-timeline-segment ${cls}`); s.style.left = `${Math.max(0, a / duration * 100)}%`; s.style.width = `${Math.max(0, Math.min(duration, b) - a) / duration * 100}%`; s.title = title; timeline.append(s); };
        if (duration) {
            for (const range of state.edits) if (range.end_ms > range.start_ms) bar(range.action === 'keep' ? 'keep' : isSuggestion(range) && range.review_status !== 'approved' ? 'skip suggested' : 'skip', range.start_ms, range.end_ms, `${isSuggestion(range) ? suggestionBadge(range) : range.action} ${ms(range.start_ms)}–${ms(range.end_ms)}`);
            for (const e of r.evidence || []) if (e.end_ms > e.start_ms) bar('evidence', e.start_ms, e.end_ms, `${e.verdict || 'Evidence'} ${ms(e.start_ms)}–${ms(e.end_ms)}`);
        }
        if (rebuild) {
            const list = $('rpRanges'); clear(list);
            if (!state.edits.length) list.append(el('p', r.analysis_status === 'complete' ? 'Nothing to review. No section is approved for skipping.' : 'Sections appear here after an analysis.', 'rp-state'));
            state.edits.forEach(range => renderRange(list, range));
            renderEvidence(); renderFacts(); renderPublication();
        }
        const current = r.freshness === 'current' && r.result_revision != null;
        $('rpSave').disabled = !state.dirty || !current; $('rpDiscard').disabled = !state.dirty; $('rpAddKeep').disabled = !current;
        $('rpSaveBar').hidden = !state.dirty;
        $('rpSaveState').textContent = r.freshness !== 'current' ? 'This file has changed, so its review can’t be saved until the library is refreshed.' : state.dirty ? 'You have unsaved changes.' : 'Saving a review never changes Plex.';
        renderOutcome();
    }
    function renderEvidence() {
        const box = $('rpEvidence'), r = state.result; clear(box);
        const codes = (r.reasons || []).filter(isCode); if (codes.length) box.append(el('p', 'Technical reasons: ' + codes.join(', '), 'rp-note'));
        const evidence = r.evidence || [];
        if (!evidence.length) { box.append(el('p', 'No evidence was recorded for this episode.', 'rp-state')); return; }
        evidence.forEach(e => {
            const row = el('div', null, 'rp-evidence-row'), head = el('div', null, 'rp-actions');
            head.append(el('strong', ms(e.start_ms) + ' – ' + ms(e.end_ms)), el('span', e.verdict || 'unknown', 'rp-chip'));
            row.append(head, el('p', [e.modality, e.reason].filter(Boolean).join(' · '), 'rp-note'));
            if (e.reference_version_id && e.reference_start_ms != null && e.reference_end_ms != null) {
                row.append(button('Compare with the matching episode', async () => {
                    try {
                        if (!await loadReference(e.reference_version_id)) return;
                        $('rpReferenceState').textContent = 'Showing the matching part of the other episode.'; $('rpCompare').open = true;
                        playBounded($('rpVideo'), e.start_ms, e.end_ms); playBounded($('rpReferenceVideo'), e.reference_start_ms, e.reference_end_ms);
                    } catch (error) { fail(error); }
                }));
            }
            box.append(row);
        });
    }
    function renderRange(list, range) {
        const known = rangeViews.get(range); if (known) { list.append(known.box); return; }
        const id = ++uid, box = el('details', null, 'rp-range'), summary = el('summary'), name = el('span', null, 'rp-range-name'), bounds = el('span', null, 'rp-range-bounds'), chip = el('span', null, 'rp-chip');
        const badge = el('span', null, 'rp-chip'), radios = [];
        box.open = !range.id && range.origin === 'manual';
        const updateSummary = () => {
            name.textContent = (range.action === 'keep' ? 'Keep ' : 'Skip ') + kindLabel(range.kind);
            bounds.textContent = ms(range.start_ms) + ' – ' + ms(range.end_ms) + (range.end_ms > range.start_ms ? ` (${Math.round((range.end_ms - range.start_ms) / 1000)} s)` : '');
            chip.textContent = decisionLabel(range.review_status) + (range.locked ? ' · Locked' : ''); chip.dataset.tone = range.review_status === 'approved' ? 'positive' : range.review_status === 'rejected' ? 'danger' : 'accent';
            if (isSuggestion(range)) { badge.textContent = suggestionBadge(range) + (range.review_status === 'approved' ? ' · approved by you' : ''); badge.dataset.tone = 'info'; chip.hidden = range.review_status === 'approved' && !range.locked; }
        };
        const changed = () => { if (isSuggestion(range) && range.review_status === 'approved') range.acknowledged = true; const was = state.dirty; state.dirty = true; updateSummary(); renderReview(false); if (!was) renderPublication(); };
        summary.append(name, bounds, ...(isSuggestion(range) ? [badge] : []), chip); box.append(summary);
        const body = el('div', null, 'rp-range-body'); body.append(el('p', plainReason(range.reason || range.reason_code || '') || 'Added by you.', 'rp-note'));
        const seg = el('div', null, 'rp-seg'); seg.setAttribute('role', 'radiogroup'); seg.setAttribute('aria-label', 'Your decision');
        const syncRadios = () => radios.forEach(input => { input.checked = input.value === (range.review_status || 'proposed'); });
        const confirmBox = el('div', null, 'rp-plex-confirm'); confirmBox.hidden = true; confirmBox.setAttribute('role', 'group'); confirmBox.setAttribute('aria-label', 'Confirm approving a suggestion');
        const confirmActions = el('div', null, 'rp-actions');
        confirmActions.append(button('Approve it', () => { range.review_status = 'approved'; range.acknowledged = true; confirmBox.hidden = true; syncRadios(); changed(); }), button('Not now', () => { confirmBox.hidden = true; syncRadios(); }, 'rp-btn quiet'));
        confirmBox.append(el('p', `${joinAnd(suggesters(range))} found this, Reprise could not confirm it. Approve it only if it is right.`), confirmActions);
        [['approved', 'Approve'], ['rejected', 'Reject'], ['proposed', 'Decide later']].forEach(([value, text]) => {
            const label = el('label'), input = el('input'); input.type = 'radio'; input.name = 'decision-' + id; input.value = value; input.checked = (range.review_status || 'proposed') === value;
            input.addEventListener('change', () => {
                if (value === 'approved' && isSuggestion(range) && range.review_status !== 'approved') { syncRadios(); confirmBox.hidden = false; return; }
                range.review_status = value; changed();
            }); radios.push(input); label.dataset.value = value; label.append(input, el('span', text)); seg.append(label);
        });
        const actions = el('div', null, 'rp-actions'); actions.append(seg, button('Play this section', () => playBounded($('rpVideo'), range.start_ms, range.end_ms)));
        if (range.origin === 'manual' && !range.id) actions.append(button('Remove', () => { state.edits = state.edits.filter(edit => edit !== range); state.dirty = true; renderReview(); $('rpAddKeep').focus(); }, 'rp-btn quiet'));
        body.append(actions, confirmBox);
        const fine = el('details', null, 'rp-fine'), grid = el('div', null, 'rp-fine-grid'); fine.append(el('summary', 'Fine-tune this section'), grid);
        const numberField = (text, key) => {
            const wrap = el('div', null, 'rp-set'), label = el('label', text), input = el('input'); input.type = 'number'; input.min = '0'; input.step = '1'; input.value = range[key] ?? '';
            input.addEventListener('input', () => { range[key] = input.value === '' ? NaN : Number(input.value); changed(); }); label.append(input);
            wrap.append(label, button('Use video time', () => { const v = fileTime($('rpVideo')); range[key] = v; input.value = String(v); changed(); })); return wrap;
        };
        const selectField = (text, key, values) => {
            const label = el('label', text), input = el('select'); values.forEach(([v, t]) => input.append(option(v, t))); input.value = range[key] ?? '';
            input.addEventListener('change', () => { range[key] = input.value; changed(); }); label.append(input); return label;
        };
        grid.append(numberField('Start (milliseconds)', 'start_ms'), numberField('End (milliseconds)', 'end_ms'),
            selectField('What to do', 'action', range.id && range.originalAction !== 'keep' ? [['skip', 'Skip it'], ['keep', 'Keep it']] : [['keep', 'Keep it']]),
            selectField('Type', 'kind', [['intro', 'Intro'], ['credits', 'Credits'], ['recap', 'Recap'], ['preview', 'Next-episode preview']]));
        const lock = el('label', null, 'rp-check'), check = el('input'); check.type = 'checkbox'; check.checked = !!range.locked;
        check.addEventListener('change', () => { range.locked = check.checked; changed(); }); lock.append(check, document.createTextNode('Lock this section so a new analysis never changes it'));
        fine.append(lock); body.append(fine); box.append(body); updateSummary(); rangeViews.set(range, { box }); list.append(box);
    }
    async function saveReview() {
        const duration = Number(state.result?.duration_ms || state.item?.duration_ms || 0);
        if (state.edits.some(r => !Number.isInteger(r.start_ms) || !Number.isInteger(r.end_ms) || r.start_ms < 0 || r.start_ms >= r.end_ms || (duration && r.end_ms > duration))) return message('Each section needs a start before its end, inside the episode.', true);
        const edits = state.edits.map(r => ({ range_id: r.id || null, action: r.action, kind: r.kind, start_ms: r.start_ms, end_ms: r.end_ms, review_status: r.review_status || 'proposed', locked: !!r.locked, reason: r.reason || '', ...(isSuggestion(r) && r.review_status === 'approved' && r.acknowledged ? { acknowledge_suggestion: true } : {}) }));
        try { $('rpSave').disabled = true; await api(`/api/reprise/media/${encodeURIComponent(state.item.id)}/review`, { method: 'PATCH', body: { expected_revision: state.result.result_revision, edits } }); state.dirty = false; await openItem(state.item, true); message('Review saved. Nothing was sent to Plex.'); }
        catch (error) { fail(error); $('rpSave').disabled = false; }
    }

    /* Per-episode Plex publication. The master switch is on the Sharing page; everything here stays confirmation-gated. */
    function renderPublication() {
        const root = $('rpPublication'); clear(root);
        const identities = state.result?.identities || state.item?.identities || [];
        if (!identities.length) { root.append(el('p', 'This file has no verified Plex identity, so it cannot be sent to Plex.', 'rp-state')); return; }
        const label = el('label', 'Which Plex copy'), select = el('select');
        identities.forEach((id, i) => select.append(option(String(i), `Plex item ${id.rating_key} · media ${id.media_id} · part ${id.part_id} · audio ${id.audio_stream_id}`))); label.append(select); root.append(label);
        const available = state.settings?.publication_available, enabled = state.settings?.writes_enabled;
        if (!available || !enabled) root.append(el('p', 'Sending to Plex is switched off, so nothing can be prepared or applied.', 'rp-note'));
        const prepare = button('Prepare a Plex change', async () => {
            try { const result = await api('/api/reprise/publications/prepare', { method: 'POST', body: { media_version_id: state.item.id, expected_revision: state.result.result_revision, identity: identities[Number(select.value)] } }); state.changes.unshift(result); renderPublication(); } catch (error) { fail(error); }
        });
        prepare.disabled = !available || !enabled || state.dirty || state.result?.result_revision == null; root.append(prepare);
        state.changes.forEach(change => {
            const row = el('div', null, 'rp-job'), info = el('div', null, 'rp-job-main'); info.append(el('strong', `Plex change ${change.status || 'unknown'}`), el('span', change.message || change.reason || '', 'rp-note')); row.append(info);
            const summaryValid = [change.before_markers, change.after_markers].every(markers => Array.isArray(markers) && markers.every(marker => typeof marker.type === 'string' && marker.rating_key != null && Number.isInteger(marker.start_ms) && Number.isInteger(marker.end_ms)));
            if (summaryValid) {
                for (const [heading, markers] of [['Current markers', change.before_markers], ['Proposed markers', change.after_markers]]) {
                    info.append(el('strong', heading));
                    if (!markers.length) info.append(el('span', 'None', 'rp-note')); else markers.forEach(marker => info.append(el('span', marker.type + ' · ' + ms(marker.start_ms) + ' – ' + ms(marker.end_ms) + ' · item ' + marker.rating_key, 'rp-note')));
                }
            }
            if (change.status === 'prepared') {
                const apply = button('Apply to Plex', async () => { if (!confirm('Apply this prepared marker change to Plex?')) return; try { Object.assign(change, await api('/api/reprise/publications/' + encodeURIComponent(change.id) + '/apply', { method: 'POST', body: {} })); renderPublication(); } catch (error) { fail(error); } }, 'rp-btn primary');
                apply.disabled = !summaryValid; if (apply.disabled) info.append(el('span', 'Change details are unavailable, so applying is disabled.', 'rp-note')); row.append(apply);
            }
            if (change.status === 'applying' || change.status === 'undoing') {
                info.append(el('span', 'This change was interrupted. Check its state without repeating it.', 'rp-note'));
                row.append(button('Check state', async () => { try { Object.assign(change, await api('/api/reprise/publications/' + encodeURIComponent(change.id) + '/recover', { method: 'POST', body: {} })); renderPublication(); } catch (error) { fail(error); } }));
            }
            if (change.status === 'uncertain') info.append(el('span', 'The result is uncertain. Nothing will be repeated automatically.', 'rp-note'));
            if (change.status === 'applied') row.append(button('Undo', async () => { if (!confirm('Undo this published marker change?')) return; try { Object.assign(change, await api(`/api/reprise/publications/${encodeURIComponent(change.id)}/undo`, { method: 'POST', body: {} })); renderPublication(); } catch (error) { fail(error); } }));
            root.append(row);
        });
    }

    /* ── activity: what Reprise is doing and what happened ──────────────────────────────────────────────── */
    function describeJob(job) {
        const ids = jobMediaIds(job), terminal = terminalJob(job.status);
        if (job.operation === 'refresh') {
            const done = terminal && job.refresh ? `Added or updated ${plural(job.refresh.registered, 'episode')}; ${job.refresh.skipped} skipped.` : '';
            return { title: 'Refreshing the library', detail: done };
        }
        if (job.operation === 'preview') return { title: 'Preparing a playable preview', detail: '' };
        let title = 'Analyzing episodes';
        if (ids) { const only = ids.length === 1 ? state.items.find(i => i.id === ids[0]) : null; title = only ? `Analyzing ${episodeName(only)}` : `Analyzing ${plural(ids.length, 'episode')}`; }
        const outcome = job.outcome && typeof job.outcome === 'object' ? job.outcome : null;
        let detail = '';
        if (terminal) {
            if (outcome && outcome.message) detail = outcome.message;
            else if (job.status === 'completed') detail = 'Finished. Open an episode to see what was found.';
            else if (job.status === 'cancelled') detail = 'Cancelled.';
            else detail = 'Failed. Settings, Logs shows what went wrong.';
        }
        return { title, detail };
    }
    function renderJobRow(job) {
        const terminal = terminalJob(job.status), { title, detail } = describeJob(job);
        const row = el('div', null, 'rp-job'), main = el('div', null, 'rp-job-main'); row.dataset.status = job.status;
        main.append(el('strong', title));
        const pct = typeof job.progress?.percent === 'number' ? Math.max(0, Math.min(100, job.progress.percent)) : null;
        const when = job.created_at ? new Date(job.created_at) : null, ago = when && !Number.isNaN(+when) ? when.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '';
        const status = terminal ? (detail || (job.status === 'completed' ? 'Finished.' : job.status)) :
            [job.status === 'running' ? 'Running' : 'Waiting', job.pause_requested ? 'pause requested' : '', pct != null && job.status === 'running' ? Math.round(pct) + '%' : '', job.progress?.current_item || ''].filter(Boolean).join(' · ');
        main.append(el('span', status + (terminal && ago ? ` (${ago})` : ''), 'rp-note'));
        if (!terminal && pct != null && job.status === 'running') { const bar = el('div', null, 'rp-bar'); const fill = el('span'); fill.style.width = pct + '%'; bar.append(fill); bar.setAttribute('role', 'progressbar'); bar.setAttribute('aria-valuenow', String(Math.round(pct))); bar.setAttribute('aria-label', title); main.append(bar); }
        row.append(main);
        const actions = terminal ? [] : job.pause_requested ? ['resume', 'cancel'] : job.status === 'running' ? ['pause', 'cancel'] : ['pending', 'queued'].includes(job.status) ? ['cancel'] : [];
        if (actions.length) {
            const group = el('div', null, 'rp-job-actions');
            for (const action of actions) group.append(button(action[0].toUpperCase() + action.slice(1), async () => { try { await api('/api/reprise/jobs/' + encodeURIComponent(job.id) + '/' + action, { method: 'POST', body: {} }); await loadQueue(); } catch (error) { fail(error); } }, 'rp-btn' + (action === 'cancel' ? ' quiet' : '')));
            row.append(group);
        }
        return row;
    }
    function renderActivity() {
        const jobs = state.jobs.slice().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
        const active = jobs.filter(j => !terminalJob(j.status)), finished = jobs.filter(j => terminalJob(j.status));
        const root = $('rpQueue'), section = $('rpActivity'); section.hidden = !jobs.length; if (!jobs.length) return;
        clear(root); const shown = active.concat(state.showOlder ? finished : finished.slice(0, 3));
        shown.forEach(job => root.append(renderJobRow(job)));
        if (finished.length > 3) root.append(button(state.showOlder ? 'Show less' : `Show ${finished.length - 3} earlier`, () => { state.showOlder = !state.showOlder; renderActivity(); }, 'rp-btn quiet rp-activity-more'));
        const toggle = $('rpActivityToggle'); root.hidden = state.activityHidden && !active.length; toggle.textContent = root.hidden ? 'Show' : 'Hide'; toggle.setAttribute('aria-expanded', String(!root.hidden));
    }
    async function loadQueue() {
        clearTimeout(queueTimer);
        let delay = 10000;
        try {
            const data = await api('/api/reprise/jobs'), jobs = data.items;
            if (!Array.isArray(jobs)) throw new Error('The activity response was incomplete.');
            const wasActive = new Set(state.jobs.filter(j => !terminalJob(j.status)).map(j => j.id));
            state.jobs = jobs; let finishedRefresh = false, finishedAnalysis = false;
            for (const job of jobs) {
                const terminal = terminalJob(job.status), before = state.seenJobs.get(job.id);
                if (job.operation === 'refresh') { if (!terminal) activeRefreshes.add(job.id); else if (activeRefreshes.delete(job.id)) finishedRefresh = true; }
                if (job.operation === 'analysis' && terminal && (wasActive.has(job.id) || (before && !terminalJob(before)))) finishedAnalysis = true;
                state.seenJobs.set(job.id, job.status);
            }
            const active = jobs.some(j => !terminalJob(j.status)); if (active) { delay = 2500; if (!wasActive.size) state.activityHidden = false; }
            renderActivity(); paintStatuses(); if (state.item) renderOutcome();
            if (finishedRefresh) { await loadLibrary(); message('Library refresh finished.'); }
            else if (finishedAnalysis) {
                await reloadLoaded();
                if (state.item && !activeJobFor(state.item.id)) { if (state.dirty) message('A new analysis finished for the open episode. Save or discard your changes to see it.'); else await openItem(state.item, true); }
                message('Analysis finished. See each episode for what was found.');
            }
        } catch (error) { if (!state.jobs.length) { $('rpActivity').hidden = false; $('rpQueue').textContent = error.message; } }
        queueTimer = setTimeout(loadQueue, document.hidden ? 10000 : delay);
    }

    /* ── refresh ────────────────────────────────────────────────────────────────────────────────────────── */
    async function startRefresh() {
        const form = $('rpRefreshForm'), select = $('rpLibraryChoice');
        try {
            const libs = await loadLibraries();
            if (!libs.length) { form.hidden = false; select.replaceChildren(); select.disabled = true; $('rpRefreshState').textContent = 'No Plex library is connected yet. Add one under Settings, Connections.'; return; }
            select.disabled = false; select.replaceChildren(); libs.forEach((lib, i) => select.append(option(String(i), (lib.server_name ? lib.server_name + ' · ' : '') + lib.name)));
            if (libs.length === 1) return queueRefresh(libs[0]);
            $('rpRefreshState').textContent = ''; form.hidden = false; select.focus();
        } catch (error) { fail(error); }
    }
    async function queueRefresh(lib) {
        try {
            const job = await api('/api/reprise/refresh', { method: 'POST', body: { server_id: lib.server_id, library_id: String(lib.id) } });
            if (!job.id) throw new Error('The refresh response was incomplete.');
            activeRefreshes.add(job.id); $('rpRefreshForm').hidden = true; message(`Refreshing ${lib.name}. New episodes appear in the list when it finishes.`); await loadQueue();
        } catch (error) { fail(error); }
    }

    /* ── wiring ─────────────────────────────────────────────────────────────────────────────────────────── */
    for (const id of ['rpVideo', 'rpReferenceVideo']) $(id).addEventListener('seeked', () => { $(id).dataset.desired = String(fileTime($(id))); });
    for (const [audioId, videoId, buttonId, noteId] of [['rpVideoAudio', 'rpVideo', 'rpVideoPreview', 'rpVideoOrigin'], ['rpReferenceAudio', 'rpReferenceVideo', 'rpReferencePreview', 'rpReferenceOrigin']]) {
        $(audioId).addEventListener('change', () => {
            const video = $(videoId); video.dataset.desired = String(fileTime(video)); video.pause(); $(buttonId).hidden = !$(audioId).value || !video.dataset.mediaId;
            if (video.dataset.preview === 'true') { video.removeAttribute('src'); video.dataset.preview = 'false'; video.dataset.origin = '0'; video.load(); }
            $(noteId).textContent = $(audioId).value ? 'Create a preview to hear the selected audio track.' : 'Choose an audio track before creating a preview.';
        });
    }
    let searchTimer; $('rpSearch').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadLibrary(), 250); });
    $('rpShow').addEventListener('change', () => { $('rpSeason').value = ''; loadLibrary(); }); $('rpSeason').addEventListener('change', () => loadLibrary()); $('rpMore').addEventListener('click', () => loadLibrary(true));
    $('rpRefresh').addEventListener('click', startRefresh); $('rpRefreshCancel').addEventListener('click', () => { $('rpRefreshForm').hidden = true; });
    $('rpRefreshForm').addEventListener('submit', async event => { event.preventDefault(); const lib = (state.libraries || [])[Number($('rpLibraryChoice').value)]; if (lib) await queueRefresh(lib); });
    $('rpAnalyze').addEventListener('click', () => analyze([...state.selected])); $('rpSelectClear').addEventListener('click', () => { state.selected.clear(); renderLibrary(); });
    $('rpAnalyzeOne').addEventListener('click', () => { if (state.item && (!state.dirty || confirm('Discard unsaved review changes and analyze again?'))) analyze([state.item.id]); });
    $('rpActivityToggle').addEventListener('click', () => { const root = $('rpQueue'); state.activityHidden = !root.hidden; root.hidden = state.activityHidden; $('rpActivityToggle').textContent = root.hidden ? 'Show' : 'Hide'; $('rpActivityToggle').setAttribute('aria-expanded', String(!root.hidden)); });
    $('rpVideo').addEventListener('error', () => { $('rpVideoError').hidden = false; $('rpVideoPreview').hidden = !$('rpVideoAudio').value && $('rpVideoAudioWrap').hidden ? true : false; if ($('rpVideoAudio').options.length === 2) $('rpVideoPreview').hidden = false; });
    $('rpReference').addEventListener('change', event => {
        const video = $('rpReferenceVideo');
        if (!event.target.value) { ++referenceRequest; $('rpReferenceAudio').replaceChildren(option('', 'Select audio')); $('rpReferenceAudio').disabled = true; $('rpReferencePreview').hidden = true; video.pause(); video.hidden = true; video.dataset.mediaId = ''; video.removeAttribute('src'); video.load(); $('rpReferenceState').textContent = 'Choose an episode to compare.'; return; }
        $('rpReferenceState').textContent = 'Times for this episode are shown on its own timeline.'; loadReference(event.target.value).catch(fail);
    });
    $('rpReferenceVideo').addEventListener('error', () => { $('rpReferenceState').textContent = "Your browser can't play this reference directly."; $('rpReferencePreview').hidden = false; });
    $('rpVideoPreview').addEventListener('click', () => makePreview($('rpVideo'), $('rpVideoAudio'), $('rpVideoPreview'), $('rpVideoOrigin')));
    $('rpReferencePreview').addEventListener('click', () => makePreview($('rpReferenceVideo'), $('rpReferenceAudio'), $('rpReferencePreview'), $('rpReferenceOrigin')));
    $('rpSave').addEventListener('click', saveReview);
    $('rpDiscard').addEventListener('click', () => { if (state.item && (!state.dirty || confirm('Discard unsaved review changes?'))) openItem(state.item, true); });
    $('rpAddKeep').addEventListener('click', () => {
        const duration = Number(state.result?.duration_ms || state.item?.duration_ms || 0), start = fileTime($('rpVideo'));
        state.edits.push({ kind: 'credits', action: 'keep', start_ms: start, end_ms: Math.min(duration || start + 1000, start + 1000), review_status: 'proposed', locked: true, origin: 'manual', reason: 'Protected by you' });
        state.dirty = true; renderReview();
    });
    window.addEventListener('beforeunload', event => { if (state.dirty) { event.preventDefault(); event.returnValue = ''; } });
    /* How analysis decodes video (GPU or CPU): reported by the same settings payload as the Plex switch. */
    let hardwareTimer = null;
    function renderHardware() {
        const a = state.settings && state.settings.acceleration, box = $('rpHardware'); box.hidden = !a; if (!a) return;
        $('rpHardwareLabel').textContent = 'Analysis uses ' + a.label + '.';
        const toggle = $('rpHardwareToggle'); toggle.hidden = typeof a.enabled !== 'boolean'; toggle.textContent = a.enabled ? 'Use CPU only' : 'Use GPU when available';
        clearTimeout(hardwareTimer);
        if (/^CPU \(checking/i.test(a.label)) hardwareTimer = setTimeout(() => { api('/api/reprise/settings').then(s => { state.settings = s; renderHardware(); }).catch(() => {}); }, 5000);
    }
    $('rpHardwareToggle').addEventListener('click', async () => {
        const current = state.settings && state.settings.acceleration; if (!current) return;
        try { state.settings = await api('/api/reprise/settings', { method: 'PATCH', body: { gpu_decode: !current.enabled } }); renderHardware(); message(current.enabled ? 'Analysis will use the CPU only.' : 'Analysis will use a GPU when one is available.'); }
        catch (error) { fail(error); }
    });
    /* Whether analysis may ask public databases (IntroDB, SkipDB) where an episode's intro and credits are. */
    function renderOnline() {
        const on = state.settings && state.settings.online_lookups, box = $('rpOnline'); box.hidden = typeof on !== 'boolean'; if (box.hidden) return;
        $('rpOnlineToggle').checked = on;
    }
    $('rpOnlineToggle').addEventListener('change', async event => {
        const toggle = event.target, wanted = toggle.checked; toggle.disabled = true;
        try { state.settings = await api('/api/reprise/settings', { method: 'PATCH', body: { online_lookups: wanted } }); message(wanted ? 'Intro times will be looked up online.' : 'Intro times will no longer be looked up online.'); }
        catch (error) { fail(error); }
        finally { toggle.disabled = false; renderOnline(); }
    });
    const renderSettings = () => { renderHardware(); renderOnline(); };
    window.addEventListener('rp:settings', event => { state.settings = event.detail; renderSettings(); if (state.result) renderPublication(); });

    api('/api/reprise/settings').then(settings => { state.settings = settings; renderSettings(); if (state.result) { renderPublication(); loadHistory(state.item.id, reviewRequest); } }).catch(() => { /* the shell chip reports it */ });
    const initialSearch = new URLSearchParams(location.search).get('search'); if (initialSearch) $('rpSearch').value = initialSearch.slice(0, 200);
    loadLibrary(); loadQueue();
})();
