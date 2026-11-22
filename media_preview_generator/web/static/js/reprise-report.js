/* Plex report: for one show or season, what Plex's skip markers say next to what Reprise found. The comparison is done by
   the server (GET /api/reprise/report); this page only shows it. Opening a row goes to that episode's review in the Library. */
(() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
    const option = (value, label) => { const n = el('option', label); n.value = value; return n; };
    const clear = node => node.replaceChildren();
    const plural = (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`;
    const state = { show: '', season: '', report: null, only: new Set(), request: 0 };

    /* Order, plain words and tone of each flag. Codes come from the server (media_preview_generator/reprise/report.py). */
    const FLAGS = [
        ['plex_cuts_into_keep', 'Skips into a scene you protected', 'danger'],
        ['plex_starts_early', 'Starts too early', 'danger'],
        ['plex_ends_late', 'Ends too late', 'danger'],
        ['failed', 'Analysis failed', 'danger'],
        ['plex_missing', 'Plex has no skip', 'warning'],
        ['unconfirmed', 'Not confirmed', 'warning'],
        ['plex_covers_unconfirmed', 'Covers something not confirmed', 'info'],
        ['one_of_a_kind', 'One of a kind', 'info'],
        ['stale', 'Out of date', 'warning'],
        ['not_analyzed', 'Not analyzed', 'warning'],
        ['published', 'Sent to Plex', 'positive'],
        ['published_auto', 'Sent to Plex automatically', 'positive'],
    ];
    const TONE = { problem: 'danger', attention: 'warning', info: 'info', good: 'positive' };
    const KIND = { intro: 'Intro', credits: 'Credits', recap: 'Recap', preview: 'Preview' };

    function clock(ms) {
        const total = Math.floor(ms / 1000), h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
        return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
    }
    /* A range such as "Credits 20:00–21:20" is never split across lines: a no-break space after the name and word joiners around the dash. */
    const span = r => `${clock(r.start_ms)}\u2060\u2013\u2060${clock(r.end_ms)}`;
    const spans = list => list.map(r => `${KIND[r.kind] || r.kind}\u00a0${span(r)}`).join(' \u00b7 ');

    function message(text) { const n = $('rpMessage'); n.textContent = text; n.hidden = !text; }
    async function api(path) {
        const response = await fetch(path);
        let data; try { data = await response.json(); } catch (_) { throw new Error(`Request failed (${response.status}).`); }
        if (!response.ok) throw new Error(data.error || data.message || `Request failed (${response.status}).`);
        return data;
    }
    function chip(text, tone) { const c = el('span', text, 'rp-chip'); if (tone) c.dataset.tone = tone; return c; }

    /* ── pickers ───────────────────────────────────────────────────────────────────────────────────── */
    async function loadShows() {
        const result = await api('/api/reprise/media?limit=1'), shows = Array.isArray(result.shows) ? result.shows : [];
        const pick = $('rpReportShow'); pick.replaceChildren(option('', shows.length ? 'Choose a show' : 'No shows yet'));
        shows.forEach(s => pick.append(option(s, s)));
        if (!shows.length) { setState('Reprise has not read any shows yet. Open the Library and choose Refresh library first.'); return; }
        if (!state.show && shows.length === 1) state.show = shows[0];
        if (state.show && !shows.includes(state.show)) { pick.append(option(state.show, state.show)); }
        pick.value = state.show;
        if (state.show) { await loadSeasons(); run(); }
    }
    async function loadSeasons() {
        const pick = $('rpReportSeason'); pick.replaceChildren(option('', 'All seasons')); pick.disabled = !state.show;
        if (!state.show) return;
        const result = await api('/api/reprise/media?' + new URLSearchParams({ limit: '1', show: state.show }));
        (result.seasons || []).forEach(s => pick.append(option(String(s), `Season ${s}`)));
        if (state.season && ![...pick.options].some(o => o.value === state.season)) pick.append(option(state.season, `Season ${state.season}`));
        pick.value = state.season;
    }
    function remember() {
        const q = new URLSearchParams(); if (state.show) q.set('show', state.show); if (state.show && state.season) q.set('season', state.season);
        history.replaceState(null, '', location.pathname + (q.size ? '?' + q : ''));
    }
    $('rpReportShow').addEventListener('change', async event => {
        state.show = event.target.value; state.season = ''; state.only.clear(); remember();
        try { await loadSeasons(); } catch (error) { message(error.message); }
        run();
    });
    $('rpReportSeason').addEventListener('change', event => { state.season = event.target.value; state.only.clear(); remember(); run(); });
    $('rpReportAgain').addEventListener('click', () => run());

    /* ── report ────────────────────────────────────────────────────────────────────────────────────── */
    function setState(text) { const box = $('rpReportState'); box.textContent = text; box.hidden = !text; }
    async function run() {
        const seq = ++state.request;
        $('rpReportAgain').disabled = !state.show; message('');
        if (!state.show) { state.report = null; $('rpReportSummary').hidden = true; $('rpReportResults').hidden = true; setState('Choose a show to compare Plex with Reprise.'); return; }
        $('rpReportSummary').hidden = true; $('rpReportResults').hidden = true; setState('Reading Plex and Reprise\u2026');
        try {
            const q = new URLSearchParams({ show: state.show }); if (state.season) q.set('season', state.season);
            const report = await api('/api/reprise/report?' + q); if (seq !== state.request) return;
            state.report = report; render();
        } catch (error) {
            if (seq !== state.request) return;
            state.report = null; setState(''); message(error.message || 'The report could not be made.');
        }
    }

    function counts(report) { return report.summary.counts || {}; }
    function renderSummary() {
        const report = state.report, s = report.summary;
        $('rpReportHeadline').textContent = s.headline;
        const parts = [plural(s.episodes, 'episode'), `${s.analyzed} analyzed`];
        if (report.plex.status === 'partly') parts.push('some episodes could not be read from Plex');
        $('rpReportSub').textContent = parts.join(' \u00b7 ') + (report.plex.status === 'unavailable' ? '' : '. Plex was read just now.');
        const box = $('rpReportFilters'); clear(box);
        const c = counts(report), shown = FLAGS.filter(f => c[f[0]]);
        shown.forEach(([code, label, tone]) => {
            const b = el('button', null, 'rp-chip rp-report-filter'); b.type = 'button'; b.dataset.tone = tone;
            b.setAttribute('aria-pressed', String(state.only.has(code)));
            b.append(el('span', label), el('b', String(c[code])));
            b.addEventListener('click', () => { if (state.only.has(code)) state.only.delete(code); else state.only.add(code); render(); });
            box.append(b);
        });
        if (state.only.size) {
            const all = el('button', 'Show every episode', 'rp-btn quiet'); all.type = 'button';
            all.addEventListener('click', () => { state.only.clear(); render(); }); box.append(all);
        }
        box.hidden = !shown.length;
        $('rpReportSummary').hidden = false;
    }

    function timeLine(label, text, muted) {
        const row = el('div'); row.append(el('dt', label), el('dd', text, muted ? 'rp-muted' : null)); return row;
    }
    function rowLink(row) {
        const q = new URLSearchParams({ show: row.show_title, open: row.id });
        if (row.season != null) q.set('season', String(row.season));
        return '/reprise?' + q;
    }
    function episodeRow(row) {
        const li = el('li'), a = el('a', null, 'rp-report-row'); a.href = rowLink(row);
        const main = el('div', null, 'rp-report-main');
        main.append(el('strong', [row.label, row.title].filter(Boolean).join(' \u00b7 ') || 'Episode', 'rp-episode-name'));
        const flags = el('div', null, 'rp-report-flags');
        row.flags.forEach(f => flags.append(chip(f.text, TONE[f.severity])));
        const quiet = !row.flags.length;
        if (quiet && row.analysis.status === 'running') flags.append(chip('Analyzing now', 'busy'));
        else if (quiet && row.verdict === 'ok') flags.append(chip('Plex and Reprise agree', 'positive'));
        else if (quiet) main.append(el('p', row.summary, 'rp-report-sentence'));  /* the chips already say it when there are flags */
        if (flags.hasChildNodes()) main.append(flags);
        if (row.flags.some(f => f.code === 'failed') && row.analysis.reasons.length) main.append(el('p', row.analysis.reasons[0], 'rp-note'));
        const times = el('dl', null, 'rp-report-times');
        times.append(timeLine('Plex', row.plex.status === 'unavailable' ? 'Could not be read' : (row.plex.markers.length ? spans(row.plex.markers) : 'No skips'), !row.plex.markers.length));
        const r = row.reprise;
        const found = r.skips.length ? spans(r.skips) : (r.suggestions.length ? 'Suggestions only: ' + spans(r.suggestions) : '');
        times.append(timeLine('Reprise', found || (row.analysis.status === 'complete' && row.analysis.freshness === 'current' ? 'Nothing found' : 'No result'), !found));
        if (r.keeps.length) times.append(timeLine('Protected', r.keeps.map(span).join(' \u00b7 ')));
        const chevron = el('i', null, 'bi bi-chevron-right'); chevron.setAttribute('aria-hidden', 'true');
        a.append(main, times, chevron);
        li.append(a); return li;
    }
    function render() {
        const report = state.report; if (!report) return;
        renderSummary();
        const rows = report.episodes.filter(row => !state.only.size || row.flags.some(f => state.only.has(f.code)));
        const list = $('rpReportList'); clear(list);
        rows.forEach(row => list.append(episodeRow(row)));
        $('rpReportResults').hidden = !rows.length;
        setState(rows.length ? '' : 'No episodes match this filter.');
        if (!report.episodes.length) setState('Reprise has no episodes for this choice.');
    }

    const params = new URLSearchParams(location.search);
    state.show = (params.get('show') || '').slice(0, 200);
    state.season = /^\d{1,4}$/.test(params.get('season') || '') ? params.get('season') : '';
    loadShows().catch(error => { setState(''); message(error.message || 'The shows could not be loaded.'); });
})();
