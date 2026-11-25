/* Reprise · To approve.
 *
 * Everything Reprise found that waits for the owner, from /api/reprise/approvals. Two ways through it:
 *   1. One click per season: "Approve & send N sure ones" (only the High-confidence suggestions), with Undo.
 *   2. One by one for the rest: a start picture and an end picture, then Skip it, Keep it or Adjust (keys S, K, A),
 *      moving on by itself, with Undo (U). At the end, "Send N episodes to Plex".
 * The pictures and the nudge/snap controls come from reprise-clip.js; the colours from the Lucent theme layer. */
(function () {
  'use strict';

  const API = '/api/reprise';
  const root = document.getElementById('apRoot');
  if (!root) return;
  const Clip = window.RepriseClip;
  const $ = id => document.getElementById(id);
  const fmt = Clip.fmt;
  const queueEl = $('apQueue'), reviewEl = $('apReview'), noticeEl = $('apNotice'), subEl = $('apSub'), sentEl = $('apSent'), titleEl = $('apTitle');

  const state = {
    queue: null,        // GET /approvals
    plans: new Map(),   // season key -> GET /approvals/season (what each button would change in Plex)
    generation: 0,      // counts queue loads, so a late plan for an old queue is dropped
    sent: [],           // what this visit sent or approved, newest first, each with its Undo
    review: null,       // the one-by-one review in progress
  };

  /* ── small helpers ────────────────────────────────────────────────────────────────────────────────────────── */
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };
  const icon = name => { const i = el('i', 'bi bi-' + name); i.setAttribute('aria-hidden', 'true'); return i; };
  const plural = (n, one, many) => n + ' ' + (n === 1 ? one : many || one + 's');
  const keyOf = (show, season) => JSON.stringify([show, season]);
  const seasonUrl = (show, season) => '/approvals/season?show=' + encodeURIComponent(show) + '&season=' + season;
  const KIND = {
    intro: { one: 'intro', many: 'intros', ask: 'Skip this intro?' },
    credits: { one: 'credits', many: 'credits', ask: 'Skip these credits?' },
  };

  /* 1 min 3 s: a length in whole seconds, for a plain sentence. */
  function length(ms) {
    const total = Math.round(Math.abs(ms) / 1000);
    if (total < 60) return total + ' s';
    const minutes = Math.floor(total / 60), rest = total % 60;
    return minutes + ' min' + (rest ? ' ' + rest + ' s' : '');
  }
  const NOUN = { intro: ['intro', 'intros'], credits: ['set of credits', 'sets of credits'] };
  function kindsText(kinds) {
    const parts = ['intro', 'credits'].filter(k => kinds && kinds[k]).map(k => plural(kinds[k], NOUN[k][0], NOUN[k][1]));
    return parts.join(' and ');
  }

  function button(text, opts = {}) {
    const b = el('button', 'lu-btn' + (opts.cls ? ' ' + opts.cls : ''));
    b.type = 'button';
    if (opts.icon) b.append(icon(opts.icon));
    b.append(el('span', null, text));
    if (opts.key) b.append(el('kbd', 'lu-kbd', opts.key));
    if (opts.act) b.dataset.act = opts.act;
    return b;
  }
  function iconButton(name, label, act) {
    const b = el('button', 'lu-iconbtn');
    b.type = 'button'; b.title = label; b.setAttribute('aria-label', label);
    b.append(icon(name));
    if (act) b.dataset.act = act;
    return b;
  }
  function glyph(name, tone, extra) {
    const tile = el('span', 'lu-gt ' + (tone || '') + (extra ? ' ' + extra : ''));
    tile.setAttribute('aria-hidden', 'true');
    tile.append(icon(name));
    return tile;
  }

  async function api(path, body) {
    const response = await fetch(API + path, body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    let data = {};
    try { data = await response.json(); } catch (_) { /* an empty or non-JSON answer */ }
    if (!response.ok) {
      const said = data.error || data.message || '';
      const error = new Error(response.status === 401
        ? 'You were signed out. Reload this page to sign in again.'
        : /refresh and try again/i.test(said) ? 'This changed while you were looking at it.' : said || 'Something went wrong (' + response.status + ').');
      error.status = response.status;
      throw error;
    }
    return data;
  }

  /* Run a button's job once at a time; any failure becomes a plain message. */
  async function act(btn, job) {
    if (btn.getAttribute('aria-busy') === 'true') return;
    btn.setAttribute('aria-busy', 'true');
    try { await job(); } catch (error) { toast(error.message, { tone: 'bad' }); } finally { btn.removeAttribute('aria-busy'); }
  }

  let toastTimer = 0;
  function toast(text, opts = {}) {
    const box = $('apToasts');
    box.replaceChildren();
    const item = el('div', 'lu-toast');
    if (opts.tone === 'bad') { const mark = icon('exclamation-triangle'); mark.classList.add('lu-bad'); item.append(mark); }
    item.append(el('span', 'lu-grow', text));
    if (opts.undo) {
      const undoButton = el('button', 'lu-linkbtn', 'Undo');
      undoButton.type = 'button';
      undoButton.addEventListener('click', () => { item.remove(); opts.undo(); });
      item.append(undoButton);
    }
    box.append(item);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => item.remove(), opts.ms || (opts.tone === 'bad' ? 9000 : 6000));
  }

  function showView(view) {
    root.dataset.view = view;
    queueEl.hidden = view !== 'queue';
    reviewEl.hidden = view !== 'review';
  }
  const sendingOn = () => Boolean(state.queue && state.queue.sending && state.queue.sending.enabled);

  /* ═══ The queue ═══════════════════════════════════════════════════════════════════════════════════════════════ */
  async function loadQueue(opts = {}) {
    state.generation += 1;
    state.plans.clear();
    if (!opts.quiet) { queueEl.setAttribute('aria-busy', 'true'); renderSkeleton(); }
    try {
      state.queue = await api('/approvals');
      renderNotice();
      renderQueue();
    } catch (error) {
      renderLoadError(error);
    } finally {
      queueEl.setAttribute('aria-busy', 'false');
    }
  }

  function renderSkeleton() {
    subEl.textContent = 'Looking for what is waiting…';
    queueEl.replaceChildren(...[0, 1].map(() => {
      const panel = el('div', 'lu-panel');
      const rows = el('div', 'ap-skel');
      for (const width of ['38%', '72%', '54%']) { const line = el('span', 'lu-skel'); line.style.width = width; rows.append(line); }
      panel.append(rows);
      return panel;
    }));
  }

  function renderLoadError(error) {
    subEl.textContent = '';
    const banner = el('div', 'lu-banner lu-bad');
    const body = el('div', 'lu-col lu-grow');
    body.append(el('b', 'lu-h', 'Couldn’t load what is waiting'), el('span', 'lu-muted', error.message));
    const again = button('Try again', { icon: 'arrow-repeat' });
    again.addEventListener('click', () => act(again, () => loadQueue()));
    const actions = el('div', 'lu-actions'); actions.append(again);
    body.append(actions);
    banner.append(glyph('exclamation-triangle', 'lu-bad'), body);
    queueEl.replaceChildren(banner);
  }

  function renderNotice() {
    noticeEl.replaceChildren();
    const sending = state.queue.sending;
    if (!sending || sending.enabled) return;
    const banner = el('div', 'lu-banner lu-warn');
    const body = el('div', 'lu-col lu-grow');
    body.append(el('span', 'lu-h', sending.message));
    if (sending.available) {
      const link = el('a', 'lu-btn', 'Open Sharing');
      link.href = root.dataset.sharingUrl;
      const actions = el('div', 'lu-actions'); actions.append(link);
      body.append(actions);
    }
    banner.append(glyph('send-slash', 'lu-warn'), body);
    noticeEl.append(banner);
  }

  function renderQueue() {
    const queue = state.queue, totals = queue.totals;
    const parts = [];
    if (totals.sure) parts.push(plural(totals.sure, 'skip') + ' Reprise is sure about');
    if (totals.needs) parts.push(totals.needs + ' for you to look at');
    if (totals.ready) parts.push(plural(totals.ready, 'episode') + ' approved, not sent yet');
    subEl.textContent = parts.length ? parts.join(' · ') : 'Nothing is waiting for you.';
    pending.clear();
    if (!queue.shows.length) { queueEl.replaceChildren(emptyState()); return; }
    queueEl.replaceChildren(...queue.shows.map(showPanel));
  }

  function emptyState() {
    const box = el('div', 'lu-panel ap-empty lu-enter');
    const text = el('div', 'lu-col');
    text.append(el('h2', 'lu-title', 'Nothing is waiting for you'),
      el('p', 'lu-muted lu-measure', 'When Reprise finds intros or credits, they show up here for you to approve. Analyze a season in the Library to get some.'));
    const open = el('a', 'lu-btn lu-primary', 'Open the Library');
    open.href = root.dataset.libraryUrl;
    box.append(glyph('inbox', 'lu-acc', 'lu-big'), text, open);
    return box;
  }

  function showPanel(show) {
    const panel = el('section', 'lu-panel ap-show lu-enter');
    const head = el('div', 'lu-panel-head');
    head.append(el('h2', 'lu-title lu-ell lu-grow', show.show));
    if (show.seasons.length > 1) head.append(el('span', 'lu-cap lu-muted', plural(show.seasons.length, 'season')));
    const list = el('div', 'ap-seasons lu-divide');
    list.append(...show.seasons.map(seasonRow));
    panel.append(head, list);
    return panel;
  }

  /* What each season's buttons would change in Plex is read lazily, one season at a time, as it scrolls into view. */
  const slots = new Map();      // season key -> {sure, ready}: where its plan lines go
  const pending = new Set();    // season keys already asked for
  let planChain = Promise.resolve();
  const watcher = 'IntersectionObserver' in window
    ? new IntersectionObserver((entries, observer) => {
      for (const entry of entries) if (entry.isIntersecting) { observer.unobserve(entry.target); wantPlan(entry.target._season); }
    }, { rootMargin: '240px' })
    : null;

  function wantPlan(season) {
    const key = keyOf(season.show, season.season);
    if (pending.has(key)) return;
    pending.add(key);
    const generation = state.generation;
    planChain = planChain.then(async () => {
      if (generation !== state.generation) return;
      let plan = null, failure = null;
      try { plan = await api(seasonUrl(season.show, season.season)); } catch (error) { failure = error; }
      if (generation !== state.generation) return;
      if (plan) state.plans.set(key, plan);
      fillPlan(key, plan, failure);
    });
  }

  function fillPlan(key, plan, failure) {
    const where = slots.get(key);
    if (!where) return;
    for (const group of ['sure', 'ready']) {
      const slot = where[group];
      if (!slot) continue;
      slot.replaceChildren();
      slot.removeAttribute('aria-busy');
      const lines = [];
      if (failure) lines.push('Couldn’t check what Plex has now: ' + failure.message);
      else {
        lines.push(...plan.changes[group].lines);
        if (plan.plex.status !== 'read') lines.push('Plex could not be fully checked' + (plan.plex.message ? ': ' + plan.plex.message : '.'));
      }
      for (const line of lines) slot.append(el('li', null, line));
    }
  }

  function planSlot(key, group) {
    const list = el('ul', 'ap-plan');
    list.setAttribute('aria-busy', 'true');
    list.setAttribute('aria-label', 'What this changes in Plex');
    const line = el('li', 'ap-plan-wait'); line.append(el('span', 'lu-skel'));
    list.append(line);
    slots.get(key)[group] = list;
    return list;
  }

  function seasonRow(season) {
    const key = keyOf(season.show, season.season);
    if (!slots.has(key)) slots.set(key, {});
    const row = el('article', 'ap-season');
    row._season = season;
    const head = el('div', 'ap-season-head');
    head.append(el('h3', 'lu-h', season.label), el('span', 'lu-cap lu-muted', plural(season.episodes, 'episode')));
    row.append(head);
    if (!Number.isInteger(season.season)) {
      row.append(el('p', 'lu-cap lu-muted', 'These episodes have no season number, so they can’t be approved here yet.'));
      return row;
    }
    const sending = state.queue.sending;
    if (season.sure) {
      const detail = el('div', 'lu-col lu-grow');
      detail.append(el('b', 'lu-h', season.sure === 1 ? '1 sure one' : season.sure + ' sure ones'),
        el('span', 'lu-cap lu-muted', kindsText(season.kinds) + ', matched closely in other episodes'), planSlot(key, 'sure'));
      const go = button(sending.enabled ? 'Approve & send ' + season.sure + ' sure ' + (season.sure === 1 ? 'one' : 'ones') : 'Approve ' + season.sure + ' sure ' + (season.sure === 1 ? 'one' : 'ones'),
        { cls: 'lu-primary', icon: sending.enabled ? 'send-check' : 'check2-all' });
      go.addEventListener('click', () => act(go, () => approveSure(season)));
      row.append(doing('check2-circle', 'lu-pos', detail, go));
    }
    if (season.needs) {
      const detail = el('div', 'lu-col lu-grow');
      detail.append(el('b', 'lu-h', season.needs + ' to look at'), el('span', 'lu-cap lu-muted', 'Less certain. You decide each one.'));
      const look = button('Review ' + season.needs, { icon: 'eye' });
      look.addEventListener('click', () => act(look, () => startReview(season)));
      row.append(doing('eye', 'lu-warn', detail, look));
    }
    if (season.ready) {
      const detail = el('div', 'lu-col lu-grow');
      detail.append(el('b', 'lu-h', plural(season.ready, 'episode') + ' approved, not sent yet'), el('span', 'lu-cap lu-muted', 'These are yours, but Plex does not have them yet.'), planSlot(key, 'ready'));
      const send = button('Send ' + plural(season.ready, 'episode') + ' to Plex', { icon: 'send' });
      if (sending.enabled) send.addEventListener('click', () => act(send, () => sendApproved(season)));
      else { send.disabled = true; send.title = sending.message; }
      row.append(doing('send', 'lu-inf', detail, send));
    }
    if (watcher && (season.sure || season.ready)) { row._season = season; watcher.observe(row); } else if (season.sure || season.ready) wantPlan(season);
    return row;
  }

  function doing(name, tone, detail, action) {
    const line = el('div', 'ap-do');
    const what = el('div', 'ap-what');
    what.append(glyph(name, tone), detail);
    line.append(what, action);
    return line;
  }

  /* ── one click for the sure ones ──────────────────────────────────────────────────────────────────────────── */
  async function approveSure(season) {
    const result = await api('/approvals/send', {
      show: season.show, season: season.season, scope: 'sure', to_plex: sendingOn(),
    });
    remember(result, season);
    await loadQueue({ quiet: true });
  }

  async function sendApproved(season) {
    const result = await api('/approvals/send', { show: season.show, season: season.season, scope: 'approved', to_plex: true });
    remember(result, season);
    await loadQueue({ quiet: true });
  }

  /* ── what was just done, and its Undo ─────────────────────────────────────────────────────────────────────── */
  function skipCount(list) { return (list || []).reduce((n, entry) => n + (entry.kinds || []).length, 0); }

  function remember(result, season) {
    state.sent.unshift({ result, label: result.show + ' · ' + (season.label || 'Season ' + result.season), undone: null, busy: false });
    renderSent();
  }

  function sentView(entry) {
    const r = entry.result;
    const sentSkips = skipCount(r.sent), approvedSkips = skipCount(r.approved);
    const trouble = r.failed.length > 0 || Boolean(r.stopped);
    const view = { tone: 'lu-pos', mark: 'send-check', title: '', detail: [entry.label], more: [], canUndo: false };
    if (entry.undone) {
      view.mark = 'arrow-counterclockwise'; view.tone = entry.undone.failed.length ? 'lu-warn' : 'lu-pos';
      view.title = entry.undone.failed.length
        ? 'Put back ' + plural(entry.undone.restored.length, 'episode') + ', but ' + entry.undone.failed.length + ' could not be'
        : 'Undone';
      view.detail.push(entry.undone.restored.length ? 'Plex and your approvals are back as they were.' : 'Nothing was changed.');
      view.more = entry.undone.failed.map(f => f.label + ': ' + f.reason);
      view.canUndo = entry.undone.failed.length > 0;
      return view;
    }
    if (r.sent.length) {
      view.title = 'Sent ' + plural(sentSkips, 'skip') + ' to Plex';
      if (r.backup) view.detail.push('Plex was backed up first');
      if (trouble) { view.tone = 'lu-warn'; view.mark = 'exclamation-circle'; }
    } else if (r.approved.length) {
      view.title = 'Approved ' + plural(approvedSkips, 'skip');
      view.detail.push(r.failed.length ? 'Not sent' : (r.already || []).length ? 'Plex already has them' : 'Not sent: sending to Plex is turned off');
      view.mark = 'check2-all'; view.tone = trouble ? 'lu-warn' : 'lu-pos';
    } else if ((r.already || []).length && !r.failed.length) {
      view.title = 'Plex already has these skips'; view.detail.push('Nothing to send'); view.mark = 'check2-all';
    } else {
      view.title = 'Nothing was sent'; view.tone = 'lu-warn'; view.mark = 'exclamation-circle';
    }
    if (r.stopped) view.more.push('Sending stopped early: ' + r.stopped);
    const shown = r.failed.slice(0, 4).map(f => f.label + ': ' + f.reason);
    view.more.push(...shown);
    if (r.failed.length > shown.length) view.more.push('and ' + (r.failed.length - shown.length) + ' more');
    view.canUndo = r.sent.length > 0 || r.approved.length > 0;
    return view;
  }

  function renderSent() {
    sentEl.hidden = state.sent.length === 0;
    const rows = state.sent.map(entry => {
      const view = sentView(entry);
      const row = el('div', 'ap-sent-row');
      const text = el('div', 'lu-col lu-grow');
      text.append(el('b', 'lu-h', view.title), el('span', 'lu-cap lu-muted', view.detail.join(' · ')));
      if (view.more.length) { const list = el('ul', 'ap-fails'); for (const line of view.more) list.append(el('li', null, line)); text.append(list); }
      const actions = el('div', 'ap-sent-actions');
      if (view.canUndo) {
        const undo = button('Undo', { icon: 'arrow-counterclockwise' });
        undo.addEventListener('click', () => act(undo, () => undoSent(entry)));
        actions.append(undo);
      }
      const close = iconButton('x-lg', 'Dismiss');
      close.addEventListener('click', () => { state.sent.splice(state.sent.indexOf(entry), 1); renderSent(); });
      actions.append(close);
      row.append(glyph(view.mark, view.tone), text, actions);
      return row;
    });
    sentEl.replaceChildren(...rows);
  }

  async function undoSent(entry) {
    const r = entry.result;
    const unsent = (r.approved || []).filter(a => !r.sent.some(s => s.media_version_id === a.media_version_id));
    const changes = [...r.sent, ...unsent].map(c => ({
      media_version_id: c.media_version_id, change_id: c.change_id || null, span_ids: c.span_ids || [], label: c.label,
    }));
    const answer = await api('/approvals/undo', { changes });
    entry.undone = answer;
    if (answer.failed.length) {   // only what could not be put back stays undoable
      const left = new Set(answer.failed.map(f => f.media_version_id));
      r.sent = r.sent.filter(s => left.has(s.media_version_id));
      r.approved = (r.approved || []).filter(a => left.has(a.media_version_id));
    }
    renderSent();
    await loadQueue({ quiet: true });
  }

  /* ═══ The review: one by one ═══════════════════════════════════════════════════════════════════════════════════ */
  const startOf = item => (item.edit ? item.edit.start_ms : item.start_ms);
  const endOf = item => (item.edit ? item.edit.end_ms : item.end_ms);

  const SURENESS = {
    high: { tone: 'lu-pos', mark: 'patch-check', text: 'Very sure' },
    confirmed: { tone: 'lu-pos', mark: 'check2-all', text: 'Matched in other episodes' },
    medium: { tone: 'lu-warn', mark: 'question-circle', text: 'Fairly sure' },
    low: { tone: 'lu-warn', mark: 'exclamation-circle', text: 'A guess' },
  };
  const sureness = item => SURENESS[item.how_sure] || { tone: 'lu-inf', mark: 'info-circle', text: 'From ' + item.source };

  function plexNote(item) {
    const word = KIND[item.kind].one;
    if (item.change === 'same') return { mark: 'check2-circle', text: 'Plex already has this ' + word + ' skip in about the same place.' };
    if (item.change === 'new') return { mark: 'plus-circle', text: 'Plex has no ' + word + ' skip here yet.' };
    if (item.change === 'moved' && item.plex) {
      const moves = [];
      for (const [edge, delta] of [['start', item.start_delta_ms], ['end', item.end_delta_ms]]) {
        if (Math.abs(delta || 0) >= 1000) moves.push('the ' + edge + ' ' + length(delta) + (delta > 0 ? ' later' : ' earlier'));
      }
      return { mark: 'arrow-left-right', text: 'Plex has it at ' + fmt(item.plex.start_ms) + ' to ' + fmt(item.plex.end_ms) + '. This moves ' + (moves.join(' and ') || 'it a little') + '.' };
    }
    return { mark: 'question-circle', text: 'Couldn’t check what Plex has for this one.' };
  }

  async function startReview(season) {
    showView('review');
    renderReviewWait('Getting the ones that need you…');
    const key = keyOf(season.show, season.season);
    try {
      const plan = await api(seasonUrl(season.show, season.season));
      state.plans.set(key, plan);
      if (!plan.needs.length) { toast('Nothing is left to look at in ' + plan.label + '.'); leaveReview(false); return; }
      state.review = {
        show: plan.show, season: season.season, label: plan.label, items: plan.needs.map(i => ({ ...i, edit: null })),
        index: 0, history: [], busy: false, adjusting: false, active: 'start', clips: null,
        revs: new Map(plan.needs.map(i => [i.media_version_id, i.revision])),
      };
      history.pushState({ review: true }, '', '#review');
      renderReview();
    } catch (error) {
      toast(error.message, { tone: 'bad' });
      leaveReview(false);
    }
  }

  function renderReviewWait(text) {
    const box = el('div', 'lu-panel ap-wait');
    const line = el('span', 'lu-skel'); line.style.width = '46%';
    box.append(el('p', 'lu-muted', text), line);
    reviewEl.replaceChildren(box);
  }

  function leaveReview(fromButton) {
    if (fromButton && history.state && history.state.review) { history.back(); return; }   // popstate finishes the job
    teardownClips();
    state.review = null;
    reviewEl.replaceChildren();
    showView('queue');
    titleEl.focus({ preventScroll: true });
    loadQueue({ quiet: true });
  }
  window.addEventListener('popstate', () => { if (state.review || root.dataset.view === 'review') leaveReview(false); });

  function teardownClips() {
    const rv = state.review;
    if (rv && rv.clips) { rv.clips.start.destroy(); rv.clips.end.destroy(); rv.clips = null; }
  }

  function renderReview() {
    const rv = state.review;
    if (!rv) return;
    if (rv.index >= rv.items.length) { renderDone(); return; }
    teardownClips();
    const item = rv.items[rv.index], total = rv.items.length;

    const top = el('div', 'ap-top');
    const where = el('div', 'lu-col lu-grow');
    where.append(el('span', 'lu-h lu-ell', rv.show + ' · ' + rv.label), el('span', 'lu-cap lu-muted', 'Choice ' + (rv.index + 1) + ' of ' + total));
    const keys = iconButton('keyboard', 'Keyboard shortcuts', 'keys');
    keys.classList.add('ap-help'); keys.setAttribute('aria-expanded', 'false'); keys.setAttribute('aria-controls', 'apKeys');
    const undo = iconButton('arrow-counterclockwise', 'Undo the last choice', 'undo');
    undo.disabled = rv.history.length === 0;
    top.append(button('To approve', { cls: 'lu-quiet', icon: 'arrow-left', act: 'back' }), where, keys, undo);

    const bar = el('div', 'lu-bar');
    bar.setAttribute('role', 'progressbar'); bar.setAttribute('aria-label', 'Progress'); bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', String(total)); bar.setAttribute('aria-valuenow', String(rv.index));
    const fill = el('i'); fill.style.width = (rv.index / total) * 100 + '%'; bar.append(fill);

    const help = el('div', 'lu-panel ap-keys'); help.id = 'apKeys'; help.hidden = true;
    for (const [key, what] of [['S', 'Skip it'], ['K', 'Keep it'], ['A', 'Adjust the times'], ['Space', 'Play around the start or the end'],
      ['1 2', 'Pick the start or the end'], ['← →', 'Move the picked time 1 second (Adjust)'], ['C', 'Snap the picked time to a cut (Adjust)'],
      ['U', 'Undo the last choice'], ['Esc', 'Back to the list']]) {
      help.append(el('kbd', 'lu-kbd', key), el('span', 'lu-muted', what));
    }

    const card = el('article', 'lu-panel ap-card lu-enter');
    card.dataset.adjusting = String(rv.adjusting);
    const mark = sureness(item);
    const head = el('div', 'ap-card-head');
    const heading = el('h2', 'lu-title', item.label + (item.title ? ' · ' + item.title : ''));
    heading.id = 'apCardTitle'; heading.tabIndex = -1;
    const badges = el('div', 'ap-badges');
    badges.append(el('span', 'lu-badge lu-acc', KIND[item.kind].one === 'intro' ? 'Intro' : 'Credits'));
    const sure = el('span', 'lu-badge ' + mark.tone); sure.append(icon(mark.mark), mark.text);
    badges.append(sure);
    if (item.how_sure) badges.append(el('span', 'lu-cap lu-muted', 'Suggested by ' + item.source));
    head.append(heading, badges);
    card.append(head);
    if (item.reason) card.append(el('p', 'lu-muted ap-reason', item.reason));

    const clips = el('div', 'ap-clips');
    const boxes = {};
    for (const [side, title] of [['start', 'Skip starts'], ['end', 'Skip ends']]) {
      const box = el('div', 'ap-edge'); box.dataset.side = side;
      box.append(el('span', 'lu-h ap-edge-title', title));
      boxes[side] = el('div'); box.append(boxes[side]);
      box.addEventListener('pointerdown', () => pick(side));
      clips.append(box);
    }
    card.append(clips);
    const plex = plexNote(item);
    const note = el('p', 'lu-cap lu-muted ap-plex'); note.append(icon(plex.mark), plex.text);
    card.append(note);

    const tray = el('div', 'lu-tray ap-choose');
    tray.setAttribute('role', 'group'); tray.setAttribute('aria-label', 'Your choice');
    const row = el('div', 'lu-trow');
    const sum = el('div', 'lu-tsum');
    sum.append(el('b', null, KIND[item.kind].ask), ' ');
    const times = el('span', 'ap-times'); times.id = 'apTimes';
    sum.append(times);
    const buttons = el('div', 'ap-buttons');
    const adjust = button('Adjust', { cls: 'lu-big', icon: 'sliders', key: 'A', act: 'adjust' });
    adjust.setAttribute('aria-pressed', String(rv.adjusting));
    buttons.append(button('Skip it', { cls: 'lu-primary lu-big', icon: 'skip-forward-fill', key: 'S', act: 'skip' }),
      button('Keep it', { cls: 'lu-big', icon: 'shield-check', key: 'K', act: 'keep' }), adjust);
    row.append(sum, buttons);
    tray.append(row);

    reviewEl.replaceChildren(top, bar, help, card, tray);

    const make = (side, edge, originalMs, label) => Clip.create(boxes[side], {
      mediaId: item.media_version_id, durationMs: item.duration_ms, edgeMs: edge, originalMs, label,
      onChange: () => { pick(side); fit(); paintTimes(); },
    });
    rv.clips = { start: make('start', startOf(item), item.start_ms, 'Skip starts'), end: make('end', endOf(item), item.end_ms, 'Skip ends') };
    fit();
    pick(rv.active, true);
    paintTimes();
    if (rv.adjusting) for (const clip of Object.values(rv.clips)) clip.edit(true);
    heading.focus({ preventScroll: true });
    const next = rv.items[rv.index + 1];
    if (next) { Clip.prefetch(next.media_version_id, startOf(next)); Clip.prefetch(next.media_version_id, endOf(next)); }
  }

  /* The start can't pass the end, nor the end the start: each clip learns the other's limit. */
  function fit() {
    const rv = state.review;
    if (!rv || !rv.clips) return;
    const item = rv.items[rv.index];
    rv.clips.start.bounds(0, rv.clips.end.edge - 1000);
    rv.clips.end.bounds(rv.clips.start.edge + 1000, item.duration_ms);
  }
  function pick(side, quiet) {
    const rv = state.review;
    if (!rv) return;
    rv.active = side;
    for (const box of reviewEl.querySelectorAll('.ap-edge')) box.classList.toggle('is-active', box.dataset.side === side);
    if (!quiet && rv.clips) rv.clips[side].focus();
  }
  function paintTimes() {
    const rv = state.review, node = $('apTimes');
    if (!rv || !rv.clips || !node) return;
    const item = rv.items[rv.index], start = rv.clips.start.edge, end = rv.clips.end.edge;
    node.replaceChildren();
    const one = (ms, moved) => el('span', moved ? 'lu-acc' : null, fmt(ms));
    node.append(one(start, start !== item.start_ms), ' → ', one(end, end !== item.end_ms), ' · ' + length(end - start));
  }

  function setAdjusting(on) {
    const rv = state.review;
    if (!rv || !rv.clips) return;
    rv.adjusting = on;
    for (const clip of Object.values(rv.clips)) clip.edit(on);
    const adjust = reviewEl.querySelector('[data-act="adjust"]');
    if (adjust) adjust.setAttribute('aria-pressed', String(on));
    const card = reviewEl.querySelector('.ap-card');
    if (card) card.dataset.adjusting = String(on);
  }

  async function decide(decision) {
    const rv = state.review;
    if (!rv || rv.busy || rv.index >= rv.items.length || !rv.clips) return;
    const item = rv.items[rv.index], start = rv.clips.start.edge, end = rv.clips.end.edge;
    const body = {
      media_version_id: item.media_version_id, expected_revision: rv.revs.get(item.media_version_id), kind: item.kind, decision,
    };
    if (decision === 'skip') Object.assign(body, { start_ms: start, end_ms: end });
    rv.busy = true;
    reviewEl.classList.add('is-busy');
    try {
      const answer = await api('/approvals/decide', body);
      rv.revs.set(item.media_version_id, answer.revision);
      item.edit = decision === 'skip' ? { start_ms: start, end_ms: end } : null;
      rv.history.push({ index: rv.index, item, decision, undo: answer.undo });
      rv.index += 1;
      rv.adjusting = false;
      rv.active = 'start';
      const what = item.label + ' ' + KIND[item.kind].one;
      toast(decision === 'skip' ? what + ': skip approved' : what + ': kept', { undo: undoLast, ms: 7000 });
      renderReview();
    } catch (error) {
      if (error.status >= 400 && error.status < 500) {   // it changed since the list was made (another tab, a new analysis)
        toast(error.message + ' Getting what is left.', { tone: 'bad' });
        const again = { show: rv.show, season: rv.season, label: rv.label };
        teardownClips(); state.review = null;
        await startReview(again);
        return;
      }
      toast(error.message, { tone: 'bad' });
    } finally {
      if (state.review === rv) rv.busy = false;
      reviewEl.classList.remove('is-busy');
    }
  }

  async function undoLast() {
    const rv = state.review;
    if (!rv || rv.busy || !rv.history.length) return;
    const last = rv.history[rv.history.length - 1], id = last.item.media_version_id;
    rv.busy = true;
    reviewEl.classList.add('is-busy');
    try {
      const answer = await api('/approvals/decide', { media_version_id: id, expected_revision: rv.revs.get(id), ...last.undo });
      rv.revs.set(id, answer.revision);
      rv.history.pop();
      rv.index = last.index;
      const moved = Boolean(last.item.edit && (last.item.edit.start_ms !== last.item.start_ms || last.item.edit.end_ms !== last.item.end_ms));
      rv.adjusting = moved;   // what you adjusted is still there to change again
      if (!moved) last.item.edit = null;
      toast('Undone. Choose again.', { ms: 3500 });
      renderReview();
    } catch (error) {
      toast(error.message, { tone: 'bad' });
    } finally {
      if (state.review === rv) rv.busy = false;
      reviewEl.classList.remove('is-busy');
    }
  }

  /* ── all done: send what was approved ─────────────────────────────────────────────────────────────────────── */
  async function renderDone() {
    const rv = state.review;
    teardownClips();
    renderReviewWait('Checking what is ready to send…');
    let plan;
    try { plan = await api(seasonUrl(rv.show, rv.season)); } catch (error) {
      if (state.review !== rv || rv.index < rv.items.length) return;
      const box = el('div', 'lu-banner lu-bad');
      const body = el('div', 'lu-col lu-grow');
      body.append(el('b', 'lu-h', 'Couldn’t check what is ready to send'), el('span', 'lu-muted', error.message));
      const actions = el('div', 'lu-actions');
      const retry = button('Try again', { icon: 'arrow-repeat' }); retry.addEventListener('click', () => renderDone());
      const back = button('To approve', { cls: 'lu-quiet', icon: 'arrow-left', act: 'back' });
      actions.append(retry, back); body.append(actions);
      box.append(glyph('exclamation-triangle', 'lu-bad'), body);
      reviewEl.replaceChildren(box);
      return;
    }
    if (state.review !== rv || rv.index < rv.items.length) return;   // undone while this was loading
    state.plans.set(keyOf(rv.show, rv.season), plan);
    const skipped = rv.history.filter(h => h.decision === 'skip').length, kept = rv.history.filter(h => h.decision === 'keep').length;
    const waiting = plan.ready.length;

    const box = el('div', 'lu-panel ap-done lu-enter');
    const heading = el('h2', 'lu-title', 'All done with ' + rv.label); heading.tabIndex = -1;
    const said = [];
    if (skipped) said.push('skip ' + skipped);
    if (kept) said.push('keep ' + kept);
    const text = el('div', 'lu-col');
    text.append(heading, el('p', 'lu-muted', said.length ? 'You chose to ' + said.join(' and ') + '.' : 'Nothing was left to choose.'));
    box.append(glyph('check2-all', 'lu-pos', 'lu-big'), text);

    const actions = el('div', 'ap-done-actions');
    if (waiting) {
      const detail = el('div', 'lu-col lu-grow');
      detail.append(el('b', 'lu-h', plural(waiting, 'episode') + (waiting === 1 ? ' is' : ' are') + ' approved and not in Plex yet'));
      const plan_ = el('ul', 'ap-plan');
      for (const line of plan.changes.ready.lines) plan_.append(el('li', null, line));
      if (plan.plex.status !== 'read') plan_.append(el('li', null, 'Plex could not be fully checked' + (plan.plex.message ? ': ' + plan.plex.message : '.')));
      detail.append(plan_);
      const send = button('Send ' + plural(waiting, 'episode') + ' to Plex', { cls: 'lu-primary', icon: 'send' });
      if (plan.sending.enabled) {
        send.addEventListener('click', () => act(send, async () => {
          const result = await api('/approvals/send', { show: rv.show, season: rv.season, scope: 'approved', to_plex: true });
          remember(result, { label: rv.label });
          leaveReview(true);
        }));
      } else { send.disabled = true; send.title = plan.sending.message; detail.append(el('span', 'lu-cap lu-muted', plan.sending.message)); }
      box.append(doing('send', 'lu-inf', detail, send));
    }
    const back = button(waiting ? 'Not now' : 'Back to the list', { cls: waiting ? 'lu-quiet' : 'lu-primary', icon: 'arrow-left', act: 'back' });
    const undo = button('Undo the last choice', { cls: 'lu-quiet', icon: 'arrow-counterclockwise', act: 'undo' });
    undo.disabled = rv.history.length === 0;
    actions.append(back, undo);
    box.append(actions);
    reviewEl.replaceChildren(box);
    heading.focus({ preventScroll: true });
  }

  /* ── clicks and keys in the review ────────────────────────────────────────────────────────────────────────── */
  reviewEl.addEventListener('click', event => {
    const target = event.target.closest('[data-act]');
    if (!target || target.disabled) return;
    const rv = state.review;
    switch (target.dataset.act) {
      case 'back': leaveReview(true); break;
      case 'skip': decide('skip'); break;
      case 'keep': decide('keep'); break;
      case 'adjust': if (rv) setAdjusting(!rv.adjusting); break;
      case 'undo': undoLast(); break;
      case 'keys': {
        const panel = $('apKeys');
        if (!panel) break;
        panel.hidden = !panel.hidden;
        target.setAttribute('aria-expanded', String(!panel.hidden));
        break;
      }
      default: break;
    }
  });

  document.addEventListener('keydown', event => {
    const rv = state.review;
    if (!rv || root.dataset.view !== 'review' || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    const from = event.target;
    if (from.closest && from.closest('input, textarea, select, [contenteditable], .lu-thm-pop')) return;
    const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
    const finished = rv.index >= rv.items.length;
    if (key === 'Escape') {
      const panel = $('apKeys');
      if (panel && !panel.hidden) { panel.hidden = true; reviewEl.querySelector('[data-act="keys"]').setAttribute('aria-expanded', 'false'); } else leaveReview(true);
    } else if (key === 'u') {
      if (event.repeat) return;
      undoLast();
    } else if (finished) {
      return;
    } else if (key === 's' || key === 'k') {
      if (event.repeat) return;   // a held key must never approve several in a row
      decide(key === 's' ? 'skip' : 'keep');
    } else if (key === 'a') {
      if (event.repeat) return;
      setAdjusting(!rv.adjusting);
    } else if (key === ' ') {
      if (from.closest && from.closest('button, a')) return;   // Space on a focused button presses it
      if (rv.clips) rv.clips[rv.active].toggle();
    } else if (key === '1' || key === '2') {
      pick(key === '1' ? 'start' : 'end', true);
    } else if ((key === 'ArrowLeft' || key === 'ArrowRight') && rv.adjusting && rv.clips) {
      rv.clips[rv.active].nudge((key === 'ArrowLeft' ? -1 : 1) * (event.shiftKey ? 100 : 1000));
    } else if (key === 'c' && rv.adjusting && rv.clips) {
      rv.clips[rv.active].snap();
    } else if (key === '?') {
      const toggle = reviewEl.querySelector('[data-act="keys"]');
      if (toggle) toggle.click();
    } else return;
    event.preventDefault();
  });

  /* ═══ Start ═══════════════════════════════════════════════════════════════════════════════════════════════════ */
  if (window.RepriseTheme) window.RepriseTheme.mountPicker($('apTheme'));
  if (location.hash === '#review') history.replaceState(null, '', location.pathname + location.search);
  loadQueue();
})();
