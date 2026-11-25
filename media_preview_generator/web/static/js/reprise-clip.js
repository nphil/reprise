/* Reprise clip: look at one moment of an episode.
 *
 * A still picture of the moment, a few seconds of video around it to play, and (while editing) a strip of pictures
 * around it with a marker, nudge buttons (1 second) and "Snap to cut". It draws into a host element with the Lucent
 * clip styles (reprise-lucent.css, section 8) and knows nothing about the screen around it: the host says which
 * episode and moment, and hears back through onChange when the owner moves the moment.
 *
 *   const clip = RepriseClip.create(host, { mediaId, durationMs, edgeMs, originalMs, label: 'Skip starts', onChange(ms, why) {} });
 *   clip.edit(true);  clip.nudge(1000);  clip.snap();  clip.toggle();  clip.set(ms);  clip.destroy();
 *
 * Pictures always come from /api/reprise/media/<id>/frame|strip|cuts (ffmpeg on the server). The video plays the file
 * itself from /stream; a file the browser cannot play falls back to a short clip Reprise makes (a preview job). */
(function () {
  'use strict';

  const API = '/api/reprise/media/';
  const STILL_W = 480;      // picture widths the server offers: 120 160 240 320 480 640 960
  const STRIP_W = 160;
  const STEP_MS = 1000;     // time between two pictures of the strip
  const NUDGE_MS = 1000;
  const FINE_MS = 100;      // with Shift, or when dragging the strip
  const REACH_MS = 3000;    // how far "Snap to cut" looks either side
  const LEAD_MS = 4000;     // the clip plays this long before the moment...
  const TRAIL_MS = 4000;    // ...and this long after it
  const STRIP_HEIGHT = 56;  // the strip's wanted picture height, in px
  const AFTER_MS = 120;     // a moved moment redraws its still this long after the last move

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };
  const icon = name => { const i = el('i', 'bi bi-' + name); i.setAttribute('aria-hidden', 'true'); return i; };
  const clamp = (n, lo, hi) => Math.min(Math.max(n, lo), Math.max(lo, hi));
  const pad = n => String(n).padStart(2, '0');
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

  /* 0:32 · 1:05.5 · 1:02:03: whole seconds unless there is a tenth to show. */
  function fmt(ms) {
    const tenths = Math.max(0, Math.round(ms / 100));
    const total = Math.floor(tenths / 10), tenth = tenths % 10;
    const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
    const clock = h ? h + ':' + pad(m) + ':' + pad(s) : m + ':' + pad(s);
    return tenth ? clock + '.' + tenth : clock;
  }

  const frameUrl = (id, ms, width) => API + encodeURIComponent(id) + '/frame?t_ms=' + Math.round(ms) + '&w=' + (width || STILL_W);
  /* Warm the browser's cache so the next moment appears the instant it is asked for. */
  const prefetch = (id, ms) => { const probe = new Image(); probe.src = frameUrl(id, ms); };

  async function request(path, body) {
    const response = await fetch(path, body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    let data = {};
    try { data = await response.json(); } catch (_) { /* an empty or non-JSON answer */ }
    if (!response.ok) throw new Error(data.error || data.message || 'That did not work (' + response.status + ').');
    return data;
  }

  const audioStreams = new Map();   // media id -> the audio track a short clip is made with
  async function audioStreamOf(id) {
    if (!audioStreams.has(id)) {
      const result = await request(API + encodeURIComponent(id) + '/result');
      const found = (result.identities || []).map(i => i.audio_stream_id).find(Boolean);
      if (!found) throw new Error('This episode has no audio track to play.');
      audioStreams.set(id, found);
    }
    return audioStreams.get(id);
  }

  const unplayable = new Set();     // media ids the browser could not play from /stream: go straight to a short clip
  let playingNow = null;            // only one clip plays at a time

  function create(host, opts) {
    const id = String(opts.mediaId);
    const duration = Math.max(0, Number(opts.durationMs) || 0);
    const label = opts.label || 'This moment';
    const original = Math.round(opts.originalMs != null ? opts.originalMs : opts.edgeMs);
    let edge = Math.round(opts.edgeMs), minMs = 0, maxMs = duration, editing = false, destroyed = false, scrubbing = false;
    let state = 'still', origin = 0, playFrom = 0, playTo = 0, usingPreview = false;
    let stillToken = 0, stripToken = 0, previewToken = 0, stillTimer = 0, noteTimer = 0;
    let tiles = 0, winStart = 0, aspect = 16 / 9, resizer = null;

    host.classList.add('lu-clip');
    host.replaceChildren();

    const stage = el('div', 'lu-clip-stage');
    const still = el('img'); still.alt = ''; still.decoding = 'async'; still.draggable = false;
    const video = el('video', 'lu-clip-video'); video.playsInline = true; video.preload = 'none'; video.hidden = true;
    const playButton = el('button', 'lu-clip-play'); playButton.type = 'button';
    const glyph = icon('play-fill');
    const disc = el('span'); disc.append(glyph); playButton.append(disc);
    const timeChip = el('span', 'lu-clip-time');
    const note = el('p', 'lu-clip-note'); note.setAttribute('role', 'status');
    stage.append(still, video, playButton, timeChip, note);

    const strip = el('div', 'lu-strip'); strip.hidden = true;
    const stripPictures = el('div', 'lu-strip-img');
    const marker = el('i', 'lu-strip-edge'); marker.setAttribute('aria-hidden', 'true');
    const seek = el('input', 'lu-strip-seek'); seek.type = 'range'; seek.min = '0'; seek.step = String(FINE_MS);
    seek.setAttribute('aria-label', label + ': drag to move it');
    strip.append(stripPictures, marker, seek);

    const tools = el('div', 'lu-clip-tools'); tools.hidden = true;
    const button = (text, title, ...parts) => {
      const b = el('button', 'lu-btn'); b.type = 'button'; b.title = title; b.setAttribute('aria-label', title); b.append(...parts, text); return b;
    };
    const earlier = button('1 s', label + ': 1 second earlier', icon('chevron-left'));
    const later = button('1 s', label + ': 1 second later');
    later.append(icon('chevron-right'));
    const snapButton = button('Snap to cut', 'Move to the nearest clean cut in the picture', icon('scissors'));
    const reset = el('button', 'lu-linkbtn', 'Reset'); reset.type = 'button'; reset.hidden = true;
    reset.title = 'Back to ' + fmt(original);
    const readout = el('span', 'lu-readout'); readout.setAttribute('aria-live', 'polite');
    tools.append(earlier, later, snapButton, reset, readout);

    host.append(stage, strip, tools);

    /* ── messages and state ───────────────────────────────────────────────────────────────────────────────────── */
    function say(text, forMs) {
      clearTimeout(noteTimer);
      note.textContent = text || '';
      if (text && forMs) noteTimer = setTimeout(() => { note.textContent = ''; }, forMs);
    }
    function setState(next) {
      state = next;
      host.dataset.state = next;
      glyph.className = 'bi bi-' + (next === 'playing' ? 'pause-fill' : next === 'loading' ? 'arrow-repeat' : 'play-fill');
      paintLabels();
    }
    function paintLabels() {
      playButton.setAttribute('aria-label', (state === 'playing' ? 'Pause' : 'Play a few seconds either side of') + ' ' + fmt(edge) + ' · ' + label);
    }
    function paintTime() {
      timeChip.textContent = fmt(edge);
      const moved = edge !== original;
      readout.textContent = moved ? fmt(edge) + ' · was ' + fmt(original) : fmt(edge);
      readout.classList.toggle('lu-acc', moved);
      reset.hidden = !moved;
      seek.setAttribute('aria-valuetext', fmt(edge));
      paintLabels();
    }

    /* ── the still ────────────────────────────────────────────────────────────────────────────────────────────── */
    function showStill() {
      const mine = ++stillToken, url = frameUrl(id, edge, STILL_W), probe = new Image();
      probe.onload = () => {
        if (mine !== stillToken || destroyed) return;
        still.src = url;
        stage.dataset.picture = 'ok';
        const shape = probe.naturalWidth / probe.naturalHeight;
        if (shape > 0.5 && Math.abs(shape - aspect) > 0.01) { aspect = shape; layoutStrip(); }
      };
      probe.onerror = () => {
        if (mine !== stillToken || destroyed) return;
        stage.dataset.picture = 'missing';
        say('Couldn’t read a picture from this episode here.');
      };
      probe.src = url;
    }
    function scheduleStill() {
      clearTimeout(stillTimer);
      stillTimer = setTimeout(showStill, AFTER_MS);
    }

    /* ── playing ──────────────────────────────────────────────────────────────────────────────────────────────── */
    function stopPlaying() {
      ++previewToken;
      video.pause();
      video.hidden = true;
      if (playingNow === api) playingNow = null;
      setState('still');
    }
    function stumbled(message) {
      stopPlaying();
      say(message || 'This part could not be played.', 6000);
    }
    function begin() {
      if (destroyed) return;
      video.currentTime = Math.max(0, (playFrom - origin) / 1000);
      video.play().then(() => { if (state === 'loading') setState('playing'); }, failed);
    }
    /* Only a refusal of the file itself is worth a short clip; a blocked or interrupted play() is not. */
    function failed(error) {
      if (state === 'still') return;
      if (usingPreview || (error && (error.name === 'AbortError' || error.name === 'NotAllowedError'))) { stumbled(); return; }
      if (unplayable.has(id)) return;   // a short clip is already being made for this file
      unplayable.add(id);
      makePreview();
    }
    async function makePreview() {
      const mine = ++previewToken;
      setState('loading');
      say('Making a short clip this browser can play…');
      try {
        const audio = await audioStreamOf(id);
        let job = await request(API + encodeURIComponent(id) + '/preview', { start_ms: playFrom, end_ms: playTo, audio_stream_id: audio });
        while (!['completed', 'complete', 'failed', 'cancelled'].includes(job.status)) {
          await wait(1200);
          if (destroyed || mine !== previewToken) return;
          job = await request('/api/reprise/jobs/' + encodeURIComponent(job.id));
        }
        if (destroyed || mine !== previewToken) return;
        const made = job.preview;
        if (!made || !made.url || job.status === 'failed' || job.status === 'cancelled') throw new Error(job.error || 'The short clip could not be made.');
        const url = new URL(made.url, location.origin);
        if (url.origin !== location.origin) throw new Error('The short clip could not be made.');
        usingPreview = true; origin = made.origin_ms || 0;
        say('');
        video.src = url.pathname + url.search;
        video.addEventListener('loadedmetadata', begin, { once: true });
        video.load();
      } catch (error) {
        if (!destroyed && mine === previewToken) stumbled(error && error.message);
      }
    }
    const streamPath = () => API + encodeURIComponent(id) + '/stream';
    function direct() {
      usingPreview = false; origin = 0;
      if (video.getAttribute('src') !== streamPath()) {
        video.src = streamPath();
        video.addEventListener('loadedmetadata', begin, { once: true });
        video.load();
      } else if (video.readyState >= 1) begin();
      else video.addEventListener('loadedmetadata', begin, { once: true });
    }
    function play() {
      if (playingNow && playingNow !== api) playingNow.pause();
      playingNow = api;
      playFrom = Math.max(0, edge - LEAD_MS);
      playTo = Math.min(duration, edge + TRAIL_MS);
      say('');
      video.hidden = false;
      setState('loading');
      if (unplayable.has(id)) makePreview(); else direct();
    }
    function toggle() { if (state === 'still') play(); else stopPlaying(); }
    video.addEventListener('timeupdate', () => {
      if (state === 'playing' && (origin + video.currentTime * 1000 >= playTo || video.ended)) stopPlaying();
    });
    video.addEventListener('error', () => { if (state !== 'still') failed(video.error); });
    video.addEventListener('ended', () => { if (state === 'playing') stopPlaying(); });
    playButton.addEventListener('click', toggle);

    /* ── the strip ────────────────────────────────────────────────────────────────────────────────────────────── */
    function windowHolds() {
      const span = tiles * STEP_MS, atStart = winStart === 0, atEnd = winStart + span >= duration;
      return edge >= winStart + (atStart ? 0 : STEP_MS) && edge <= winStart + span - (atEnd ? 0 : STEP_MS);
    }
    function centreWindow() {
      const span = tiles * STEP_MS;
      winStart = clamp(Math.floor((edge - span / 2) / STEP_MS) * STEP_MS, 0, duration - span);
    }
    function loadStrip() {
      const mine = ++stripToken, count = tiles, start = winStart;
      const url = API + encodeURIComponent(id) + '/strip?start_ms=' + start + '&step_ms=' + STEP_MS + '&count=' + count + '&w=' + STRIP_W;
      stripPictures.style.backgroundImage = 'none';
      strip.dataset.state = 'loading';
      const probe = new Image();
      probe.onload = () => {
        if (mine !== stripToken || destroyed) return;
        stripPictures.style.backgroundImage = 'url("' + url + '")';
        strip.dataset.state = 'ready';
      };
      probe.onerror = () => {
        if (mine !== stripToken || destroyed) return;
        strip.dataset.state = 'failed';
        say('Couldn’t read the pictures around this time.', 5000);
      };
      probe.src = url;
      seek.max = String(count * STEP_MS);
      paintMarker();
    }
    function paintMarker() {
      const span = Math.max(1, tiles * STEP_MS);
      marker.style.left = clamp(((edge - winStart) / span) * 100, 0, 100) + '%';
      if (!scrubbing) seek.value = String(clamp(edge - winStart, 0, span));
    }
    function layoutStrip() {
      if (!editing) return;
      const width = strip.clientWidth || host.clientWidth || 320;
      const wanted = clamp(Math.round(width / (STRIP_HEIGHT * aspect)), 3, 12);
      strip.style.setProperty('--strip-ar', String(wanted * aspect));
      if (wanted !== tiles || !windowHolds()) {
        tiles = wanted;
        centreWindow();
        loadStrip();
      } else paintMarker();
    }
    seek.addEventListener('input', () => {
      scrubbing = true;
      setEdge(winStart + Number(seek.value), 'scrub');
    });
    seek.addEventListener('change', () => {
      scrubbing = false;
      if (!windowHolds()) { centreWindow(); loadStrip(); } else paintMarker();
    });
    seek.addEventListener('keydown', event => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      event.stopPropagation();
      nudge((event.key === 'ArrowLeft' ? -1 : 1) * (event.shiftKey ? FINE_MS : NUDGE_MS));
    });

    /* ── moving the moment ────────────────────────────────────────────────────────────────────────────────────── */
    function setEdge(ms, why) {
      const next = clamp(Math.round(ms), minMs, maxMs);
      if (next === edge) return false;
      if (state !== 'still') stopPlaying();
      edge = next;
      paintTime();
      if (editing) { if (scrubbing) paintMarker(); else layoutStrip(); }
      scheduleStill();
      if (why !== 'set' && opts.onChange) opts.onChange(edge, why);
      return true;
    }
    function nudge(delta) {
      if (!setEdge(edge + delta, 'nudge')) say('That is as far as it can go.', 2500);
    }
    async function snap() {
      snapButton.setAttribute('aria-busy', 'true'); snapButton.disabled = true;
      say('Looking for a clean cut…');
      try {
        const from = Math.max(0, edge - REACH_MS), to = Math.min(duration, edge + REACH_MS);
        const found = await request(API + encodeURIComponent(id) + '/cuts?start_ms=' + from + '&end_ms=' + to);
        if (destroyed) return;
        const near = (found.cuts || []).filter(t => t >= minMs && t <= maxMs && Math.abs(t - edge) > 40)
          .sort((a, b) => Math.abs(a - edge) - Math.abs(b - edge));
        if (near.length) { say(''); setEdge(near[0], 'snap'); } else say('No clean cut within ' + REACH_MS / 1000 + ' seconds of here.', 4000);
      } catch (error) {
        if (!destroyed) say(error.message || 'Couldn’t look for a cut.', 5000);
      } finally {
        snapButton.removeAttribute('aria-busy'); snapButton.disabled = false;
      }
    }
    earlier.addEventListener('click', () => nudge(-NUDGE_MS));
    later.addEventListener('click', () => nudge(NUDGE_MS));
    snapButton.addEventListener('click', snap);
    reset.addEventListener('click', () => setEdge(original, 'reset'));

    function edit(on) {
      editing = Boolean(on);
      strip.hidden = tools.hidden = !editing;
      host.classList.toggle('is-editing', editing);
      if (resizer) { resizer.disconnect(); resizer = null; }
      if (!editing) return;
      requestAnimationFrame(layoutStrip);
      if (window.ResizeObserver) { resizer = new ResizeObserver(() => layoutStrip()); resizer.observe(strip); }
    }

    function destroy() {
      destroyed = true;
      clearTimeout(stillTimer); clearTimeout(noteTimer);
      if (resizer) resizer.disconnect();
      video.pause(); video.removeAttribute('src'); video.load();
      if (playingNow === api) playingNow = null;
      host.replaceChildren();
      host.classList.remove('lu-clip', 'is-editing');
      delete host.dataset.state;
    }

    const api = {
      get edge() { return edge; },
      get changed() { return edge !== original; },
      set(ms) { return setEdge(ms, 'set'); },
      bounds(lo, hi) { minMs = Math.max(0, lo); maxMs = Math.min(duration, hi); },
      edit, nudge, snap, toggle, play, pause: stopPlaying, destroy,
      reset() { return setEdge(original, 'reset'); },
      focus() { playButton.focus({ preventScroll: true }); },
    };

    setState('still');
    paintTime();
    showStill();
    return api;
  }

  window.RepriseClip = { create, fmt, frameUrl, prefetch };
})();
