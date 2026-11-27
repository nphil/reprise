/* Reprise · Review a season or fix a skip for one exact media version. */
(function () {
  'use strict';

  const API = '/api/reprise';
  const root = document.getElementById('apRoot');
  if (!root) return;
  const Clip = window.RepriseClip;
  const $ = id => document.getElementById(id);
  const fmt = Clip.fmt;
  const queueEl = $('apQueue'), reviewEl = $('apReview'), noticeEl = $('apNotice'), subEl = $('apSub'), sentEl = $('apSent'), titleEl = $('apTitle');

  const state = { sent: [], review: null, route: 0, sending: false };

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
  const KIND = { intro: 'Intro', credits: 'Credits' };

  function scopeHref(scope, hash = '') {
    const params = new URLSearchParams();
    if (scope.show && Number.isInteger(scope.season)) {
      params.set('show', scope.show);
      params.set('season', scope.season);
    }
    if (scope.episode) params.set('episode_id', scope.episode);
    if (scope.kind) params.set('kind', scope.kind);
    const query = params.toString();
    return location.pathname + (query ? '?' + query : '') + hash;
  }

  function libraryLink(scope = {}) {
    const url = new URL(root.dataset.libraryUrl, location.origin);
    if (scope.show) url.searchParams.set('show', scope.show);
    if (Number.isInteger(scope.season)) url.searchParams.set('season', scope.season);
    if (scope.episode) {
      url.searchParams.set('open', scope.episode);
      if (scope.kind) url.searchParams.set('kind', scope.kind);
      url.hash = 'fix';
    }
    const link = el('a', 'lu-btn', scope.episode ? 'Fix in Library' : 'Open Library');
    link.href = url.pathname + url.search + url.hash;
    return link;
  }

  function viewLink(text, href, opts = {}) {
    const link = el('a', opts.cls || 'lu-btn');
    link.href = href;
    if (opts.icon) link.append(icon(opts.icon));
    link.append(el('span', null, text));
    link.addEventListener('click', event => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      const here = location.pathname + location.search + location.hash;
      if (here === href) return;
      if (opts.back && history.state?.reviewFrom === href) { history.back(); return; }
      if (opts.back) history.replaceState(null, '', href);
      else history.pushState({ reviewFrom: here }, '', href);
      loadRoute();
    });
    return link;
  }

  /* 1 min 3 s: a length in whole seconds, for a plain sentence. */
  function length(ms) {
    const total = Math.round(Math.abs(ms) / 1000);
    if (total < 60) return total + ' s';
    const minutes = Math.floor(total / 60), rest = total % 60;
    return minutes + ' min' + (rest ? ' ' + rest + ' s' : '');
  }
  function counts(sure, needs, ready) {
    return [
      sure ? plural(sure, 'high-confidence skip') : '',
      needs ? plural(needs, 'skip') + ' to check' : '',
      ready ? plural(ready, 'episode') + ' ready to send' : '',
    ].filter(Boolean).join(' · ');
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
    reviewEl.hidden = view === 'queue';
  }

  /* ═══ The queue ═══════════════════════════════════════════════════════════════════════════════════════════════ */
  async function loadQueue(route) {
    queueEl.setAttribute('aria-busy', 'true');
    renderSkeleton();
    try {
      const queue = await api('/approvals');
      if (route !== state.route) return;
      renderNotice(queue.sending);
      renderQueue(queue);
    } catch (error) {
      if (route === state.route) renderLoadError(error);
    } finally {
      if (route === state.route) queueEl.setAttribute('aria-busy', 'false');
    }
  }

  function renderSkeleton() {
    subEl.textContent = 'Loading seasons…';
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
    body.append(el('b', 'lu-h', 'Couldn’t load Review'), el('span', 'lu-muted', error.message));
    const again = button('Try again', { icon: 'arrow-repeat' });
    again.addEventListener('click', () => loadRoute());
    const actions = el('div', 'lu-actions'); actions.append(again);
    body.append(actions);
    banner.append(glyph('exclamation-triangle', 'lu-bad'), body);
    queueEl.replaceChildren(banner);
  }

  function renderNotice(sending) {
    noticeEl.replaceChildren();
    if (!sending || sending.enabled) return;
    const note = el('p', 'ap-sending-note lu-muted', sending.available
      ? 'Review only · Approvals stay in Reprise.'
      : 'Review only · Sending to Plex is unavailable on this install.');
    if (sending.available) {
      const link = el('a', 'lu-btn lu-quiet', 'Sharing');
      link.href = root.dataset.sharingUrl;
      note.append(link);
    }
    noticeEl.append(note);
  }

  function renderQueue(queue) {
    const { sure, needs, ready } = queue.totals;
    subEl.textContent = counts(sure, needs, ready);
    if (!queue.shows.length) { queueEl.replaceChildren(emptyState()); return; }
    queueEl.replaceChildren(...queue.shows.map(showPanel));
  }

  function emptyState() {
    const box = el('div', 'lu-panel ap-empty lu-enter');
    const text = el('div', 'lu-col');
    text.append(el('h2', 'lu-title', 'Nothing to review'),
      el('p', 'lu-muted lu-measure', 'Analyze a season in Library to find intros and credits.'));
    const open = libraryLink();
    open.classList.add('lu-primary');
    box.append(glyph('inbox', 'lu-acc', 'lu-big'), text, open);
    return box;
  }

  function showPanel(show) {
    const panel = el('section', 'lu-panel ap-show');
    const head = el('div', 'lu-panel-head');
    head.append(el('h2', 'lu-title lu-grow', show.show));
    if (show.seasons.length > 1) head.append(el('span', 'lu-cap lu-muted', plural(show.seasons.length, 'season')));
    const list = el('div', 'ap-seasons lu-divide');
    list.append(...show.seasons.map(seasonRow));
    panel.append(head, list);
    return panel;
  }

  function seasonRow(season) {
    const row = el('article', 'ap-season');
    const detail = el('div', 'lu-col');
    detail.append(el('h3', 'lu-h', season.label),
      el('p', 'lu-muted lu-cap', counts(season.sure, season.needs, season.ready)));
    row.append(detail);
    if (Number.isInteger(season.season)) {
      const open = viewLink('Review season', scopeHref(season), { icon: 'chevron-right' });
      open.setAttribute('aria-label', 'Review season: ' + season.show + ', ' + season.label);
      row.append(open);
    } else {
      detail.append(el('p', 'lu-cap lu-muted', 'Open these episodes in Library to review them.'));
      row.append(libraryLink(season));
    }
    return row;
  }

  function backToQueue() {
    return viewLink('All seasons', scopeHref({}), { back: true, icon: 'arrow-left' });
  }

  function planDetails(plan, group) {
    const details = el('details', 'ap-plan');
    details.append(el('summary', null, 'What changes in Plex'));
    const lines = [...plan.changes[group].lines];
    if (plan.plex.status !== 'read') lines.push('Plex’s current timings could not be fully checked.');
    if (lines.length) {
      const list = el('ul');
      for (const line of lines) list.append(el('li', null, line));
      details.append(list);
    }
    details.hidden = lines.length === 0;
    return details;
  }

  async function sendSeason(plan, scope, toPlex) {
    const result = await api('/approvals/send', { show: plan.show, season: plan.season, scope, to_plex: toPlex });
    remember(result, plan);
  }

  function sendButton(plan, scope, primary, after) {
    const sure = scope === 'sure';
    const toPlex = !sure || plan.sending.enabled;
    const text = sure
      ? (toPlex ? 'Approve & send ' : 'Approve ') + plural(plan.sure.length, 'high-confidence skip')
      : 'Send skips for ' + plural(plan.ready.length, 'episode') + ' to Plex';
    const send = button(text, { cls: primary ? 'lu-primary' : 'lu-quiet', icon: toPlex ? 'send' : 'check2-all' });
    send.disabled = !sure && !plan.sending.enabled;
    send.addEventListener('click', () => act(send, async () => {
      if (state.sending) return;
      const route = state.route;
      state.sending = true;
      try { await sendSeason(plan, scope, toPlex); }
      finally { state.sending = false; }
      if (route === state.route) after();
    }));
    return send;
  }

  function backToSeason(scope) {
    return viewLink('Back to season', scopeHref({ show: scope.show, season: scope.season }), { back: true, icon: 'arrow-left' });
  }

  function renderSeason(plan, scopedEpisode) {
    const top = el('div', 'ap-top');
    top.append(scopedEpisode ? backToSeason(plan) : backToQueue());
    const panel = el('section', 'lu-panel ap-season-work');
    const heading = el('h2', 'lu-title', plan.show + ' · ' + plan.label); heading.tabIndex = -1;
    panel.append(heading);
    if (scopedEpisode) {
      panel.append(el('p', 'lu-muted', 'No pending skip matches this episode. Open it in Library to check saved timings or undo an earlier send.'), libraryLink(scopedEpisode));
    } else if (!plan.sure.length && !plan.needs.length && !plan.ready.length) {
      panel.append(el('p', 'lu-muted', 'Nothing to review in this season.'), libraryLink(plan));
    }
    if (!scopedEpisode) {
      function step(label, action, details) {
        const row = el('div', 'ap-season ap-step');
        const text = el('div', 'lu-col');
        text.append(el('h3', 'lu-h', label));
        if (details) text.append(details);
        row.append(text, action);
        panel.append(row);
      }
      if (plan.needs.length) {
        step('Check the suggested timings', viewLink('Review ' + plural(plan.needs.length, 'skip'), scopeHref(plan, '#review'),
          { cls: 'lu-btn lu-primary', icon: 'eye' }));
      }
      if (plan.sure.length) {
        step('High-confidence suggestions', sendButton(plan, 'sure', !plan.needs.length, loadRoute), planDetails(plan, 'sure'));
      }
      if (plan.ready.length) {
        step('Approved, not sent', sendButton(plan, 'approved', !plan.needs.length && !plan.sure.length, loadRoute), planDetails(plan, 'ready'));
      }
      const candidates = [...plan.needs, ...plan.sure];
      if (candidates.length) {
        const details = el('details', 'ap-episode-links');
        details.append(el('summary', null, 'Fix a specific skip'));
        const list = el('ul');
        for (const item of candidates) {
          const li = el('li');
          li.append(viewLink(item.label + (item.title ? ' · ' + item.title : '') + ' · ' + KIND[item.kind],
            scopeHref({ ...plan, episode: item.media_version_id, kind: item.kind }, '#fix'), { cls: 'lu-btn lu-quiet' }));
          list.append(li);
        }
        details.append(list);
        panel.append(details);
      }
    }
    reviewEl.replaceChildren(top, panel);
    heading.focus({ preventScroll: true });
  }

  /* ── what was just done, and its Undo ─────────────────────────────────────────────────────────────────────── */
  function skipCount(list) { return (list || []).reduce((n, entry) => n + (entry.kinds || []).length, 0); }

  function remember(result, season) {
    state.sent.unshift({ result, label: result.show + ' · ' + (season.label || 'Season ' + result.season), undone: null, busy: false });
    renderSent();
  }

  function sentView(entry) {
    const r = entry.result;
    const approvedSkips = skipCount(r.approved);
    const trouble = r.failed.length > 0 || Boolean(r.stopped);
    const view = { tone: 'lu-pos', mark: 'send-check', title: '', detail: [entry.label], more: [], canUndo: false };
    if (entry.undone) {
      view.mark = 'arrow-counterclockwise'; view.tone = entry.undone.failed.length ? 'lu-warn' : 'lu-pos';
      view.title = entry.undone.failed.length
        ? 'Restored ' + plural(entry.undone.restored.length, 'episode') + '; ' + plural(entry.undone.failed.length, 'episode') + ' could not be restored'
        : 'Undone';
      view.more = entry.undone.failed.map(f => f.label + ': ' + f.reason);
      view.canUndo = entry.undone.failed.length > 0;
      return view;
    }
    if (entry.auto) {   // sent by Reprise itself, one row for a whole season: there is only ever a send to undo
      const counts = { intro: 0, credits: 0 }, words = { intro: ['intro', 'intros'], credits: ['ending', 'endings'] };
      for (const sent of r.sent) for (const kind of sent.kinds) counts[kind] += 1;
      view.title = 'Sent ' + ['intro', 'credits'].filter(kind => counts[kind]).map(kind => plural(counts[kind], ...words[kind])).join(' and ') + ' to Plex automatically';
      if (r.sent.length > 1) view.detail.push(plural(r.sent.length, 'episode'));
      view.detail.push(whenSent(entry.auto.sent_at));
      view.fresh = entry.auto.unread > 0;
      view.canUndo = true;
      return view;
    }
    if (r.sent.length) {
      view.title = 'Sent skips for ' + plural(r.sent.length, 'episode') + ' to Plex';
      if (r.backup) view.detail.push('Plex was backed up first');
      if (trouble) { view.tone = 'lu-warn'; view.mark = 'exclamation-circle'; }
    } else if (r.approved.length) {
      view.title = 'Approved ' + plural(approvedSkips, 'skip');
      view.detail.push(r.failed.length ? 'Not sent' : (r.already || []).length ? 'Plex already has them' : 'Not sent · Review only');
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

  /* "today", "yesterday", "3 days ago": when Reprise sent something, for a row's small print. */
  function whenSent(seconds) {
    const days = Math.floor((Date.now() / 1000 - seconds) / 86400);
    return days <= 0 ? 'today' : days === 1 ? 'yesterday' : days + ' days ago';
  }

  /* The list is kept on the server, so a row cleared here is gone on every screen. Nothing here touches Plex. */
  const autoIds = entries => entries.filter(entry => entry.auto).flatMap(entry => entry.auto.change_ids);
  async function clearNotices(entries, everything) {
    const ids = autoIds(entries);
    state.sent = state.sent.filter(entry => !entries.includes(entry));
    renderSent();
    if (ids.length) {
      try { await api('/approvals/automatic/notices', everything ? { action: 'dismiss', all: true } : { action: 'dismiss', change_ids: ids.slice(0, 500) }); }
      catch (error) { toast(error.message + ' It will be back next time.', { tone: 'bad' }); await loadAutomatic(); return false; }
    }
    return true;
  }
  async function clearAll() {
    const count = state.sent.length;
    if (await clearNotices([...state.sent], true)) toast('Cleared ' + plural(count, 'notice') + '. Nothing was changed in Plex.');
  }
  async function markAllRead() {
    await api('/approvals/automatic/notices', { action: 'read', all: true });
    for (const entry of state.sent) if (entry.auto) entry.auto.unread = 0;
    renderSent();
  }

  function sentHead() {
    const fresh = state.sent.filter(entry => entry.auto && entry.auto.unread > 0).length;
    const head = el('div', 'ap-sent-head');
    const title = el('h2', 'lu-h lu-grow', 'Recent changes');
    title.id = 'automatic-sends';
    title.tabIndex = -1;
    if (state.sent.length) title.append(el('span', 'lu-cap lu-muted', plural(state.sent.length, 'notice') + (fresh ? ' · ' + fresh + ' new' : '')));
    const actions = el('div', 'ap-sent-tools');
    if (fresh) {
      const read = button('Mark all as read', { cls: 'lu-quiet', icon: 'check2-all' });
      read.addEventListener('click', () => act(read, markAllRead));
      actions.append(read);
    }
    if (state.sent.length) {
      const clear = button('Clear all', { cls: 'lu-quiet', icon: 'x-circle' });
      clear.addEventListener('click', () => act(clear, clearAll));
      actions.append(clear);
    }
    head.append(title, actions);
    return head;
  }

  function renderSent(emptyMessage = 'No recent changes.') {
    sentEl.hidden = state.sent.length === 0 && location.hash !== '#automatic-sends';
    if (sentEl.hidden) { sentEl.replaceChildren(); return; }
    const rows = state.sent.map(entry => {
      const view = sentView(entry);
      const row = el('div', 'ap-sent-row');
      const text = el('div', 'lu-col lu-grow');
      const title = el('b', 'lu-h', view.title);
      if (view.fresh) title.append(' ', el('span', 'ap-new', 'New'));
      text.append(title, el('span', 'lu-cap lu-muted', view.detail.join(' · ')));
      if (view.more.length) { const list = el('ul', 'ap-fails'); for (const line of view.more) list.append(el('li', null, line)); text.append(list); }
      if (entry.auto && !entry.undone && Number.isInteger(entry.result.season)) {
        const links = el('details', 'ap-episode-links');
        links.append(el('summary', null, 'Fix an episode'));
        const list = el('ul');
        for (const item of entry.result.sent) {
          const li = el('li');
          const link = libraryLink({
            show: entry.result.show, season: entry.result.season, episode: item.media_version_id,
            kind: item.kinds.length === 1 ? item.kinds[0] : '',
          });
          link.classList.add('lu-quiet');
          link.textContent = item.label;
          li.append(link);
          list.append(li);
        }
        links.append(list);
        text.append(links);
      }
      const actions = el('div', 'ap-sent-actions');
      if (view.canUndo) {
        const undo = button('Undo', { icon: 'arrow-counterclockwise' });
        undo.addEventListener('click', () => act(undo, () => undoSent(entry)));
        actions.append(undo);
      }
      const close = iconButton('x-lg', entry.auto ? 'Clear this notice' : 'Dismiss');
      close.addEventListener('click', () => clearNotices([entry], false));
      actions.append(close);
      row.append(glyph(view.mark, view.tone), text, actions);
      return row;
    });
    sentEl.replaceChildren(sentHead(), ...(rows.length ? rows : [el('p', 'lu-muted', emptyMessage)]));
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
    loadRoute();
  }

  /* What Reprise sent by itself (kept in Plex, newest first) joins the tray as one row for each show and season, so a
     season of sends is one notice, not twenty. Reading and clearing them is kept on the server (/approvals/automatic/notices). */
  function autoGroups(items) {
    const groups = new Map();
    for (const item of items) {
      const key = keyOf(item.show || '', item.season == null ? '' : item.season);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(item);
    }
    return [...groups.values()].map(list => {
      const first = list[0];
      const place = [first.show || 'An episode', first.season == null ? null : 'Season ' + first.season];
      const sent = list.map(item => ({ media_version_id: item.media_version_id, change_id: item.change_id, span_ids: item.span_ids || [], label: item.label, kinds: item.kinds || [] }));
      return {
        result: { show: first.show, season: first.season, sent, approved: [], failed: [], stopped: null, backup: null },
        label: place.filter(Boolean).join(' · '), undone: null, busy: false,
        auto: { change_ids: list.map(item => item.change_id), sent_at: first.sent_at || 0, unread: list.filter(item => !item.read).length },
      };
    });
  }
  async function loadAutomatic() {
    let found;
    try { found = (await api('/approvals/automatic')).sent || []; }
    catch (_) {
      renderSent('Couldn’t load recent changes.');
      return;
    }
    const mine = state.sent.filter(entry => !entry.auto);
    const kept = state.sent.filter(entry => entry.auto && entry.undone);
    const known = new Set(kept.flatMap(entry => entry.auto.change_ids));
    const fresh = autoGroups(found.filter(item => !known.has(item.change_id)));
    const auto = [...kept, ...fresh].sort((a, b) => b.auto.sent_at - a.auto.sent_at);
    state.sent = [...mine, ...auto];
    renderSent();
  }
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') loadAutomatic(); });

  /* ═══ The review: one by one ═══════════════════════════════════════════════════════════════════════════════════ */
  const startOf = item => (item.edit ? item.edit.start_ms : item.start_ms);
  const endOf = item => (item.edit ? item.edit.end_ms : item.end_ms);

  const SURENESS = {
    high: { tone: 'lu-pos', mark: 'patch-check', text: 'High confidence' },
    confirmed: { tone: 'lu-pos', mark: 'check2-all', text: 'Matched across episodes' },
    medium: { tone: 'lu-warn', mark: 'question-circle', text: 'Medium confidence' },
    low: { tone: 'lu-warn', mark: 'exclamation-circle', text: 'Low confidence' },
  };
  const sureness = item => SURENESS[item.how_sure] || { tone: 'lu-inf', mark: 'info-circle', text: 'From ' + item.source };

  function plexNote(item) {
    if (item.plex) return 'Plex now skips ' + fmt(item.plex.start_ms) + ' to ' + fmt(item.plex.end_ms) + '. Keeping Plex unchanged won’t remove that skip.';
    if (item.change === 'new') return 'Plex has no ' + KIND[item.kind].toLowerCase() + ' skip.';
    return 'Plex’s current timing is unavailable. Keeping Plex unchanged won’t remove any existing skip.';
  }

  async function loadRoute() {
    const route = ++state.route;
    teardownClips();
    state.review = null;
    reviewEl.replaceChildren();
    if ($('apToasts').querySelector('button')) $('apToasts').replaceChildren();
    reviewEl.classList.remove('is-busy');
    noticeEl.replaceChildren();
    const params = new URLSearchParams(location.search);
    const number = params.get('season') || '';
    const scope = {
      show: params.get('show') || '', season: /^\d{1,4}$/.test(number) ? Number(number) : null,
      episode: params.get('episode_id') || '', kind: params.get('kind') || '',
    };
    if (!scope.show || scope.season === null) {
      if (scope.episode) {
        showView('season');
        renderSeasonError('No season was specified for this episode.', scope);
        return;
      }
      showView('queue');
      const activity = location.hash === '#automatic-sends';
      renderSent(activity ? 'Loading recent changes…' : undefined);
      await Promise.all([loadQueue(route), activity ? loadAutomatic() : undefined]);
      if (route === state.route) {
        if (activity) $('automatic-sends')?.focus();
        else titleEl.focus({ preventScroll: true });
      }
      return;
    }
    const manual = scope.episode || location.hash === '#review';
    showView(manual ? 'review' : 'season');
    renderReviewWait('Loading ' + scope.show + ', Season ' + scope.season + '…', manual ? scope : null);
    try {
      const plan = await api(seasonUrl(scope.show, scope.season));
      if (state.route !== route) return;
      renderNotice(plan.sending);
      const items = scope.episode
        ? [...plan.needs, ...plan.sure].filter(item => item.media_version_id === scope.episode && (!scope.kind || item.kind === scope.kind))
        : plan.needs;
      if (!manual || !items.length) {
        showView('season');
        renderSeason(plan, scope.episode ? scope : null);
        return;
      }
      state.review = {
        ...scope, show: plan.show, label: plan.label, items: items.map(i => ({ ...i, edit: null })),
        index: 0, history: [], busy: false, adjusting: location.hash === '#fix', active: 'start',
        revs: new Map(items.map(i => [i.media_version_id, i.revision])),
      };
      renderReview();
    } catch (error) {
      if (state.route === route) renderSeasonError(error.message, scope);
    }
  }

  function renderReviewWait(text, scope) {
    const box = el('div', 'lu-panel ap-wait');
    const line = el('span', 'lu-skel'); line.style.width = '46%';
    box.append(el('p', 'lu-muted', text), line);
    reviewEl.replaceChildren(scope ? backToSeason(scope) : backToQueue(), box);
  }

  function renderSeasonError(message, scope) {
    const box = el('div', 'lu-banner lu-bad');
    const body = el('div', 'lu-col lu-grow');
    body.append(el('b', 'lu-h', 'Couldn’t open this review'), el('p', 'lu-muted', message));
    const actions = el('div', 'lu-actions');
    if (scope.show && scope.season !== null) {
      const retry = button('Try again', { icon: 'arrow-repeat' }); retry.addEventListener('click', loadRoute);
      actions.append(retry);
    }
    if (scope.episode) actions.append(libraryLink(scope));
    body.append(actions);
    box.append(glyph('exclamation-triangle', 'lu-bad'), body);
    reviewEl.replaceChildren(backToQueue(), box);
  }

  window.addEventListener('popstate', loadRoute);

  function teardownClips() {
    const rv = state.review;
    if (!rv) return;
    if (rv.jump) { rv.jump.reset(); rv.jump = null; }
    if (rv.clips) { rv.clips.start.destroy(); rv.clips.end.destroy(); rv.clips = null; }
  }

  function jumpPreview(rv, item, stage, clips) {
    const toolbar = el('div', 'ap-jump-tools');
    const play = button('Play the jump', { icon: 'play-fill' });
    const caption = el('p', 'lu-cap lu-muted', 'Watch what plays before and after the skip.');
    const status = el('p', 'ap-jump-status lu-muted'); status.setAttribute('role', 'status');
    const video = el('video', 'ap-jump-video');
    video.controls = true; video.playsInline = true; video.preload = 'none'; video.hidden = true;
    video.setAttribute('aria-label', 'Skip preview');
    toolbar.append(play, caption);
    stage.append(toolbar, status, video);
    let attempt = 0;
    function reset(text = '') {
      ++attempt;
      const loaded = video.hasAttribute('src');
      if (loaded) {
        video.pause(); video.removeAttribute('src'); video.load();
      }
      video.hidden = true;
      clips.hidden = false;
      play.querySelector('span').textContent = 'Play the jump';
      play.querySelector('.bi').className = 'bi bi-play-fill';
      status.textContent = text;
    }
    function toggle() {
      if (video.hasAttribute('src')) { reset(); return; }
      const start = rv.clips.start.edge, end = rv.clips.end.edge;
      if (start === 0 && end >= item.duration_ms) { reset('Jump preview unavailable: there’s no footage outside this skip.'); return; }
      const mine = ++attempt;
      for (const clip of Object.values(rv.clips)) clip.pause();
      const params = new URLSearchParams({ start_ms: start, end_ms: end });
      video.src = API + '/media/' + encodeURIComponent(item.media_version_id) + '/jump-preview?' + params;
      status.textContent = 'Preparing preview…';
      play.querySelector('span').textContent = 'Close preview';
      play.querySelector('.bi').className = 'bi bi-x-lg';
      video.play().catch(error => {
        if (mine !== attempt) return;
        if (error.name === 'NotAllowedError') { video.hidden = false; status.textContent = 'Use the video’s Play button.'; }
        else reset('Jump preview unavailable. You can still check the timings below.');
      });
    }
    play.addEventListener('click', toggle);
    video.addEventListener('playing', () => {
      if (!video.hasAttribute('src')) return;
      video.hidden = false;
      clips.hidden = !rv.adjusting;
      status.textContent = '';
    });
    video.addEventListener('error', () => {
      if (video.hasAttribute('src')) reset('Jump preview unavailable. You can still check the timings below.');
    });
    return { reset, toggle };
  }

  function renderReview() {
    const rv = state.review;
    if (!rv) return;
    if (rv.index >= rv.items.length) { renderDone(); return; }
    teardownClips();
    const item = rv.items[rv.index], total = rv.items.length;

    const top = el('div', 'ap-top');
    const where = el('div', 'lu-col lu-grow');
    where.append(el('span', 'lu-h', rv.show + ' · ' + rv.label),
      el('span', 'lu-cap lu-muted', 'Skip ' + (rv.index + 1) + ' of ' + total));
    const keys = iconButton('keyboard', 'Keyboard shortcuts', 'keys');
    keys.classList.add('ap-help'); keys.setAttribute('aria-expanded', 'false'); keys.setAttribute('aria-controls', 'apKeys');
    const undo = iconButton('arrow-counterclockwise', 'Undo the last choice', 'undo');
    undo.disabled = rv.history.length === 0;
    const back = backToSeason(rv); back.dataset.act = 'back';
    top.append(back, where, keys, undo);

    const bar = el('div', 'lu-bar');
    bar.setAttribute('role', 'progressbar'); bar.setAttribute('aria-label', 'Progress'); bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', String(total)); bar.setAttribute('aria-valuenow', String(rv.index));
    const fill = el('i'); fill.style.width = (rv.index / total) * 100 + '%'; bar.append(fill);

    const help = el('div', 'lu-panel ap-keys'); help.id = 'apKeys'; help.hidden = true;
    for (const [key, what] of [['S', 'Approve skip'], ['K', 'Keep Plex unchanged'], ['A', 'Adjust timings'], ['Space', 'Play the jump or the selected edge while editing'],
      ['1 2', 'Pick the start or end'], ['← →', 'Move that edge by 1 second'], ['C', 'Snap the edge to a cut'],
      ['U', 'Undo the last choice'], ['Esc', 'Back to season']]) {
      help.append(el('kbd', 'lu-kbd', key), el('span', 'lu-muted', what));
    }

    const card = el('article', 'lu-panel ap-card');
    card.dataset.adjusting = String(rv.adjusting);
    const mark = sureness(item);
    const head = el('div', 'ap-card-head');
    const heading = el('h2', 'lu-title', item.label + (item.title ? ' · ' + item.title : ''));
    heading.id = 'apCardTitle'; heading.tabIndex = -1;
    const badges = el('div', 'ap-badges');
    badges.append(el('span', 'lu-badge lu-acc', KIND[item.kind]));
    const sure = el('span', 'lu-badge ' + mark.tone); sure.append(icon(mark.mark), mark.text);
    badges.append(sure);
    if (item.how_sure && item.source !== 'Reprise') badges.append(el('span', 'lu-cap lu-muted', item.source));
    head.append(heading, badges);
    card.append(head);
    if (item.reason) {
      const why = el('details', 'ap-reason');
      why.append(el('summary', null, 'Why this skip?'), el('p', 'lu-muted', item.reason));
      card.append(why);
    }

    const jump = el('div', 'ap-jump');
    card.append(jump);
    const clips = el('div', 'ap-clips');
    const boxes = {};
    for (const [side, title] of [['start', 'Skip starts'], ['end', 'Skip ends']]) {
      const box = el('div', 'ap-edge'); box.dataset.side = side;
      box.append(el('span', 'lu-h ap-edge-title', title));
      boxes[side] = el('div'); box.append(boxes[side]);
      box.addEventListener('focusin', () => pick(side));
      box.addEventListener('pointerdown', () => pick(side));
      box.addEventListener('click', event => {
        if (event.target.closest('.lu-clip-play')) rv.jump.reset();
      }, { capture: true });
      clips.append(box);
    }
    card.append(clips);
    const note = el('p', 'lu-cap lu-muted ap-plex');
    note.append(icon('info-circle'), plexNote(item));
    card.append(note);

    const tray = el('div', 'lu-tray ap-choose');
    tray.setAttribute('role', 'group'); tray.setAttribute('aria-label', 'Your choice');
    const row = el('div', 'lu-trow');
    const sum = el('div', 'lu-tsum');
    sum.append(el('b', null, KIND[item.kind] + ' skip'));
    const times = el('span', 'ap-times'); times.id = 'apTimes';
    sum.append(times);
    const buttons = el('div', 'ap-buttons');
    const adjust = button('Adjust timings', { icon: 'sliders', key: 'A', act: 'adjust' });
    adjust.setAttribute('aria-pressed', String(rv.adjusting));
    buttons.append(button('Approve skip', { cls: 'lu-primary', icon: 'check2', key: 'S', act: 'skip' }),
      button('Keep Plex unchanged', { key: 'K', act: 'keep' }), adjust);
    row.append(sum, buttons);
    tray.append(row);

    reviewEl.replaceChildren(top, bar, help, card, tray);

    const make = (side, edge, originalMs, label) => Clip.create(boxes[side], {
      mediaId: item.media_version_id, durationMs: item.duration_ms, edgeMs: edge, originalMs, label,
      onChange: () => { rv.jump.reset(); pick(side); fit(); paintTimes(); },
    });
    rv.clips = { start: make('start', startOf(item), item.start_ms, 'Skip starts'), end: make('end', endOf(item), item.end_ms, 'Skip ends') };
    rv.jump = jumpPreview(rv, item, jump, clips);
    fit();
    pick(rv.active);
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
    rv.clips.start.bounds(0, rv.clips.end.edge - 1);
    rv.clips.end.bounds(rv.clips.start.edge + 1, item.duration_ms);
  }
  function pick(side) {
    const rv = state.review;
    if (!rv) return;
    rv.active = side;
    for (const box of reviewEl.querySelectorAll('.ap-edge')) box.classList.toggle('is-active', box.dataset.side === side);
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
    if (!rv || rv.busy || !rv.clips) return;
    rv.jump.reset();
    rv.adjusting = on;
    for (const clip of Object.values(rv.clips)) clip.edit(on);
    if (!on) for (const clip of Object.values(rv.clips)) clip.pause();
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
      if (state.review !== rv) return;
      rv.revs.set(item.media_version_id, answer.revision);
      item.edit = { start_ms: start, end_ms: end };
      rv.history.push({ index: rv.index, item, decision, undo: answer.undo });
      rv.index += 1;
      rv.adjusting = location.hash === '#fix';
      rv.active = 'start';
      toast(decision === 'skip' ? 'Skip approved. Nothing sent to Plex.' : 'Suggestion dismissed. Plex unchanged.',
        { undo: () => { if (state.review === rv) undoLast(); } });
      renderReview();
    } catch (error) {
      if (state.review !== rv) return;
      if (error.status === 409) {
        toast(error.message + ' Reloading this review.', { tone: 'bad' });
        loadRoute();
        return;
      }
      toast(error.message, { tone: 'bad' });
    } finally {
      if (state.review === rv) { rv.busy = false; reviewEl.classList.remove('is-busy'); }
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
      if (state.review !== rv) return;
      rv.revs.set(id, answer.revision);
      rv.history.pop();
      rv.index = last.index;
      const moved = Boolean(last.item.edit && (last.item.edit.start_ms !== last.item.start_ms || last.item.edit.end_ms !== last.item.end_ms));
      rv.adjusting = moved || location.hash === '#fix';
      if (!moved) last.item.edit = null;
      toast('Undone. Choose again.', { ms: 3500 });
      renderReview();
    } catch (error) {
      toast(error.message, { tone: 'bad' });
    } finally {
      if (state.review === rv) { rv.busy = false; reviewEl.classList.remove('is-busy'); }
    }
  }

  function renderDone() {
    const rv = state.review;
    teardownClips();
    const approved = rv.history.filter(h => h.decision === 'skip').length;
    const dismissed = rv.history.length - approved;
    const text = el('div', 'lu-col');
    const heading = el('h2', 'lu-title', rv.episode ? 'Episode review complete' : 'Timing review complete');
    heading.tabIndex = -1;
    text.append(heading, el('p', 'lu-muted', [
      approved ? plural(approved, 'skip') + ' approved' : '',
      dismissed ? plural(dismissed, 'suggestion') + ' dismissed' : '',
    ].filter(Boolean).join(' · ')), el('p', 'lu-cap lu-muted', 'Nothing was sent to Plex.'));
    const box = el('div', 'lu-panel ap-done');
    const actions = el('div', 'ap-done-actions');
    const back = backToSeason(rv);
    back.classList.add('lu-primary');
    back.dataset.act = 'back';
    const undo = button('Undo the last choice', { icon: 'arrow-counterclockwise', act: 'undo' });
    actions.append(back, undo);
    box.append(glyph('check2-all', 'lu-pos', 'lu-big'), text, actions);
    reviewEl.replaceChildren(box);
    heading.focus({ preventScroll: true });
  }

  /* ── clicks and keys in the review ────────────────────────────────────────────────────────────────────────── */
  reviewEl.addEventListener('click', event => {
    const target = event.target.closest('[data-act]');
    if (!target || target.disabled) return;
    const rv = state.review;
    switch (target.dataset.act) {
      case 'back': break; // the link handles the browser history
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
    if (!rv || rv.busy || root.dataset.view !== 'review' || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    const from = event.target;
    if (from.closest && from.closest('input, textarea, select, video, dialog, [contenteditable], .lu-thm-pop')) return;
    const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
    const finished = rv.index >= rv.items.length;
    if (key === 'Escape') {
      const panel = $('apKeys');
      if (panel && !panel.hidden) {
        panel.hidden = true;
        const toggle = reviewEl.querySelector('[data-act="keys"]');
        toggle.setAttribute('aria-expanded', 'false');
        toggle.focus();
      } else reviewEl.querySelector('[data-act="back"]')?.click();
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
      if (from.closest && from.closest('button, a, summary')) return;
      if (rv.adjusting && rv.clips) {
        rv.jump.reset();
        rv.clips[rv.active].toggle();
      } else rv.jump?.toggle();
    } else if (key === '1' || key === '2') {
      pick(key === '1' ? 'start' : 'end');
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
  loadRoute();
  if (location.hash !== '#automatic-sends') loadAutomatic();
})();
