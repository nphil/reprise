/* Reprise's 42 colour themes. Loaded before CSS to apply the saved palette without flashing.
 * The palettes live in reprise-themes.css; the shell mounts the picker after its HTML exists. */
(function () {
  'use strict';
  var KEY = 'reprise.theme', DEFAULT_DARK = 'lucent-dark', DEFAULT_LIGHT = 'lucent-light';
  var THEMES = [
    ["lucent-dark", "Lucent", "Lucent", "dark", "#191724"],
    ["rose-pine", "Rosé Pine", "Rosé Pine", "dark", "#191724"],
    ["rose-pine-moon", "Rosé Pine Moon", "Rosé Pine", "dark", "#232136"],
    ["catppuccin-mocha", "Catppuccin Mocha", "Catppuccin", "dark", "#1E1E2E"],
    ["catppuccin-macchiato", "Catppuccin Macchiato", "Catppuccin", "dark", "#24273A"],
    ["catppuccin-frappe", "Catppuccin Frappé", "Catppuccin", "dark", "#303446"],
    ["tokyo-night", "Tokyo Night", "Tokyo Night", "dark", "#1A1B26"],
    ["tokyo-night-storm", "Tokyo Night Storm", "Tokyo Night", "dark", "#24283B"],
    ["dracula", "Dracula", "Dracula", "dark", "#282A36"],
    ["nord", "Nord", "Nord", "dark", "#2E3440"],
    ["gruvbox-dark", "Gruvbox Dark", "Gruvbox", "dark", "#282828"],
    ["everforest-dark", "Everforest Dark", "Everforest", "dark", "#2D353B"],
    ["solarized-dark", "Solarized Dark", "Solarized", "dark", "#002B36"],
    ["one-dark", "One Dark", "One", "dark", "#282C34"],
    ["monokai", "Monokai", "Monokai", "dark", "#272822"],
    ["night-owl", "Night Owl", "Night Owl", "dark", "#011627"],
    ["ayu-mirage", "Ayu Mirage", "Ayu", "dark", "#242936"],
    ["github-dark", "GitHub Dark", "GitHub", "dark", "#0D1117"],
    ["kanagawa-wave", "Kanagawa Wave", "Kanagawa", "dark", "#1F1F28"],
    ["nightfox", "Nightfox", "Nightfox", "dark", "#192330"],
    ["material-palenight", "Material Palenight", "Material", "dark", "#292D3E"],
    ["lucent-light", "Lucent", "Lucent", "light", "#FAF4ED"],
    ["rose-pine-dawn", "Rosé Pine Dawn", "Rosé Pine", "light", "#FAF4ED"],
    ["catppuccin-latte", "Catppuccin Latte", "Catppuccin", "light", "#EFF1F5"],
    ["tokyo-night-day", "Tokyo Night Day", "Tokyo Night", "light", "#E1E2E7"],
    ["alucard", "Alucard", "Dracula", "light", "#FFFBEB"],
    ["gruvbox-light", "Gruvbox Light", "Gruvbox", "light", "#FBF1C7"],
    ["everforest-light", "Everforest Light", "Everforest", "light", "#FDF6E3"],
    ["solarized-light", "Solarized Light", "Solarized", "light", "#FDF6E3"],
    ["one-light", "One Light", "One", "light", "#FAFAFA"],
    ["night-owl-light", "Night Owl Light", "Night Owl", "light", "#FBFBFB"],
    ["ayu-light", "Ayu Light", "Ayu", "light", "#FCFCFC"],
    ["github-light", "GitHub Light", "GitHub", "light", "#FFFFFF"],
    ["kanagawa-lotus", "Kanagawa Lotus", "Kanagawa", "light", "#F2ECBC"],
    ["dayfox", "Dayfox", "Nightfox", "light", "#F6F2EE"],
    ["dawnfox", "Dawnfox", "Nightfox", "light", "#FAF4ED"],
    ["material-lighter", "Material Lighter", "Material", "light", "#FAFAFA"],
    ["oxocarbon-light", "Oxocarbon Light", "Oxocarbon", "light", "#F2F4F8"],
    ["flexoki-light", "Flexoki Light", "Flexoki", "light", "#FFFCF0"],
    ["modus-operandi", "Modus Operandi", "Modus", "light", "#FFFFFF"],
    ["papercolor-light", "PaperColor Light", "PaperColor", "light", "#EEEEEE"],
    ["selenized-light", "Selenized Light", "Selenized", "light", "#FBF3DB"]
  ].map(function (t) { return { id: t[0], name: t[1], family: t[2], scheme: t[3], canvas: t[4] }; });
  var byId = Object.create(null);
  THEMES.forEach(function (t) { byId[t.id] = t; });

  var root = document.documentElement;
  var query = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;
  var pref = read();

  function valid(id, scheme) {
    var t = typeof id === 'string' ? byId[id] : null;
    return !!t && (!scheme || t.scheme === scheme);
  }
  function read() {
    var saved = null, legacy = null;
    try { saved = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (e) { /* unreadable: use the defaults */ }
    try { legacy = localStorage.getItem('theme'); } catch (e) { /* storage blocked */ }
    saved = saved && typeof saved === 'object' ? saved : {};
    var dark = valid(saved.dark, 'dark') ? saved.dark : DEFAULT_DARK;
    var light = valid(saved.light, 'light') ? saved.light : DEFAULT_LIGHT;
    var id = valid(saved.id) ? saved.id : (legacy === 'light' ? light : dark);
    return { mode: saved.mode === 'auto' ? 'auto' : 'fixed', id: id, scheme: byId[id].scheme, dark: dark, light: light };
  }
  function save() {
    try {
      localStorage.setItem(KEY, JSON.stringify(pref));
    } catch (e) { /* best effort */ }
  }
  function active() {
    if (pref.mode === 'auto') return byId[query && query.matches ? pref.light : pref.dark];
    return byId[pref.id];
  }
  function paint() {
    var t = active();
    root.setAttribute('data-theme', t.id);
    root.setAttribute('data-scheme', t.scheme);
    root.setAttribute('data-bs-theme', t.scheme);
    var meta = document.querySelector('meta[name=theme-color]');
    if (!meta) { meta = document.createElement('meta'); meta.name = 'theme-color'; document.head.appendChild(meta); }
    meta.content = t.canvas;
    document.dispatchEvent(new CustomEvent('reprise:theme', { detail: t }));
  }
  function changed() {
    save();
    paint();
  }
  function choose(id) {
    var t = byId[id];
    if (!t) return;
    if (pref.mode === 'fixed') { pref.id = id; pref.scheme = t.scheme; }
    pref[t.scheme] = id;
    changed();
  }
  function setAuto(on) {
    var shown = active();
    if (on) pref.mode = 'auto';
    else { pref.mode = 'fixed'; pref.id = shown.id; pref.scheme = shown.scheme; }
    changed();
  }
  function toggleScheme() {
    var next = active().scheme === 'dark' ? 'light' : 'dark';
    pref.mode = 'fixed';
    choose(pref[next]);
  }
  if (query) query.addEventListener('change', function () { if (pref.mode === 'auto') changed(); });
  window.addEventListener('storage', function (e) {
    if (e.key === KEY) { pref = read(); paint(); }
  });
  paint();

  /* ── the picker ─────────────────────────────────────────────────────────────────────────────────────────── */
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function icon(name) { var i = el('i', 'bi bi-' + name); i.setAttribute('aria-hidden', 'true'); return i; }

  function mountPicker(pop) {
    var search = pop.querySelector('input');
    var scroll = pop.querySelector('.lu-thm-scroll');
    var autoNote = pop.querySelector('#rpThemePair');
    var autoSwitch = pop.querySelector('[role=switch]');

    function isOn(t) {
      if (pref.mode === 'auto') return (t.scheme === 'dark' ? pref.dark : pref.light) === t.id;
      return pref.id === t.id;
    }
    function tile(t, tabbable) {
      var b = el('button', 'lu-thm-tile');
      b.type = 'button'; b.setAttribute('role', 'radio'); b.setAttribute('aria-checked', String(isOn(t)));
      b.setAttribute('aria-label', t.name); b.dataset.id = t.id; b.tabIndex = tabbable ? 0 : -1;
      var prev = el('span', 'lu-thm-prev'); prev.dataset.theme = t.id; prev.setAttribute('aria-hidden', 'true');
      ['pv-card', 'pv-ink', 'pv-ink2', 'pv-acc'].forEach(function (c) { prev.appendChild(el('i', c)); });
      var dots = el('span', 'pv-dots');
      ['positive', 'warning', 'danger', 'info'].forEach(function (c) { var d = el('i'); d.style.background = 'var(--pal-' + c + ')'; dots.appendChild(d); });
      prev.appendChild(dots);
      b.append(prev, el('span', 'lu-thm-name', t.name));
      if (isOn(t)) { var chk = el('span', 'lu-thm-chk'); chk.setAttribute('aria-hidden', 'true'); chk.appendChild(icon('check-lg')); b.appendChild(chk); }
      b.addEventListener('click', function () { choose(t.id); });
      return b;
    }
    function group(name, list) {
      var section = el('section', 'lu-thm-group');
      section.setAttribute('aria-label', name + ' themes');
      var head = el('h3', 'lu-h', name);
      if (pref.mode === 'auto') head.append(el('span', 'lu-cap lu-muted', ' · used when your device is ' + name.toLowerCase()));
      var grid = el('div', 'lu-thm-grid');
      grid.setAttribute('role', 'radiogroup'); grid.setAttribute('aria-label', name + ' themes');
      var anyOn = list.some(isOn);
      list.forEach(function (t, i) { grid.appendChild(tile(t, anyOn ? isOn(t) : i === 0)); });
      grid.addEventListener('keydown', function (e) { arrow(e, grid, list); });
      section.append(head, grid);
      return section;
    }
    /* Arrow keys move through a group and pick as they go, like any radio group. */
    function arrow(e, grid, list) {
      var keys = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'];
      if (keys.indexOf(e.key) < 0) return;
      var cols = getComputedStyle(grid).gridTemplateColumns.split(' ').length || 1;
      var at = list.findIndex(function (t) { return document.activeElement && document.activeElement.dataset.id === t.id; });
      var n = at;
      if (e.key === 'ArrowLeft') n = at - 1; else if (e.key === 'ArrowRight') n = at + 1;
      else if (e.key === 'ArrowUp') n = at - cols; else if (e.key === 'ArrowDown') n = at + cols;
      else if (e.key === 'Home') n = 0; else n = list.length - 1;
      e.preventDefault();
      if (n < 0 || n >= list.length) return;
      var id = list[n].id;
      choose(id);
      var next = scroll.querySelector('[data-id="' + id + '"]');
      if (next) next.focus();
    }
    function fill() {
      var q = search.value.trim().toLowerCase();
      var hit = function (t) { return !q || (t.name + ' ' + t.family).toLowerCase().indexOf(q) >= 0; };
      var dark = THEMES.filter(function (t) { return t.scheme === 'dark' && hit(t); });
      var light = THEMES.filter(function (t) { return t.scheme === 'light' && hit(t); });
      var focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.id : null;
      scroll.replaceChildren();
      if (dark.length) scroll.appendChild(group('Dark', dark));
      if (light.length) scroll.appendChild(group('Light', light));
      if (!dark.length && !light.length) scroll.appendChild(el('p', 'lu-thm-empty lu-muted', 'No theme matches “' + search.value + '”.'));
      autoNote.textContent = pref.mode === 'auto'
        ? byId[pref.dark].name + ' in dark mode, ' + byId[pref.light].name + ' in light mode'
        : 'Pick one theme, or let your device decide';
      autoSwitch.setAttribute('aria-checked', String(pref.mode === 'auto'));
      if (focused) { var again = scroll.querySelector('[data-id="' + focused + '"]'); if (again) again.focus(); }
    }
    search.addEventListener('input', fill);
    autoSwitch.addEventListener('click', function () { setAuto(pref.mode !== 'auto'); });
    pop.addEventListener('rp:popup-open', function () {
      search.value = '';
      scroll.scrollTop = 0;
      fill();
    });
    document.addEventListener('reprise:theme', function () { if (!pop.hidden) fill(); });
  }

  window.RepriseTheme = {
    themes: THEMES,
    active: active,
    preference: function () { return Object.assign({}, pref); },
    choose: choose,
    setAuto: setAuto,
    toggleScheme: toggleScheme,
    mountPicker: mountPicker
  };
})();
