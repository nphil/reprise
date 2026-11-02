(() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const state = { items: [], selected: new Set(), item: null, result: null, edits: [], cursor: null, request: 0, dirty: false, secret: false, changes: [], libraries: [], settings: null };
    const activeRefreshes = new Set(), rangeViews = new WeakMap();
    let reviewRequest = 0, referenceRequest = 0;
    const terminalJob = status => ['completed', 'failed', 'cancelled'].includes(status);
    const analysisLabel = status => !status || status === 'missing' ? 'Not analyzed' : status;
    const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
    const button = (text, fn) => { const b = el('button', text); b.type = 'button'; b.addEventListener('click', fn); return b; };
    const clear = node => node.replaceChildren();
    function message(text, error = false) { const n = $('rpMessage'); n.textContent = text; n.classList.toggle('error', error); n.hidden = !text; }
    async function api(path, options = {}) {
        const init = { ...options, headers: { ...(options.headers || {}) } };
        if (init.body && typeof init.body !== 'string') { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(init.body); }
        const response = await fetch(path, init);
        if (response.status === 204) return {};
        let data; try { data = await response.json(); } catch (_) { throw new Error(`Request failed (${response.status}).`); }
        if (!response.ok) throw new Error(data.error || data.message || `Request failed (${response.status}).`);
        return data;
    }
    function fail(error) { message(error.message || 'Something went wrong.', true); }
    function ms(value) { const n = Math.max(0, Math.floor(Number(value) || 0)); const h = Math.floor(n / 3600000), m = Math.floor(n / 60000) % 60, s = Math.floor(n / 1000) % 60; return `${h ? `${h}:` : ''}${String(m).padStart(h ? 2 : 1, '0')}:${String(s).padStart(2, '0')}.${String(n % 1000).padStart(3, '0')}`; }
    function option(value, label) { const n = el('option', label); n.value = value; return n; }
    function title(item) { return item.show_title ? `${item.show_title} · S${String(item.season ?? '?').padStart(2, '0')}E${String(item.episode ?? '?').padStart(2, '0')} · ${item.title || 'Episode'}` : (item.title || 'Untitled'); }
    function versionLabel(item) {
        const identities = Array.isArray(item.identities) ? item.identities : [];
        if (!identities.length) return 'Playback identity unavailable';
        const labels = identities.map(identity => {
            const fields = [];
            if (identities.length > 1) fields.push(`Plex ${identity.plex_machine_identifier}, item ${identity.rating_key}`);
            fields.push(`Media ${identity.media_id}`, `part ${identity.part_id}`, `audio ${identity.audio_stream_id}`);
            if (Number.isFinite(Number(identity.part_size_bytes)) && Number(identity.part_size_bytes) > 0) fields.push(`${(Number(identity.part_size_bytes) / 1000000).toFixed(2)} MB`);
            return fields.join(' · ');
        });
        return [...new Set(labels)].sort().join('; ');
    }
    function fileTime(video) { return Math.round((Number(video.dataset.origin) || 0) + (video.currentTime || 0) * 1000); }
    function playBounded(video, start, end) {
        video.dataset.desired = start; const origin = Number(video.dataset.origin) || 0;
        if (video.dataset.preview === 'true' && (start < origin || end > origin + Number(video.dataset.previewDuration))) { message('This range is outside the current preview. Create another preview.', true); $(video.id === 'rpVideo' ? 'rpVideoPreview' : 'rpReferencePreview').hidden = false; return; }
        if (video._rpStop) video.removeEventListener('timeupdate', video._rpStop);
        video.pause(); video.currentTime = Math.max(0, (start - origin) / 1000);
        const stop = () => { if (fileTime(video) >= end || video.ended) { video.pause(); video.removeEventListener('timeupdate', stop); } };
        video._rpStop = stop; video.addEventListener('timeupdate', stop); video.play().catch(error => { video.removeEventListener('timeupdate', stop); fail(error); });
    }
    function setAudio(select, identities) {
        const ids = [...new Set((identities || []).map(i => i.audio_stream_id).filter(Boolean))]; select.replaceChildren(option('', 'Select audio'));
        ids.forEach(id => select.append(option(id, 'Audio ' + id))); if (ids.length === 1) select.value = ids[0];
        $(select.id === 'rpVideoAudio' ? 'rpVideoPreview' : 'rpReferencePreview').hidden = !select.value;
    }
    async function loadReference(id) {
        const request = ++referenceRequest, video = $('rpReferenceVideo'), audio = $('rpReferenceAudio');
        video.pause(); video.hidden = true; audio.replaceChildren(option('', 'Select audio')); audio.disabled = true;
        video.dataset.mediaId = id; video.dataset.duration = '0'; video.dataset.origin = '0'; video.dataset.preview = 'false'; video.dataset.desired = '0';
        $('rpReferenceOrigin').textContent = ''; $('rpReferencePreview').hidden = true;
        video.src = '/api/reprise/media/' + encodeURIComponent(id) + '/stream'; video.load();
        try {
            const result = await api('/api/reprise/media/' + encodeURIComponent(id) + '/result');
            if (request !== referenceRequest) return false;
            video.dataset.duration = result.duration_ms || '0'; setAudio(audio, result.identities); audio.disabled = false; video.hidden = false; return true;
        } catch (error) { if (request !== referenceRequest) return false; throw error; }
    }
    async function makePreview(video, audio, button, note) {
        const mediaId = video.dataset.mediaId, duration = Number(video.dataset.duration), desired = Number(video.dataset.desired || fileTime(video));
        if (!mediaId || !duration || !audio.value) { message('Select an audio track and a valid episode before creating a preview.', true); return; }
        const start = Math.max(0, Math.min(duration - 1, desired - 15000)), end = Math.min(duration, start + 90000); button.disabled = true; note.textContent = 'Preparing playable preview…';
        try { let job = await api('/api/reprise/media/' + encodeURIComponent(mediaId) + '/preview', {method:'POST',body:{start_ms:start,end_ms:end,audio_stream_id:audio.value}}); while (!['completed','complete','failed','cancelled'].includes(job.status)) { await new Promise(resolve => setTimeout(resolve, 1500)); if (video.dataset.mediaId !== mediaId) return; job = await api('/api/reprise/jobs/' + encodeURIComponent(job.id)); }
            if (video.dataset.mediaId !== mediaId) return;
            if (!job.preview || !job.preview.url || job.status === 'failed' || job.status === 'cancelled') throw new Error(job.error || 'Preview could not be created.'); const url = new URL(job.preview.url, location.origin); if (url.origin !== location.origin) throw new Error('Invalid preview address.');
            if (!Number.isInteger(job.preview.origin_ms) || !Number.isInteger(job.preview.duration_ms) || job.preview.duration_ms <= 0) throw new Error('Preview timing is unavailable.'); video.dataset.origin = String(job.preview.origin_ms); video.dataset.previewDuration = String(job.preview.duration_ms); video.dataset.preview = 'true'; video.src = url.pathname + url.search; video.addEventListener('loadedmetadata', () => { video.currentTime = Math.max(0, (desired - job.preview.origin_ms) / 1000); }, {once:true}); video.load(); note.textContent = 'Preview starts at ' + ms(job.preview.origin_ms) + ' on the original file timeline.'; button.hidden = true;
        } catch(error) { note.textContent = error.message; } finally { button.disabled = false; }
    }
    for (const id of ['rpVideo', 'rpReferenceVideo']) $(id).addEventListener('seeked', () => { $(id).dataset.desired = String(fileTime($(id))); });
    for (const [audioId, videoId, buttonId, noteId] of [['rpVideoAudio','rpVideo','rpVideoPreview','rpVideoOrigin'],['rpReferenceAudio','rpReferenceVideo','rpReferencePreview','rpReferenceOrigin']]) {
        $(audioId).addEventListener('change', () => {
            const video = $(videoId); video.dataset.desired = String(fileTime(video)); video.pause(); $(buttonId).hidden = !$(audioId).value || !video.dataset.mediaId;
            if (video.dataset.preview === 'true') { video.removeAttribute('src'); video.dataset.preview = 'false'; video.dataset.origin = '0'; video.load(); }
            $(noteId).textContent = $(audioId).value ? 'Create a preview to listen to the selected audio track.' : 'Select an audio track before creating a preview.';
        });
    }
    function selectOptions(select, values, all) { const previous = select.value; select.replaceChildren(option('', all)); values.forEach(v => select.append(option(String(v), String(v)))); if (values.some(v => String(v) === previous)) select.value = previous; }
    function filters(result) {
        if (Array.isArray(result.shows)) selectOptions($('rpShow'), result.shows, 'All shows');
        if (Array.isArray(result.seasons)) selectOptions($('rpSeason'), result.seasons, 'All seasons');
    }
    async function loadLibrary(append = false) {
        const seq = ++state.request; $('rpLibraryState').hidden = false; $('rpLibraryState').textContent = 'Loading library…';
        try {
            const q = new URLSearchParams({limit:'100'}); if (append && state.cursor) q.set('cursor', state.cursor); if ($('rpSearch').value.trim()) q.set('search', $('rpSearch').value.trim()); if ($('rpShow').value) q.set('show', $('rpShow').value); if ($('rpSeason').value) q.set('season', $('rpSeason').value);
            const result = await api('/api/reprise/media?' + q); if (seq !== state.request) return;
            if (!Array.isArray(result.items)) throw new Error('The library response was incomplete.');
            state.items = append ? state.items.concat(result.items) : result.items; state.cursor = result.next_cursor || null;
            filters(result); renderLibrary(); renderReferences();
        } catch (e) { if (seq === state.request) { $('rpLibraryState').textContent = e.message; $('rpLibraryState').hidden = false; } }
    }
    function renderLibrary() {
        const target = $('rpLibraryList'); clear(target); const q = $('rpSearch').value.trim().toLowerCase(), show = $('rpShow').value, season = $('rpSeason').value;
        const visible = state.items.filter(i => (!q || title(i).toLowerCase().includes(q)) && (!show || i.show_title === show) && (!season || String(i.season) === season));
        $('rpLibraryState').hidden = visible.length > 0; $('rpLibraryState').textContent = q || show || season ? 'No episodes match these filters.' : 'No episodes found. Refresh a configured library to begin.';
        for (const item of visible) {
            const row = el('div', null, 'rp-episode'), label = el('label');
            const version = versionLabel(item);
            const check = el('input'); check.type = 'checkbox'; check.checked = state.selected.has(item.id); check.setAttribute('aria-label', `Select ${title(item)}, ${version} for analysis`); check.addEventListener('change', () => { check.checked ? state.selected.add(item.id) : state.selected.delete(item.id); $('rpAnalyze').disabled = !state.selected.size; });
            const open = button('', () => openItem(item)); open.className = 'rp-episode-open';
            open.dataset.mediaId = item.id; if (state.item?.id === item.id) open.setAttribute('aria-current', 'true');
            const copy = el('span', null, 'rp-episode-copy'); copy.append(el('span', item.title || 'Untitled', 'rp-episode-name'));
            const episodeCode = item.season != null && item.episode != null ? `S${String(item.season).padStart(2, '0')}E${String(item.episode).padStart(2, '0')}` : '';
            const context = [item.show_title, episodeCode].filter(Boolean).join(' · ');
            if (context) copy.append(el('small', context, 'rp-episode-context'));
            copy.append(el('small', version, 'rp-episode-context'));
            const status = analysisLabel(item.analysis_status) + (item.freshness === 'stale' ? ' · Stale' : '');
            open.setAttribute('aria-label', `${title(item)} ${version} ${status}`); open.append(copy, el('span', status, 'rp-meta'));
            label.append(check); row.append(label, open); target.append(row);
        }
        $('rpMore').hidden = !state.cursor; $('rpAnalyze').disabled = !state.selected.size;
    }
    async function openItem(item, force = false) {
        if (!force && state.dirty && !confirm('Discard unsaved review changes?')) return;
        const request = ++reviewRequest;
        ++referenceRequest;
        state.item = item; state.result = null; state.edits = []; state.dirty = false; state.changes = [];
        document.querySelectorAll('.rp-episode-open').forEach(row => { if (row.dataset.mediaId === item.id) row.setAttribute('aria-current', 'true'); else row.removeAttribute('aria-current'); });
        $('rpReferenceVideo').pause(); $('rpReferenceVideo').removeAttribute('src'); $('rpReferenceVideo').dataset.mediaId = ''; $('rpReferenceVideo').hidden = true; $('rpReferencePreview').hidden = true; $('rpReference').value = ''; $('rpReferenceAudio').replaceChildren(option('', 'Select audio')); $('rpReferenceState').textContent = 'Choose an episode to compare.';
        $('rpReviewTitle').textContent = title(item); $('rpReviewVersion').textContent = versionLabel(item); $('rpReviewSubtitle').textContent = 'Loading episode…'; $('rpReviewEmpty').hidden = true; $('rpReview').hidden = true;
        $('rpVideo').pause(); $('rpVideoAudio').replaceChildren(option('', 'Select audio')); clear($('rpPublication'));
        $('rpVideo').dataset.mediaId = item.id; $('rpVideo').dataset.duration = item.duration_ms || '0'; $('rpVideo').dataset.origin = '0'; $('rpVideo').dataset.preview = 'false'; $('rpVideo').dataset.desired = '0'; $('rpVideoOrigin').textContent = ''; $('rpVideoPreview').hidden = true; $('rpVideo').src = '/api/reprise/media/' + encodeURIComponent(item.id) + '/stream'; $('rpVideoError').hidden = true; $('rpVideo').load();
        renderReferences(); clear($('rpRanges')); clear($('rpTimeline')); clear($('rpEvidence')); $('rpSave').disabled = true;
        try {
            const result = await api(`/api/reprise/media/${encodeURIComponent(item.id)}/result`); if (request !== reviewRequest) return;
            state.result = result; setAudio($('rpVideoAudio'), result.identities); state.edits = (result.ranges || [...(result.proposed_ranges || []), ...(result.protected_keep_ranges || []), ...(result.eligible_skip_ranges || [])]).filter((r,i,a) => a.findIndex(x => x.id === r.id) === i).map(r => ({...r, originalAction:r.action}));
            $('rpReviewSubtitle').textContent = (result.reasons || []).map(reason => reason === 'unapproved_or_unverified' ? 'No skips are ready for playback. Review the evidence below.' : /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/.test(reason) ? 'Review needs attention. See Evidence for details.' : reason).join(' · ') || 'Review this file before approving any ranges.'; renderReview(); $('rpReview').hidden = false;
            await loadHistory(item.id, request);
        } catch (e) { if (request !== reviewRequest) return; $('rpReviewSubtitle').textContent = e.message; $('rpAnalysisStatus').textContent = 'Unavailable'; $('rpFreshness').textContent = ''; fail(e); }
    }
    async function loadHistory(id, request) { if (!state.settings?.publication_available) return; try { const history = await api('/api/reprise/publications?media_version_id=' + encodeURIComponent(id)); if (request === reviewRequest && Array.isArray(history.items)) { state.changes = history.items; renderPublication(); } } catch (error) { if (request === reviewRequest) message('Publication history unavailable: ' + error.message, true); } }
    function renderReferences() {
        const select = $('rpReference'), previous = select.value; select.replaceChildren(option('', 'Choose an episode'));
        state.items.filter(i => i.id !== state.item?.id && (!state.item?.show_title || i.show_title === state.item.show_title)).forEach(i => select.append(option(i.id, title(i) + ' · ' + versionLabel(i))));
        if ([...select.options].some(o => o.value === previous)) select.value = previous;
    }
    function renderReview(rebuild = true) {
        const r = state.result; if (!r) return;
        $('rpAnalysisStatus').textContent = 'Analysis: ' + analysisLabel(r.analysis_status); $('rpFreshness').textContent = 'File: ' + (r.freshness || 'unavailable');
        const duration = Number(r.duration_ms || state.item?.duration_ms || 0); $('rpDuration').textContent = duration ? ms(duration) : 'Duration unavailable';
        const timeline = $('rpTimeline'); clear(timeline);
        for (const range of state.edits) if (duration && range.end_ms > range.start_ms) { const bar = el('span', null, `rp-timeline-segment${range.action === 'keep' ? ' keep' : ''}`); bar.style.left = `${Math.max(0, range.start_ms / duration * 100)}%`; bar.style.width = `${Math.max(0, Math.min(duration, range.end_ms) - range.start_ms) / duration * 100}%`; bar.title = `${range.action} ${ms(range.start_ms)}–${ms(range.end_ms)}`; timeline.append(bar); }
        for (const evidence of r.evidence || []) if (duration && evidence.end_ms > evidence.start_ms) { const bar = el('span', null, 'rp-timeline-segment evidence'); bar.style.left = (Math.max(0,evidence.start_ms) / duration * 100) + '%'; bar.style.width = (Math.max(0,Math.min(duration,evidence.end_ms)-evidence.start_ms) / duration * 100) + '%'; bar.title = (evidence.verdict || 'Evidence') + ' ' + ms(evidence.start_ms) + '–' + ms(evidence.end_ms); timeline.append(bar); }
        if (rebuild) {
        const list = $('rpRanges'); clear(list);
        if (!state.edits.length) list.append(el('p', r.analysis_status === 'complete' ? 'No ranges found. Nothing is approved for skipping.' : 'No review ranges are available.', 'rp-state'));
        state.edits.forEach(range => renderRange(list, range));
        const ev = $('rpEvidence'); clear(ev); const evidence = r.evidence || [];
        const reasonCodes = (r.reasons || []).filter(reason => typeof reason === 'string' && /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/.test(reason));
        if (reasonCodes.length) ev.append(el('p', 'Reason codes: ' + reasonCodes.join(', '), 'rp-note'));
        if (!evidence.length) ev.append(el('p', 'No evidence available.', 'rp-state')); else evidence.forEach(e => { const row = el('div', null, 'rp-range'); row.append(el('p', ms(e.start_ms) + '–' + ms(e.end_ms) + ' · ' + (e.modality || 'Evidence') + ' · ' + (e.verdict || 'unknown') + ' · ' + (e.reason || ''), 'rp-note')); if (e.reference_version_id && e.reference_start_ms != null && e.reference_end_ms != null) row.append(button('Compare this evidence', async () => { try { if (!await loadReference(e.reference_version_id)) return; $('rpReferenceState').textContent = 'Showing aligned evidence from the reference episode.'; playBounded($('rpVideo'), e.start_ms, e.end_ms); playBounded($('rpReferenceVideo'), e.reference_start_ms, e.reference_end_ms); } catch(error) { fail(error); } })); ev.append(row); });
        }
        $('rpSave').disabled = !state.dirty || r.result_revision == null || r.freshness !== 'current'; $('rpAddKeep').disabled = r.result_revision == null || r.freshness !== 'current'; $('rpSaveState').textContent = r.freshness !== 'current' ? 'This file is not current. Refresh its analysis before saving.' : state.dirty ? 'Unsaved changes' : 'Review saved separately from publication.';
        if (rebuild) renderPublication();
    }
    function renderRange(list, range) {
        let view = rangeViews.get(range);
        if (view) { list.append(view.box); return; }
        const box = el('details', null, 'rp-range rp-range-editor'), summary = el('summary'), name = el('strong'), bounds = el('span', null, 'rp-range-bounds'), decision = el('span', null, 'rp-note');
        box.open = !range.id && range.origin === 'manual';
        const updateSummary = () => { name.textContent = (range.action === 'keep' ? 'Keep' : 'Skip') + ' · ' + (range.kind || 'content'); bounds.textContent = ms(range.start_ms) + '–' + ms(range.end_ms); decision.textContent = (range.review_status || 'proposed') + (range.locked ? ' · Locked' : ''); };
        summary.append(name, bounds, decision); box.append(summary, el('p', range.reason || range.reason_code || 'Manual review', 'rp-note'));
        const changed = () => { const wasDirty = state.dirty; state.dirty = true; updateSummary(); renderReview(false); if (!wasDirty) renderPublication(); };
        const fields = el('div', null, 'rp-range-fields');
        function field(label, key, values) {
            const wrap = el('label', label), input = values ? el('select') : el('input');
            if (values) values.forEach(v => input.append(option(v, v))); else { input.type = 'number'; input.min = '0'; input.step = '1'; }
            input.value = range[key] ?? ''; input.addEventListener(values ? 'change' : 'input', () => { range[key] = values ? input.value : input.value === '' ? NaN : Number(input.value); changed(); }); wrap.append(input); fields.append(wrap);
        }
        field('Start (ms)', 'start_ms'); field('End (ms)', 'end_ms'); field('Action', 'action', range.id && range.originalAction !== 'keep' ? ['skip','keep'] : ['keep']); field('Type', 'kind', ['intro','credits','recap','preview']); box.append(fields);
        const controls = el('div', null, 'rp-range-options'), status = el('label', 'Decision'), select = el('select'); ['proposed','approved','rejected'].forEach(v => select.append(option(v,v))); select.value = range.review_status || 'proposed'; select.addEventListener('change', () => { range.review_status = select.value; changed(); }); status.append(select); controls.append(status);
        const locked = el('label'), check = el('input'); check.type = 'checkbox'; check.checked = !!range.locked; check.addEventListener('change', () => { range.locked = check.checked; changed(); }); locked.append(check, document.createTextNode(' Lock this range')); controls.append(locked);
        controls.append(button('Play range', () => playBounded($('rpVideo'), range.start_ms, range.end_ms)));
        if (range.origin === 'manual' && !range.id) controls.append(button('Remove', () => { state.edits = state.edits.filter(edit => edit !== range); state.dirty = true; renderReview(); $('rpAddKeep').focus(); }));
        box.append(controls); updateSummary(); rangeViews.set(range, {box}); list.append(box);
    }
    async function saveReview() {
        const duration = Number(state.result?.duration_ms || state.item?.duration_ms || 0);
        if (state.edits.some(r => !Number.isInteger(r.start_ms) || !Number.isInteger(r.end_ms) || r.start_ms < 0 || r.start_ms >= r.end_ms || (duration && r.end_ms > duration))) return message('Each range needs a valid start and end within the episode.', true);
        const edits = state.edits.map(r => ({range_id:r.id || null, action:r.action, kind:r.kind, start_ms:r.start_ms, end_ms:r.end_ms, review_status:r.review_status || 'proposed', locked:!!r.locked, reason:r.reason || ''}));
        try { $('rpSave').disabled = true; await api(`/api/reprise/media/${encodeURIComponent(state.item.id)}/review`, {method:'PATCH',body:{expected_revision:state.result.result_revision,edits}}); state.dirty = false; await openItem(state.item,true); message('Review saved. Nothing was published.'); } catch(e) { fail(e); $('rpSave').disabled = false; }
    }
    function renderPublication() {
        const root = $('rpPublication'); clear(root);
        const identities = state.result?.identities || state.item?.identities || []; if (!identities.length) { root.append(el('p','This file has no verified playback identity available for publication.','rp-state')); return; }
        const label = el('label','Playback version'), select = el('select'); identities.forEach((id,i) => select.append(option(String(i), `${id.rating_key} · media ${id.media_id} · part ${id.part_id} · audio ${id.audio_stream_id}`))); label.append(select); root.append(label);
        if (!state.settings?.publication_available || !state.settings?.writes_enabled) root.append(el('p','Plex publication is unavailable or disabled.','rp-note'));
        const prepare = button('Prepare publication', async () => { try { const result = await api('/api/reprise/publications/prepare',{method:'POST',body:{media_version_id:state.item.id,expected_revision:state.result.result_revision,identity:identities[Number(select.value)]}}); state.changes.unshift(result); renderPublication(); } catch(e){fail(e);} }); prepare.disabled = !state.settings?.publication_available || !state.settings?.writes_enabled || state.dirty || state.result?.result_revision == null; root.append(prepare);
        state.changes.forEach(change => { const row = el('div',null,'rp-job'); const info = el('div'); info.append(el('strong', `Publication ${change.status || 'unknown'}`), el('span', change.message || change.reason || '', 'rp-meta')); row.append(info);
            const summaryValid = [change.before_markers, change.after_markers].every(markers => Array.isArray(markers) && markers.every(marker => typeof marker.type === 'string' && marker.rating_key != null && Number.isInteger(marker.start_ms) && Number.isInteger(marker.end_ms)));
            if (summaryValid) { for (const [label, markers] of [['Current markers',change.before_markers],['Proposed markers',change.after_markers]]) { info.append(el('strong',label)); if (!markers.length) info.append(el('span','None','rp-meta')); else markers.forEach(marker => info.append(el('span',marker.type + ' · ' + ms(marker.start_ms) + '–' + ms(marker.end_ms) + ' · item ' + marker.rating_key,'rp-meta'))); } }
            if (change.status === 'prepared') { const apply = button('Apply', async () => { if (!confirm('Apply this prepared marker change to Plex?')) return; try { Object.assign(change, await api('/api/reprise/publications/' + encodeURIComponent(change.id) + '/apply',{method:'POST',body:{}})); renderPublication(); } catch(e){fail(e);} }); apply.disabled = !summaryValid; if (apply.disabled) info.append(el('span','Change details unavailable; apply is disabled.','rp-meta')); row.append(apply); }
            if (change.status === 'applying' || change.status === 'undoing') { info.append(el('span','This change was interrupted. Check its state without repeating it.','rp-meta')); row.append(button('Check state', async () => { try { Object.assign(change, await api('/api/reprise/publications/' + encodeURIComponent(change.id) + '/recover',{method:'POST',body:{}})); renderPublication(); } catch(e) { fail(e); } })); }
            if (change.status === 'uncertain') info.append(el('span','The result is uncertain. No change will be repeated automatically.','rp-meta'));
            if (change.status === 'applied') row.append(button('Undo', async () => { if (!confirm('Undo this published marker change?')) return; try { Object.assign(change, await api(`/api/reprise/publications/${encodeURIComponent(change.id)}/undo`,{method:'POST',body:{}})); renderPublication(); } catch(e){fail(e);} })); root.append(row); });
    }
    async function loadQueue() {
        const root = $('rpQueue');
        try {
            const data = await api('/api/reprise/jobs'), jobs = data.items;
            if (!Array.isArray(jobs)) throw new Error('The queue response was incomplete.');
            clear(root); if (!jobs.length) root.textContent = 'No work queued yet.';
            let refreshFinished = false;
            for (const job of jobs) {
                const terminal = terminalJob(job.status);
                if (job.operation === 'refresh') {
                    if (!terminal) activeRefreshes.add(job.id);
                    else if (activeRefreshes.delete(job.id)) refreshFinished = true;
                }
                const row = el('div', null, 'rp-job'), info = el('div');
                const operation = {analysis:'Analysis',preview:'Preview',refresh:'Library refresh'}[job.operation] || 'Work';
                const progress = typeof job.progress?.percent === 'number' ? ' · ' + job.progress.percent.toFixed(0) + '%' : '';
                const result = terminal && job.refresh ? ' · ' + job.refresh.registered + ' registered, ' + job.refresh.skipped + ' skipped' : '';
                info.append(el('strong', operation), el('span', (job.status || 'unknown') + (!terminal && job.pause_requested ? ' · Pause requested' : '') + progress + (!terminal && job.progress?.current_item ? ' · ' + job.progress.current_item : '') + result + (job.error ? ' · ' + job.error : ''), 'rp-meta')); row.append(info);
                const actions = terminal ? [] : job.pause_requested ? ['resume','cancel'] : job.status === 'running' ? ['pause','cancel'] : ['pending','queued'].includes(job.status) ? ['cancel'] : [];
                for (const action of actions) row.append(button(action[0].toUpperCase() + action.slice(1), async () => { try { await api('/api/reprise/jobs/' + encodeURIComponent(job.id) + '/' + action, {method:'POST',body:{}}); await loadQueue(); } catch(e) { fail(e); } }));
                root.append(row);
            }
            if (refreshFinished) { await loadLibrary(); message('Library refresh finished. See the work queue for its result.'); }
        } catch(e) { root.textContent = e.message; }
    }
    async function loadClients() {
        const root = $('rpClients'); try { const data = await api('/api/reprise/clients'); const clients = Array.isArray(data) ? data : data.clients; if (!Array.isArray(clients)) throw new Error('The clients response was incomplete.'); clear(root); if (!clients.length) { root.textContent = 'No API clients yet.'; return; }
            clients.forEach(client => { const row = el('div',null,'rp-client'), info = el('div'); info.append(el('strong',client.name),el('span',(client.scopes || []).join(', ') + (client.revoked ? ' · Revoked' : ''), 'rp-meta')); row.append(info); if (!client.revoked) { row.append(button('Rotate',async () => {if (!confirm('Rotate ' + client.name + "'s token? The old token will stop working.")) return; try { const result = await api('/api/reprise/clients/' + encodeURIComponent(client.id) + '/rotate',{method:'POST',body:{}}); showSecret(result); await loadClients(); } catch(e){fail(e);} })); row.append(button('Revoke',async () => {if (!confirm('Revoke ' + client.name + "'s token?")) return; try { await api('/api/reprise/clients/' + encodeURIComponent(client.id),{method:'DELETE'}); await loadClients(); } catch(e){fail(e);} })); } root.append(row); });
        } catch(e) { root.textContent = e.message; }
    }
    function showSecret(result) { if (!result.secret) throw new Error('The server did not return the new token.'); $('rpSecretValue').textContent = result.secret; $('rpSecret').hidden = false; state.secret = true; $('rpClientOpen').disabled = true; }
    $('rpClients').addEventListener('click', e => { if (state.secret && e.target.closest('button')) { e.preventDefault(); e.stopImmediatePropagation(); message('Copy and close the current token first.', true); } }, true);
    let searchTimer; $('rpSearch').addEventListener('input',() => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadLibrary(), 250); }); $('rpShow').addEventListener('change',() => { $('rpSeason').value = ''; loadLibrary(); }); $('rpSeason').addEventListener('change',() => loadLibrary()); $('rpMore').addEventListener('click',() => loadLibrary(true));
    $('rpRefresh').addEventListener('click',async () => { const form = $('rpRefreshForm'), select = $('rpLibraryChoice'); form.hidden = false; $('rpRefreshState').textContent = 'Loading configured libraries…'; try { const data = await api('/api/libraries'); state.libraries = (data.libraries || []).filter(l => l.server_id && l.server_type === 'plex'); select.replaceChildren(); state.libraries.forEach((lib,i) => select.append(option(String(i), (lib.server_name ? lib.server_name + ' · ' : '') + lib.name))); $('rpRefreshState').textContent = state.libraries.length ? '' : 'No enabled Plex libraries found. Add a connection first.'; } catch(e) { $('rpRefreshState').textContent = e.message; } });
    $('rpRefreshCancel').addEventListener('click',() => { $('rpRefreshForm').hidden = true; });
    $('rpRefreshForm').addEventListener('submit', async e => {
        e.preventDefault(); const lib = state.libraries[Number($('rpLibraryChoice').value)]; if (!lib) return;
        const submit = e.currentTarget.querySelector('[type="submit"]'); submit.disabled = true;
        try {
            $('rpRefreshState').textContent = 'Queueing library refresh…';
            const job = await api('/api/reprise/refresh', {method:'POST',body:{server_id:lib.server_id,library_id:String(lib.id)}});
            if (!job.id) throw new Error('The refresh response was incomplete.');
            activeRefreshes.add(job.id); $('rpRefreshForm').hidden = true; message('Library refresh queued. Progress appears in the work queue.'); await loadQueue();
        } catch(error) { $('rpRefreshState').textContent = error.message; } finally { submit.disabled = false; }
    });
    $('rpAnalyze').addEventListener('click',async () => { try { await api('/api/reprise/analyses',{method:'POST',body:{media_version_ids:[...state.selected]}}); state.selected.clear(); renderLibrary(); await loadQueue(); message('Analysis queued.'); } catch(e){fail(e);} });
    $('rpVideo').addEventListener('error',() => { $('rpVideoError').hidden = false; $('rpVideoPreview').hidden = false; }); $('rpReference').addEventListener('change',e => { const video = $('rpReferenceVideo'); if (!e.target.value) {++referenceRequest; $('rpReferenceAudio').replaceChildren(option('', 'Select audio')); $('rpReferenceAudio').disabled = true; $('rpReferencePreview').hidden = true; video.pause(); video.hidden = true; video.dataset.mediaId = ''; video.removeAttribute('src'); video.load(); $('rpReferenceState').textContent = 'Choose an episode to compare.'; return;} $('rpReferenceState').textContent = 'Reference time is shown on its own file timeline.'; loadReference(e.target.value).catch(fail); }); $('rpReferenceVideo').addEventListener('error',() => { $('rpReferenceState').textContent = 'This reference cannot be played directly by your browser.'; $('rpReferencePreview').hidden = false; });
    $('rpVideoPreview').addEventListener('click',() => makePreview($('rpVideo'), $('rpVideoAudio'), $('rpVideoPreview'), $('rpVideoOrigin'))); $('rpReferencePreview').addEventListener('click',() => makePreview($('rpReferenceVideo'), $('rpReferenceAudio'), $('rpReferencePreview'), $('rpReferenceOrigin')));
    $('rpSave').addEventListener('click',saveReview); $('rpDiscard').addEventListener('click',() => { if (state.item && (!state.dirty || confirm('Discard unsaved review changes?'))) openItem(state.item,true); }); $('rpAddKeep').addEventListener('click',() => { const duration = Number(state.result?.duration_ms || state.item?.duration_ms || 0), start = fileTime($('rpVideo')); state.edits.push({kind:'credits',action:'keep',start_ms:start,end_ms:Math.min(duration || start+1000,start+1000),review_status:'proposed',locked:true,origin:'manual',reason:'Protected by reviewer'}); state.dirty = true; renderReview(); });
    $('rpQueueRefresh').addEventListener('click',loadQueue); $('rpClientOpen').addEventListener('click',() => { $('rpClientForm').hidden = false; }); $('rpClientCancel').addEventListener('click',() => { $('rpClientForm').hidden = true; });
    $('rpClientForm').addEventListener('submit',async e => {e.preventDefault(); const form = e.currentTarget, scopes = ['results:read','jobs:read']; if (form.elements.analysis.checked) scopes.push('analysis:request'); try { const result = await api('/api/reprise/clients',{method:'POST',body:{name:form.elements.name.value.trim(),scopes}}); showSecret(result); form.reset(); form.hidden = true; await loadClients(); } catch(err){fail(err);} });
    $('rpSecretCopy').addEventListener('click',async () => { try { await navigator.clipboard.writeText($('rpSecretValue').textContent); message('Token copied.'); } catch(e){fail(e);} }); $('rpSecretClose').addEventListener('click',() => { if (!confirm('Hide this token? It cannot be shown again.')) return; $('rpSecret').hidden = true; $('rpSecretValue').textContent = ''; state.secret = false; $('rpClientOpen').disabled = false; });
    window.addEventListener('beforeunload',e => { if (state.dirty || state.secret) { e.preventDefault(); e.returnValue = ''; } });
    const railToggle = $('rpRailToggle'); const small = matchMedia('(max-width: 700px)'); function railState() { const expanded = small.matches ? document.body.classList.contains('rp-rail-open') : !document.body.classList.contains('rp-rail-collapsed'); railToggle.setAttribute('aria-expanded', String(expanded)); railToggle.setAttribute('aria-label', expanded ? 'Collapse navigation' : 'Expand navigation'); } railToggle.addEventListener('click', () => { document.body.classList.toggle(small.matches ? 'rp-rail-open' : 'rp-rail-collapsed'); railState(); }); small.addEventListener('change',railState); railState(); $('rpRailTheme').addEventListener('click',() => $('themeToggleBtn').click());
    document.querySelectorAll('.rp-rail-links a, .rp-rail-links button').forEach(control => { control.title = control.textContent.trim(); });
    function showSettings(settings) { state.settings = settings; $('rpWritesEnabled').checked = !!settings.writes_enabled; $('rpWritesEnabled').disabled = !settings.publication_available; $('rpWriteState').textContent = settings.publication_available ? (settings.writes_enabled ? 'Publication is enabled. Applying changes still requires confirmation.' : 'Publication is off. No Plex marker changes can be applied.') : 'Publication is unavailable until the required Plex connection is configured.'; if (state.result) { renderPublication(); loadHistory(state.item.id); } }
    $('rpWritesEnabled').addEventListener('change',async e => { const enabled = e.target.checked; if (enabled && !confirm('Allow reviewed Reprise changes to be published to Plex? Nothing is applied yet.')) {e.target.checked = false; return;} e.target.disabled = true; try { showSettings(await api('/api/reprise/settings',{method:'PATCH',body:{writes_enabled:enabled}})); } catch(error) { fail(error); try { showSettings(await api('/api/reprise/settings')); } catch(reloadError) { $('rpWriteState').textContent = reloadError.message; } } });
    loadLibrary(); loadQueue(); loadClients(); api('/api/reprise/settings').then(showSettings).catch(error => { $('rpWriteState').textContent = error.message; }); setInterval(() => { if (!document.hidden) loadQueue(); }, 10000);
})();
