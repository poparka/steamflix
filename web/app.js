/* SteamFlix front end. Talks to the local Flask API in server.py. */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const OPTS_KEY = 'steamflix.options';
// `greeted` is what makes the first run a first run: until someone has answered
// the welcome panel, SteamFlix does not decide for them how much to fetch.
const DEFAULT_OPTS = { light: true, lazyArt: true, greeted: false };

function loadOptions() {
  try {
    return { ...DEFAULT_OPTS, ...JSON.parse(localStorage.getItem(OPTS_KEY) || '{}') };
  } catch (e) { return { ...DEFAULT_OPTS }; }
}

function saveOptions() {
  try { localStorage.setItem(OPTS_KEY, JSON.stringify(state.opts)); } catch (e) { /* private mode */ }
}

const state = {
  view: 'home',
  opts: loadOptions(),
  browse: {
    offset: 0, limit: 60, total: 0, q: '', scope: 'all', sort: 'newest',
    developer: '', publisher: '', genre: '', year: '', loading: false,
  },
  home: { shelves: 0, moreShelves: false, loading: false, arrange: 'mixed' },
  heroes: [], heroIndex: 0,
  favourites: new Set(),
  activeJobs: new Map(),          // depot -> the job currently moving it
  facets: null,
  detail: null,
  installedDepots: new Set(),
  // Cards are one per game, and the depot a card represents is not necessarily
  // the depot that was downloaded, so ownership is tracked by app id too.
  installedApps: new Set(),
  lastLogId: 0,
  timers: {},
};

// How much the first paint asks for. Light start keeps the opening request to
// two short shelves; everything past that is fetched only when asked for.
const PAGE = {
  // Ten shelves is the full home screen; the light start opens with three and
  // fetches the rest when asked.
  get shelvesFirst() { return state.opts.light ? 3 : 10; },
  get perShelf() { return state.opts.light ? 12 : 24; },
  get shelfStep() { return state.opts.light ? 12 : 24; },
  get browseLimit() { return state.opts.light ? 24 : 60; },
  get shelfStride() { return state.opts.light ? 3 : 5; },
};

/* ------------------------------------------------------------------ utils */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) {
    const err = new Error((data && data.error) || `${res.status} ${res.statusText}`);
    err.data = data;
    throw err;
  }
  return data;
}

function bytes(n) {
  if (n === null || n === undefined) return '—';
  if (n === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
}

function duration(sec) {
  if (sec === null || sec === undefined) return '—';
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

function shortDate(s) {
  if (!s) return '—';
  return String(s).split(' ')[0];
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function toast(msg, kind = '', detail = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.innerHTML = `<div>${esc(msg)}</div>${detail ? `<small>${esc(detail)}</small>` : ''}`;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 5200);
}

/* ------------------------------------------------------------------ cards */
function cardArt(item, kind = 'portrait') {
  const url = item.art && item.art[kind];
  if (!url) return null;
  return url;
}

// Cover art is the only thing on this page that hits a remote server per card,
// so off-screen cards keep their art unloaded until they scroll in.
const artObserver = ('IntersectionObserver' in window)
  ? new IntersectionObserver((entries, obs) => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        obs.unobserve(entry.target);
        applyArt(entry.target);
      });
    }, { rootMargin: '400px 0px', threshold: 0 })
  : null;

// A stable colour per title: the same game keeps the same cover between runs,
// and neighbouring cards do not come out the same shade.
function titleHue(text) {
  let h = 0;
  for (let i = 0; i < text.length; i++) h = (h * 31 + text.charCodeAt(i)) % 360;
  return h;
}

function letteredCover(el, item) {
  const name = item && item.name ? String(item.name) : `Depot ${el.dataset.depot}`;
  const generic = !item || !item.identified;
  const h = titleHue(name);
  el.className = 'card-art lettered';
  el.style.backgroundImage = '';
  el.style.setProperty('--c1', `hsl(${h} 46% 30%)`);
  el.style.setProperty('--c2', `hsl(${(h + 38) % 360} 52% 13%)`);
  el.innerHTML = `<div class="lt-name">${esc(name)}</div>` +
    `<div class="lt-sub">${generic ? 'Unidentified depot' : 'No cover art'}</div>`;
}

function placeholderArt(el, item) {
  // Kept for callers that only have a depot id to hand.
  letteredCover(el, typeof item === 'object' ? item : null);
}

const artCache = new Map();       // url -> true/false, so a 404 is asked once

function applyArt(el) {
  const item = el._item || null;
  const urls = (el.dataset.art || '').split('|').filter(Boolean);
  if (!urls.length) { letteredCover(el, item); return; }

  // Walk the candidates in order: portrait, then the landscape header, then
  // fall back to a lettered cover. Steam has no art at all for most 2004 depots.
  const tryNext = i => {
    if (i >= urls.length) { letteredCover(el, item); return; }
    const url = urls[i];
    if (artCache.get(url) === false) { tryNext(i + 1); return; }
    const probe = new Image();
    probe.onload = () => {
      artCache.set(url, true);
      el.classList.remove('pending');
      el.style.backgroundImage = `url('${url}')`;
    };
    probe.onerror = () => { artCache.set(url, false); tryNext(i + 1); };
    probe.src = url;
  };
  tryNext(0);
}

// "Load every cover now": stop waiting for the viewport and fetch the lot.
function loadAllArt() {
  const pending = $$('.card-art.pending');
  pending.forEach(el => {
    if (artObserver) artObserver.unobserve(el);
    applyArt(el);
  });
  return pending.length;
}

function heartButton(item, cls) {
  const on = state.favourites.has(item.depot);
  return `<button class="heart-btn ${cls}" data-fav="${item.depot}"
            aria-pressed="${on}" title="${on ? 'Remove from favourites' : 'Add to favourites'}"
          >${on ? '&#9829;' : '&#9825;'}</button>`;
}

// Favouriting is per game, and a game owns many depots, so the local set has to
// learn about every depot the server just hearted or the icon flickers back off
// the next time a different depot of the same game represents it.
async function toggleFavourite(depot) {
  const on = state.favourites.has(depot);
  try {
    const r = await api(`/api/favourites/${depot}`, { method: on ? 'DELETE' : 'POST' });
    if (on) {
      await loadFavourites();
    } else {
      (r.depots || [depot]).forEach(d => state.favourites.add(d));
    }
  } catch (e) {
    toast('Could not update favourites', 'bad', e.message);
    return;
  }
  syncHearts();
}

function syncHearts() {
  $$('[data-fav]').forEach(btn => {
    const on = state.favourites.has(Number(btn.dataset.fav));
    btn.setAttribute('aria-pressed', String(on));
    btn.innerHTML = on ? '&#9829;' : '&#9825;';
    btn.title = on ? 'Remove from favourites' : 'Add to favourites';
  });
}

async function loadFavourites() {
  try {
    const r = await api('/api/favourites');
    state.favourites = new Set(r.depots);
  } catch (e) { /* leave whatever we had */ }
}

function isInstalled(item) {
  return state.installedDepots.has(item.depot)
      || (!!item.appid && state.installedApps.has(item.appid));
}

// Cards already on screen when a download finishes need the badge too, so the
// job poller repaints them rather than waiting for a navigation.
function markInstalledCards() {
  $$('.card[data-depot]').forEach(el => {
    const depot = Number(el.dataset.depot);
    const item = ($('.card-art', el) || {})._item;
    const owned = state.installedDepots.has(depot)
      || (item && item.appid && state.installedApps.has(item.appid));
    const flags = $('.card-flags', el);
    if (!flags) return;
    const has = !!$('.badge.owned', flags);
    if (owned && !has) {
      flags.insertAdjacentHTML('beforeend',
        '<span class="badge owned">In library</span>');
    } else if (!owned && has) {
      $('.badge.owned', flags).remove();
    }
  });
}

function cardSubtitle(item) {
  // A grouped card stands for a whole game, so it counts the game's depots and
  // versions rather than just the one depot that happens to represent it.
  return item.grouped
    ? `${item.total_versions.toLocaleString()} versions · ${item.depot_count} depots`
    : `${item.versions} version${item.versions === 1 ? '' : 's'} · depot ${item.depot}`;
}

function renderCard(item) {
  const el = document.createElement('div');
  el.className = 'card';
  el.dataset.depot = item.depot;

  const flags = [];
  if (item.reset) flags.push('<span class="badge reset">Reset</span>');
  if (!item.has_key) flags.push('<span class="badge nokey">No key</span>');
  if (item.confidence === 'likely') flags.push('<span class="badge guess">Guess</span>');
  if (isInstalled(item)) flags.push('<span class="badge owned">In library</span>');

  const urls = [cardArt(item, 'portrait'), cardArt(item, 'header')].filter(Boolean);
  const studio = item.developer || item.publisher || '';

  el.innerHTML = `
    <div class="card-art ${urls.length ? 'pending' : ''}"
         data-depot="${item.depot}" data-art="${urls.join('|')}"></div>
    <div class="card-flags">${flags.join('')}</div>
    ${heartButton(item, 'card-heart')}
    <div class="card-hover"></div>
    <div class="card-body">
      <div class="card-title">${esc(item.name)}</div>
      <div class="card-sub">${cardSubtitle(item)}</div>
      ${studio || item.year ? `<div class="card-meta-row">
          <span>${esc(studio)}</span>
          ${item.year ? `<span class="yr">${esc(item.year)}</span>` : ''}
        </div>` : ''}
    </div>`;

  const artEl = $('.card-art', el);
  artEl._item = item;
  if (urls.length) {
    if (state.opts.lazyArt && artObserver) artObserver.observe(artEl);
    else applyArt(artEl);
  } else {
    letteredCover(artEl, item);
  }

  el.addEventListener('click', ev => {
    if (ev.target.closest('[data-fav]')) return;      // the heart is not the card
    openDetail(item.depot);
  });
  el.addEventListener('mouseenter', () => fillHover(el, item));
  paintCardJob(el);
  return el;
}

/* --------------------------------------------------- hover / live progress */
function jobLine(job) {
  const pct = job.bytes_total ? Math.round(job.bytes_done / job.bytes_total * 100) : 0;
  const label = {
    queued: 'Queued', planning: 'Working out the chain', downloading: 'Downloading',
    extracting: 'Extracting', done: 'Ready', failed: 'Failed', cancelled: 'Cancelled',
  }[job.state] || job.state;
  const right = job.state === 'downloading'
    ? `${pct}% · ${bytes(job.speed || 0)}/s`
    : (job.state === 'extracting' ? (job.current || '') : '');
  return { pct, label, right };
}

function fillHover(el, item) {
  const box = $('.card-hover', el);
  if (!box) return;
  const job = state.activeJobs.get(item.depot);
  if (job) {
    const { pct, label, right } = jobLine(job);
    box.innerHTML = `
      <div class="hv-line"><b>${esc(label)}</b><span>${esc(right)}</span></div>
      <div class="hv-bar"><div class="hv-fill" style="width:${pct}%"></div></div>`;
    return;
  }
  const bits = [];
  if (item.year) bits.push(item.year);
  if (item.developer) bits.push(item.developer);
  if ((item.genres || []).length) bits.push(item.genres.slice(0, 2).join(', '));
  const span = (item.first_date && item.last_date)
    ? `${shortDate(item.first_date)} → ${shortDate(item.last_date)}` : '';
  box.innerHTML = `
    <div class="hv-line"><b>${esc(bits.join(' · '))}</b></div>
    ${span ? `<div class="hv-line"><span>${esc(span)}</span></div>` : ''}`;
}

// Repainted whenever the job poll returns, so a card already on screen picks up
// a download that was started from somewhere else.
function paintCardJob(el) {
  const depot = Number(el.dataset.depot);
  const job = state.activeJobs.get(depot);
  const existing = $('.card-progress', el);
  if (!job) {
    if (existing) existing.remove();
    el.classList.remove('busy');
    return;
  }
  const { pct } = jobLine(job);
  el.classList.add('busy');
  if (existing) {
    $('i', existing).style.width = pct + '%';
  } else {
    const bar = document.createElement('div');
    bar.className = 'card-progress';
    bar.innerHTML = `<i style="width:${pct}%"></i>`;
    el.appendChild(bar);
  }
  if (el.matches(':hover')) {
    const { label, right } = jobLine(job);
    const box = $('.card-hover', el);
    if (box) {
      box.innerHTML = `
        <div class="hv-line"><b>${esc(label)}</b><span>${esc(right)}</span></div>
        <div class="hv-bar"><div class="hv-fill" style="width:${pct}%"></div></div>`;
    }
  }
}

function repaintAllCards() {
  $$('.card[data-depot]').forEach(paintCardJob);
}

/* ------------------------------------------------- session state on reload */
const SESSION_KEY = 'steamflix.session';

function saveSession() {
  try {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify({
      view: state.view,
      q: state.browse.q,
      scope: state.browse.scope,
      sort: state.browse.sort,
      developer: state.browse.developer,
      publisher: state.browse.publisher,
      genre: state.browse.genre,
      year: state.browse.year,
      arrange: state.home.arrange,
      offset: state.browse.offset,
      scroll: window.scrollY,
      depot: state.detail ? state.detail.depot : null,
      intro: true,
      at: Date.now(),
    }));
  } catch (e) { /* private mode */ }
}

function loadSession() {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const s = JSON.parse(raw);
    return (Date.now() - (s.at || 0) < 12 * 3600 * 1000) ? s : null;
  } catch (e) { return null; }
}

/* ------------------------------------------------------------------- boot */
function playIntro(done) {
  const el = $('#boot');
  // The intro is for a cold start only; a reload drops you straight back in.
  if (state.resume && state.resume.intro) {
    el.hidden = true;
    $('#app').hidden = false;
    done();
    return;
  }
  if (state.opts.greeted && state.skipIntro) {
    el.hidden = true;
    $('#app').hidden = false;
    done();
    return;
  }
  $('#app').hidden = false;
  el.classList.add('intro');

  let finished = false;
  const finish = () => {
    if (finished) return;
    finished = true;
    // Hide before dropping .intro: the class carries the fade-out's `forwards`
    // fill, so removing it first would snap the overlay back to fully opaque.
    el.hidden = true;
    el.classList.remove('intro');
    el.removeEventListener('animationend', onEnd);
    done();
  };
  // Child animations bubble their own animationend up here first, so the
  // listener cannot be a one-shot - it has to wait for the overlay's own.
  const onEnd = ev => { if (ev.animationName === 'boot-out') finish(); };
  el.addEventListener('animationend', onEnd);
  setTimeout(finish, 3000);           // belt and braces if animations are off
}

async function boot() {
  let status;
  try {
    status = await api('/api/status');
  } catch (e) {
    $('#boot-msg').textContent = 'Cannot reach the SteamFlix server.';
    $('#boot-sub').textContent = e.message;
    setTimeout(boot, 2000);
    return;
  }

  if (!status.index_built) {
    const idx = status.index || {};
    $('#boot-msg').textContent = {
      download: 'Downloading the mirror file listings…',
      parse: 'Reading the file names…',
      store: 'Writing the catalogue…',
      aggregate: 'Working out versions and resets…',
    }[idx.step] || 'Building the catalogue from the mirror…';
    $('#boot-sub').textContent = idx.detail || '';

    let pct = { download: 20, parse: 55, store: 82, aggregate: 93 }[idx.step] || 10;
    if (idx.step === 'download' && idx.total_bytes) {
      pct = 8 + (idx.bytes / idx.total_bytes) * 30;
      $('#boot-sub').textContent =
        `${idx.detail} — ${bytes(idx.bytes)} of ${bytes(idx.total_bytes)}`;
    }
    $('#boot-bar').style.width = pct + '%';

    if (idx.recent && idx.recent.length) {
      $('#boot-stream').innerHTML = idx.recent.map(f => `<div>${esc(f)}</div>`).join('');
    }
    if (idx.seen) {
      $('#boot-stats').textContent =
        `${idx.seen.toLocaleString()} files read` +
        (idx.depots ? ` · ${idx.depots.toLocaleString()} depots found` : '');
    }
    setTimeout(boot, 600);
    return;
  }

  $('#boot-bar').style.width = '100%';
  $('#boot-msg').textContent = 'Catalogue ready';
  $('#boot-stats').textContent =
    `${(status.counts.files || 0).toLocaleString()} files · ` +
    `${(status.counts.titles || status.counts.depots || 0).toLocaleString()} titles`;

  state.resume = loadSession();
  if (state.resume && state.resume.arrange) state.home.arrange = state.resume.arrange;

  // First visit: ask before fetching a wall of covers on someone's behalf.
  if (!state.opts.greeted) {
    $('#app').hidden = false;
    $('#boot').hidden = true;
    await askFirstRun((status.counts && status.counts.titles) || 0);
    state.skipIntro = true;
  }
  // The home screen must never be able to keep the overlay up: whatever fails
  // here, the app still gets revealed and the error goes to a toast.
  try {
    await Promise.all([loadInstalled(), loadFavourites()]);
  } catch (e) {
    console.error('library state failed', e);
  }
  try {
    await loadHome();
  } catch (e) {
    console.error('home shelves failed', e);
    $('#shelves').innerHTML =
      `<div class="note bad">Could not build the home screen: ${esc(e.message)}</div>`;
  }

  playIntro(() => restoreSession());

  refreshStorage();
  pollJobs();
  pollLogs();
  refreshStatusPanel(status);

  state.timers.storage = setInterval(refreshStorage, 15000);
  state.timers.jobs = setInterval(pollJobs, 1500);
  state.timers.logs = setInterval(pollLogs, 5000);
  state.timers.status = setInterval(() => api('/api/status').then(refreshStatusPanel), 10000);
  setInterval(saveSession, 2000);
  window.addEventListener('beforeunload', saveSession);
}

async function restoreSession() {
  const s = state.resume;
  if (!s) return;

  state.browse.q = s.q || '';
  state.browse.scope = s.scope || 'all';
  state.browse.sort = s.sort || 'name';
  state.browse.developer = s.developer || '';
  state.browse.publisher = s.publisher || '';
  state.browse.genre = s.genre || '';
  state.browse.year = s.year || '';
  $('#search').value = state.browse.q;
  $('#scope').value = state.browse.scope;
  $('#sort').value = state.browse.sort;
  $$('#arrange .seg-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.arrange === state.home.arrange));
  await refreshFacets();

  if (s.view && s.view !== 'home') switchView(s.view, true);

  if (s.view === 'browse') {
    // Re-fetch everything you had scrolled through, not just the first page.
    const want = Math.max(state.browse.limit, s.offset || 0);
    state.browse.offset = 0;
    $('#browse-grid').innerHTML = '';
    while (state.browse.offset < want) {
      const before = state.browse.offset;
      await loadBrowse(false);
      if (state.browse.offset === before) break;
    }
  }

  if (s.scroll) window.scrollTo({ top: s.scroll, behavior: 'auto' });
  if (s.depot) openDetail(s.depot);
}

/* ------------------------------------------------------------------- home */
async function loadHome() {
  const qs = new URLSearchParams({
    per: PAGE.perShelf, shelves: PAGE.shelvesFirst, offset: 0,
    arrange: state.home.arrange,
  });
  const data = await api(`/api/rows?${qs}`);

  state.heroes = data.heroes && data.heroes.length
    ? data.heroes : (data.hero ? [data.hero] : []);
  state.heroIndex = 0;
  startHero();

  $('#shelves').innerHTML = '';
  state.home.shelves = 0;
  appendShelves(data);
}

/* -------------------------------------------------------------------- hero */
function heroDesc(h) {
  const made = [h.developer, h.year].filter(Boolean).join(' · ');
  return (made ? made + '. ' : '') +
    `Preserved on the mirror from ${shortDate(h.first_date)} to ${shortDate(h.last_date)}. ` +
    `Steam2 stores versions as deltas, so SteamFlix pulls the whole chain up to the ` +
    `one you pick.`;
}

// Every hero paint gets a ticket. A cross-fade finishes ~900ms after it starts,
// and by then the timer or a click may have moved on - without this the stale
// fade writes an old cover over the new one.
let heroTicket = 0;

function paintHero(index, animate = true) {
  const h = state.heroes[index];
  if (!h) return;
  state.heroIndex = index;
  state.hero = h;
  const ticket = ++heroTicket;

  // Steam has no library_hero.jpg for plenty of 2005-2012 apps, so the wide art
  // falls back the same way a card's cover does. Two rules keep the panel from
  // ever going black between titles: the outgoing image is never cleared until
  // the incoming one has actually decoded, and a title with no art at all gets
  // a lettered backdrop rather than nothing.
  const urls = ['hero', 'capsule', 'header', 'portrait']
    .map(kind => cardArt(h, kind)).filter(Boolean);
  const cur = $('#hero-art');
  const next = $('#hero-art-next');

  const paintLettered = target => {
    const hue = titleHue(h.name || '');
    target.classList.add('lettered');
    target.style.backgroundImage = 'none';
    target.style.setProperty('--c1', `hsl(${hue} 44% 26%)`);
    target.style.setProperty('--c2', `hsl(${(hue + 38) % 360} 50% 10%)`);
    target.innerHTML = `<div class="lt-name">${esc(h.name || '')}</div>`;
  };

  const commit = (url) => {
    if (ticket !== heroTicket) return;
    if (url) {
      cur.classList.remove('lettered');
      cur.innerHTML = '';
      cur.style.backgroundImage = `url('${url}')`;
    } else {
      paintLettered(cur);
    }
    next.classList.remove('showing');
    next.innerHTML = '';
    next.classList.remove('lettered');
  };

  const show = (url) => {
    if (ticket !== heroTicket) return;
    if (!animate) { commit(url); return; }
    if (url) {
      next.classList.remove('lettered');
      next.innerHTML = '';
      next.style.backgroundImage = `url('${url}')`;
    } else {
      paintLettered(next);
    }
    next.classList.add('showing');
    setTimeout(() => commit(url), 900);
  };

  // Preload before fading, or the cross-fade reveals an empty panel first.
  const tryNext = i => {
    if (ticket !== heroTicket) return;
    if (i >= urls.length) { show(null); return; }
    const url = urls[i];
    if (artCache.get(url) === false) { tryNext(i + 1); return; }
    const probe = new Image();
    probe.onerror = () => { artCache.set(url, false); tryNext(i + 1); };
    probe.onload = () => { artCache.set(url, true); show(url); };
    probe.src = url;
  };
  tryNext(0);

  $('#hero-title').textContent = h.name;
  $('#hero-meta').innerHTML = [
    h.year ? `<span class="badge plain">${esc(h.year)}</span>` : '',
    `<span class="badge plain">${(h.total_versions || h.versions).toLocaleString()} versions</span>`,
    h.developer ? `<span class="badge plain">${esc(h.developer)}</span>` : '',
    (h.genres || []).length ? `<span class="badge plain">${esc(h.genres[0])}</span>` : '',
    h.reset ? '<span class="badge reset">Reset</span>' : '',
    h.has_key ? '<span class="badge key">Key available</span>' : '<span class="badge nokey">No key</span>',
  ].filter(Boolean).join('');
  $('#hero-desc').textContent = heroDesc(h);
  $('#hero-play').onclick = () => openDetail(h.depot);
  $('#hero-info').onclick = () => openDetail(h.depot);

  const heart = $('#hero-heart');
  heart.dataset.fav = h.depot;
  syncHearts();

  $('#hero-dots').innerHTML = state.heroes.map((_, i) =>
    `<button type="button" class="hero-dot ${i === index ? 'active' : ''}"
       data-hero="${i}" title="Show title ${i + 1}"></button>`).join('');
}

function heroTimer() {
  clearInterval(state.timers.hero);
  if (state.heroes.length < 2) return;
  state.timers.hero = setInterval(() => {
    // Rotating behind a modal or another tab just wastes the viewer's turn.
    if (document.hidden || state.view !== 'home' || !$('#modal').hidden) return;
    paintHero((state.heroIndex + 1) % state.heroes.length);
  }, 9000);
}

function startHero() {
  if (!state.heroes.length) return;
  paintHero(0, false);
  heroTimer();
}

function showHero(index) {
  paintHero(index);
  heroTimer();          // restart the clock so a manual pick gets its full turn
}

function appendShelves(data) {
  const wrap = $('#shelves');
  data.shelves.forEach(shelf => {
    const sec = document.createElement('div');
    sec.className = 'shelf';
    sec.dataset.key = shelf.key;
    sec.dataset.offset = shelf.offset;
    sec.innerHTML = `<div class="shelf-head">
        <h3>${esc(shelf.title)}</h3>
        <p>${esc(shelf.subtitle)}</p>
      </div><div class="shelf-scroll"></div>`;
    const scroll = $('.shelf-scroll', sec);
    shelf.items.forEach(item => scroll.appendChild(renderCard(item)));
    if (shelf.more) scroll.appendChild(shelfMoreTile(sec));
    wrap.appendChild(sec);
  });

  state.home.shelves = data.shelf_offset + data.shelves.length;
  state.home.moreShelves = !!data.more_shelves;
  $('#more-shelves').hidden = !data.more_shelves;
  $('#home-status').textContent = data.more_shelves
    ? `${state.home.shelves} of ${data.shelf_total} shelves loaded`
    : '';
}

// The "more" affordance lives at the end of the shelf itself, which is where
// you already are once you have scrolled through the first dozen cards.
function shelfMoreTile(sec) {
  const tile = document.createElement('button');
  tile.className = 'shelf-more';
  tile.type = 'button';
  tile.textContent = 'Load more →';
  tile.addEventListener('click', async () => {
    tile.disabled = true;
    tile.innerHTML = '<span class="spinner"></span>';
    try {
      const qs = new URLSearchParams({ offset: sec.dataset.offset, limit: PAGE.shelfStep,
                                       arrange: state.home.arrange });
      const page = await api(`/api/shelf/${sec.dataset.key}?${qs}`);
      const scroll = $('.shelf-scroll', sec);
      page.items.forEach(item => scroll.insertBefore(renderCard(item), tile));
      sec.dataset.offset = page.offset;
      if (!page.more) tile.remove();
    } catch (e) {
      toast('Could not load more', 'bad', e.message);
    } finally {
      tile.disabled = false;
      tile.textContent = 'Load more →';
    }
  });
  return tile;
}

async function loadMoreShelves() {
  if (state.home.loading || !state.home.moreShelves) return;
  state.home.loading = true;
  const btn = $('#more-shelves');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Loading';
  try {
    const qs = new URLSearchParams({
      per: PAGE.perShelf, shelves: PAGE.shelfStride, offset: state.home.shelves,
      arrange: state.home.arrange,
    });
    appendShelves(await api(`/api/rows?${qs}`));
  } catch (e) {
    toast('Could not load more shelves', 'bad', e.message);
  } finally {
    state.home.loading = false;
    btn.disabled = false;
    btn.textContent = 'Show more shelves';
  }
}

/* ----------------------------------------------------------------- browse */
function browseParams(extra = {}) {
  const b = state.browse;
  const p = { q: b.q, scope: b.scope, sort: b.sort, ...extra };
  if (b.developer) p.developer = b.developer;
  if (b.publisher) p.publisher = b.publisher;
  if (b.genre) p.genre = b.genre;
  if (b.year) p.year = b.year;
  return p;
}

async function loadBrowse(reset = false) {
  if (state.browse.loading) return;
  state.browse.loading = true;
  if (reset) {
    state.browse.offset = 0;
    state.browse.limit = PAGE.browseLimit;
    $('#browse-grid').innerHTML = '';
  }
  $('#browse-status').textContent = 'Loading…';
  renderChips();

  const b = state.browse;
  const qs = new URLSearchParams(browseParams({ offset: b.offset, limit: b.limit }));
  try {
    const data = await api(`/api/library?${qs}`);
    b.total = data.total;
    const grid = $('#browse-grid');
    data.items.forEach(item => grid.appendChild(renderCard(item)));
    b.offset += data.items.length;
    $('#browse-count').textContent =
      `${data.total.toLocaleString()} title${data.total === 1 ? '' : 's'}`;
    $('#load-more').hidden = b.offset >= data.total;
    $('#browse-status').textContent = b.offset >= data.total
      ? (data.total ? 'End of list' : 'Nothing matched') : '';
  } catch (e) {
    $('#browse-status').textContent = e.message;
  } finally {
    state.browse.loading = false;
  }
  if (reset) refreshFacets();
}

/* ----------------------------------------------------------------- facets */
// Counts are recomputed against the current filters, so the studio list narrows
// as you pick a genre instead of offering studios with nothing left to show.
function fillSelect(el, options, current, allLabel) {
  const chosen = current || '';
  const known = options.some(o => o.value === chosen);
  el.innerHTML =
    `<option value="">${allLabel}</option>` +
    (chosen && !known ? `<option value="${esc(chosen)}" selected>${esc(chosen)}</option>` : '') +
    options.map(o =>
      `<option value="${esc(o.value)}" ${o.value === chosen ? 'selected' : ''}>${
        esc(o.label)}</option>`).join('');
  el.value = chosen;
  el.disabled = !options.length && !chosen;
}

async function refreshFacets() {
  let f;
  try {
    f = await api(`/api/facets?${new URLSearchParams(browseParams())}`);
  } catch (e) {
    return;
  }
  state.facets = f;

  fillSelect($('#genre'),
    f.genres.map(g => ({ value: String(g.id), label: `${g.name} (${g.count})` })),
    state.browse.genre, 'All genres');
  fillSelect($('#year'),
    (f.years || []).map(y => ({ value: y.year, label: `${y.year} (${y.count})` })),
    state.browse.year, 'All years');
  fillSelect($('#developer'),
    f.developers.map(d => ({ value: d.name, label: `${d.name} (${d.count})` })),
    state.browse.developer, 'All studios');
  fillSelect($('#publisher'),
    f.publishers.map(d => ({ value: d.name, label: `${d.name} (${d.count})` })),
    state.browse.publisher, 'All publishers');

  const d = f.details || {};
  if (d.running) {
    $('#browse-status').textContent =
      `Filling in studios and genres… ${d.done || 0}/${d.total || 0}`;
    clearTimeout(state.timers.facets);
    state.timers.facets = setTimeout(refreshFacets, 4000);
  }
}

function genreName(id) {
  const g = (state.facets && state.facets.genres || []).find(x => String(x.id) === String(id));
  return g ? g.name : `Genre ${id}`;
}

function renderChips() {
  const b = state.browse;
  const chips = [];
  if (b.scope === 'favourite') chips.push(['scope', 'Showing', 'Favourites']);
  if (b.year) chips.push(['year', 'Year', b.year]);
  if (b.genre) chips.push(['genre', 'Genre', genreName(b.genre)]);
  if (b.developer) chips.push(['developer', 'Studio', b.developer]);
  if (b.publisher) chips.push(['publisher', 'Publisher', b.publisher]);
  if (b.q) chips.push(['q', 'Search', b.q]);

  const box = $('#browse-chips');
  box.hidden = !chips.length;
  if (!chips.length) return;
  box.innerHTML = chips.map(([key, label, value]) =>
    `<span class="chip"><small>${label}</small><b>${esc(value)}</b>
       <button type="button" data-drop="${key}" title="Remove">&times;</button></span>`).join('')
    + '<button type="button" class="chip clear" data-drop="all">Clear all</button>';
}

function setFilter(key, value) {
  state.browse[key] = value || (key === 'scope' ? 'all' : '');
  if (key === 'q') $('#search').value = state.browse.q;
  if (key === 'scope') $('#scope').value = state.browse.scope;
  if (state.view !== 'browse') switchView('browse');
  loadBrowse(true);
  saveSession();
}

function clearFilters() {
  Object.assign(state.browse,
    { q: '', developer: '', publisher: '', genre: '', year: '', scope: 'all' });
  $('#search').value = '';
  $('#scope').value = 'all';
  loadBrowse(true);
  saveSession();
}

/* ---------------------------------------------------------------- library */
async function loadInstalled() {
  const data = await api('/api/installed');
  state.installed = data.items;
  state.installedDepots = new Set(data.items.map(i => i.depot));
  state.installedApps = new Set(data.items.map(i => i.appid).filter(Boolean));
  return data.items;
}

async function renderLibrary() {
  // Strictly what is extracted on this machine - the catalogue is Browse's job.
  const items = await loadInstalled();
  markInstalledCards();
  const grid = $('#library-grid');
  grid.innerHTML = '';
  $('#library-empty').hidden = items.length > 0;
  $('#library-count').textContent = items.length
    ? `${items.length} title${items.length === 1 ? '' : 's'} · ${bytes(items.reduce((a, i) => a + i.bytes, 0))}`
    : '';

  items.forEach(item => {
    const el = document.createElement('div');
    el.className = 'card';
    const art = item.appid
      ? `https://cdn.akamai.steamstatic.com/steam/apps/${item.appid}/library_600x900.jpg`
      : null;
    el.innerHTML = `
      <div class="card-art ${art ? 'pending' : ''}" data-depot="${item.depot}"
           data-art="${art || ''}"></div>
      <div class="card-flags"><span class="badge owned">v${item.version}</span></div>
      <div class="card-body">
        <div class="card-title">${esc(item.name)}</div>
        <div class="card-sub">${item.files} files · ${bytes(item.bytes)}</div>
      </div>`;
    // Installed titles get the same art pipeline as the catalogue, so a game
    // with no cover on the CDN shows its name rather than a grey box.
    const artEl = $('.card-art', el);
    artEl._item = { ...item, identified: true };
    if (art) applyArt(artEl); else letteredCover(artEl, artEl._item);

    el.addEventListener('click', () => openPlayer(item));
    grid.appendChild(el);
  });
  refreshStorage();
}

/* ---------------------------------------------------------------- storage */
async function refreshStorage() {
  let s;
  try { s = await api('/api/storage'); } catch (e) { return; }
  state.storage = s;

  // The pill shows what is actually left to download on this machine - the
  // tighter of the drive's own free space and any budget the user has set -
  // and names the drive, because "100 GB left" means nothing on its own.
  const pct = s.budget ? Math.min(100, s.budget_used / s.budget * 100) : 0;
  $('#storage-mini-fill').style.width = pct + '%';
  $('#storage-mini-fill').style.background =
    pct > 92 ? 'var(--bad)' : pct > 75 ? 'var(--warn)' : 'var(--good)';
  const drive = (s.drive || '').replace(/\\$/, '');
  $('#storage-mini-text').textContent = `${bytes(s.usable)} free${drive ? ' on ' + drive : ''}`;
  $('#storage-btn').title = s.budget_capped
    ? `${bytes(s.budget_left)} left of your ${bytes(s.budget)} budget · `
      + `${bytes(s.free)} free on ${drive}`
    : `${bytes(s.free)} free on ${drive}, minus a ${bytes(s.reserve)} reserve`;

  const cachePct = s.budget ? s.cache_bytes / s.budget * 100 : 0;
  const extPct = s.budget ? s.extracted_bytes / s.budget * 100 : 0;

  $('#storage-panel').innerHTML = `
    <div class="storage-title">
      <strong>SteamFlix budget · ${esc(s.path)}</strong>
      <span>${bytes(s.budget_used)} of ${bytes(s.budget)} used · ${bytes(s.budget_left)} left</span>
    </div>
    <div class="budget-bar">
      <div class="storage-seg cache" style="width:${cachePct}%"
           title="Blob and dat cache — ${bytes(s.cache_bytes)}. These are the raw
downloaded files. Deleting them frees space and keeps the extracted game."></div>
      <div class="storage-seg extracted" style="width:${extPct}%"
           title="Extracted games — ${bytes(s.extracted_bytes)}. The playable files
in your library."></div>
    </div>
    <div class="storage-legend">
      <span><i style="background:var(--accent-dim)"></i>Blob + dat cache ${bytes(s.cache_bytes)}</span>
      <span><i style="background:var(--accent)"></i>Extracted games ${bytes(s.extracted_bytes)}</span>
      <span><i style="background:#23232c"></i>Free for SteamFlix ${bytes(s.budget_left)}</span>
    </div>
    ${s.usable < 5e9 ? `<div class="storage-warn">Only ${bytes(s.usable)} usable right now.
       Delete a depot's blob/dat cache from its page to free space without losing the
       extracted game.</div>` : ''}

    <div class="storage-title" style="margin-top:20px">
      <strong>Drives</strong>
      <span>the library lives on ${esc(s.drive)}</span>
    </div>
    <div class="disk-list">
      ${(s.disks || []).map(d => {
        // A single bar of "used space" never said whose space it was. It is
        // split instead: SteamFlix's own downloads in the theme red, everything
        // else on the drive in grey, and free space left dark. Hovering any of
        // the three says exactly what it is.
        const minePct = d.is_library && d.total ? (s.steamflix / d.total) * 100 : 0;
        const otherPct = Math.max(0, d.percent_used - minePct);
        const tight = d.percent_used > 92;
        return `<div class="disk ${d.is_library ? 'library' : ''}">
          <div class="disk-name">${esc(d.drive)}${d.is_library ? '<small>library</small>' : ''}</div>
          <div class="disk-bar ${tight ? 'tight' : ''}"
               title="${esc(d.drive)} — ${bytes(d.used)} of ${bytes(d.total)} used, ${
                 bytes(d.free)} free${d.is_library
                 ? `. SteamFlix accounts for ${bytes(s.steamflix)} of that (${
                     bytes(s.cache_bytes)} cache, ${bytes(s.extracted_bytes)} extracted).`
                 : '. SteamFlix stores nothing on this drive.'}">
            <span class="seg-mine" style="width:${minePct}%"
                  title="Downloaded by SteamFlix — ${bytes(s.steamflix)}"></span>
            <span class="seg-other" style="width:${otherPct}%"
                  title="Used by everything else — ${bytes(d.used - (d.is_library ? s.steamflix : 0))}"></span>
          </div>
          <div class="disk-free">${bytes(d.free)} free of ${bytes(d.total)}</div>
        </div>`;
      }).join('')}
    </div>
    <div class="disk-legend">
      <span><i style="background:var(--accent)"></i>Downloaded by SteamFlix</span>
      <span><i style="background:#4a4a58"></i>Used by other things</span>
      <span><i style="background:#23232c"></i>Free</span>
    </div>`;
}

/* -------------------------------------------------------------- downloads */
// While a job is still working out its chain there are no byte totals yet, so
// the bar would sit at zero and look stuck. It runs as a moving stripe instead,
// and the hover line always says what is actually happening.
function jobHoverText(job) {
  const pct = job.bytes_total
    ? Math.round(job.bytes_done / job.bytes_total * 100) : null;
  switch (job.state) {
    case 'queued':      return 'Waiting for a free slot…';
    case 'planning':    return 'Following the delta chain back to version 0…';
    case 'downloading': return `${pct}% · ${bytes(job.bytes_done)} of ${bytes(job.bytes_total)}`
                             + (job.speed ? ` · ${bytes(job.speed)}/s` : '')
                             + (job.eta !== null ? ` · ETA ${duration(job.eta)}` : '');
    case 'extracting':  return `Extracting${job.current ? ' — ' + job.current : ''}`;
    case 'done':        return `Finished — ${job.extracted_files || 0} file(s) extracted`;
    case 'failed':      return job.error || 'Failed';
    case 'cancelled':   return 'Cancelled';
    default:            return job.state;
  }
}

async function pollJobs() {
  let data;
  try { data = await api('/api/jobs'); } catch (e) { return; }
  const jobs = data.jobs;
  const active = jobs.filter(j => ['queued', 'planning', 'downloading', 'extracting'].includes(j.state));
  const pill = $('#job-pill');
  pill.hidden = active.length === 0;
  pill.textContent = active.length;

  // Cards anywhere in the app track the same jobs, so a title being downloaded
  // shows its progress on hover without a trip to the Downloads tab.
  const before = state.activeJobs.size;
  state.activeJobs = new Map(active.map(j => [j.depot, j]));
  if (before || state.activeJobs.size) repaintAllCards();

  $('#jobs-empty').hidden = jobs.length > 0;
  const list = $('#job-list');

  jobs.forEach(job => {
    let el = $(`#job-${job.id}`);
    if (!el) {
      el = document.createElement('div');
      el.className = 'job';
      el.id = `job-${job.id}`;
      list.prepend(el);
    }
    const pct = job.bytes_total ? Math.min(100, job.bytes_done / job.bytes_total * 100) : 0;
    const busy = ['queued', 'planning', 'downloading', 'extracting'].includes(job.state);

    el.innerHTML = `
      <div class="job-head">
        <strong>${esc(job.title)}</strong>
        <span class="job-state ${job.state}">${job.state}</span>
        <span class="job-meta">v${job.version}${job.crc ? ` · crc ${job.crc}` : ''}</span>
        <div class="job-actions">
          ${busy ? `<button class="ghost-btn" data-cancel="${job.id}">Cancel</button>` : ''}
          ${job.state === 'done' && job.out_dir
            ? `<button class="ghost-btn" data-open="${esc(job.out_dir)}">Open folder</button>
               <button class="btn accent" style="padding:6px 14px;font-size:12px"
                       data-playjob="${job.depot}">Play / Watch</button>` : ''}
          <button class="ghost-btn" data-log="${job.id}">Log</button>
        </div>
      </div>
      <div class="job-bar ${busy ? 'live' : ''}" title="${esc(job.current || job.state)}">
        <div class="job-bar-fill ${busy && !job.bytes_total ? 'indeterminate' : ''}"
             style="width:${busy && !job.bytes_total ? '100' : pct}%"></div>
      </div>
      <div class="job-hoverline">${esc(jobHoverText(job))}</div>
      <div class="job-meta">
        <span>${bytes(job.bytes_done)} / ${bytes(job.bytes_total)}</span>
        <span>${job.files_done}/${job.files_total} files</span>
        ${job.speed ? `<span>${bytes(job.speed)}/s</span>` : ''}
        ${job.eta !== null && busy ? `<span>ETA ${duration(job.eta)}</span>` : ''}
        ${job.current ? `<span title="${esc(job.current)}">${esc(job.current.slice(0, 46))}…</span>` : ''}
        ${job.key_used ? `<span>key ${esc(job.key_used.slice(0, 8))}…</span>` : ''}
      </div>
      ${job.error ? `<div class="note bad" style="margin-bottom:0">${esc(job.error)}</div>` : ''}
      <div class="job-log" id="joblog-${job.id}" hidden></div>`;

    if (job.state === 'done' && !el.dataset.notified) {
      el.dataset.notified = '1';
      toast(`${job.title} is ready`, 'good',
            job.files_missing
              ? `${job.extracted_files} of ${job.files_expected} files - some are missing`
              : `${job.extracted_files} files extracted`);
      loadInstalled().then(markInstalledCards);
      loadInstalled();
      refreshStorage();
    }
    if (job.state === 'error' && !el.dataset.notified) {
      el.dataset.notified = '1';
      toast(`${job.title} failed`, 'bad', job.error || 'see the Logs panel');
    }
  });
}

async function showJobLog(id) {
  const box = $(`#joblog-${id}`);
  if (!box) return;
  if (!box.hidden) { box.hidden = true; return; }
  const job = await api(`/api/jobs/${id}`);
  box.innerHTML = (job.log || []).map(line =>
    `<div class="${/error|fail/i.test(line) ? 'err' : ''}">${esc(line)}</div>`).join('');
  box.hidden = false;
  box.scrollTop = box.scrollHeight;
}

/* ------------------------------------------------------------------- logs */
async function pollLogs() {
  let data;
  try {
    const cat = $('#log-category').value || 'all';
    const lvl = $('#log-level').value || 'all';
    data = await api(`/api/logs?category=${cat}&level=${lvl}&limit=200`);
  } catch (e) { return; }

  if ($('#log-category').options.length === 1) {
    data.categories.forEach(c => {
      const o = document.createElement('option');
      o.value = c; o.textContent = c;
      $('#log-category').appendChild(o);
    });
  }

  const bad = data.summary.errors + data.summary.warnings;
  const pill = $('#log-pill');
  pill.hidden = bad === 0;
  pill.textContent = bad;

  $('#log-list').innerHTML = data.entries.length ? data.entries.map(e => `
    <div class="log-item ${e.level}">
      <div class="log-cat">${esc(e.category)}</div>
      <div>
        <div class="log-msg">${esc(e.message)}</div>
        ${e.detail ? `<div class="log-detail">${esc(e.detail)}</div>` : ''}
        ${e.hint && e.level !== 'info' ? `<div class="log-hint">${esc(e.hint)}</div>` : ''}
        <div class="log-time">${new Date(e.ts * 1000).toLocaleTimeString()}${e.depot ? ` · depot ${e.depot}` : ''}</div>
      </div>
    </div>`).join('')
    : '<div class="empty" style="padding:26px 16px">Nothing logged. Everything is working.</div>';
}

function refreshStatusPanel(status) {
  if (!status) return;
  const r = status.resolver || {};
  const c = status.counts || {};
  $('#status-body').innerHTML = `
    ${(status.mirrors || []).map(m => `
      <div class="status-row">
        <span><i class="dot ${m.healthy ? 'up' : 'down'}"></i>${esc(m.url.replace(/^https?:\/\//, ''))}</span>
        <span>${m.healthy ? 'online' : `retry in ${m.retry_in}s`}</span>
      </div>`).join('')}
    <div class="status-row"><span>Depots on mirror</span><span>${(c.depots || 0).toLocaleString()}</span></div>
    <div class="status-row"><span>Files indexed</span><span>${(c.files || 0).toLocaleString()}</span></div>
    <div class="status-row"><span>Named so far</span><span>${(r.named || 0).toLocaleString()} / ${(r.total || 0).toLocaleString()}</span></div>
    <div class="status-row"><span>Naming queue</span><span>${(r.queued || 0).toLocaleString()}</span></div>
    <div class="status-row"><span>Depots with a key</span><span>${(c.keyed_depots || 0).toLocaleString()}</span></div>
    <div class="status-row"><span>Reset depots</span><span>${(c.reset_depots || 0).toLocaleString()}</span></div>
    <div class="status-row"><span>Extractor</span><span>${status.extractor_ready ? 'ready' : 'downloads on first use'}</span></div>
    <div class="status-row"><span>Depot keys</span><span>${(status.counts.keyed_depots || 0).toLocaleString()} of ${(status.counts.depots || 0).toLocaleString()} depots</span></div>
    <div class="status-row"><span>Torrent fallback</span><span>${status.torrent ? 'available' : 'not found'}</span></div>`;
}

/* ----------------------------------------------------------------- detail */
async function openDetail(depot) {
  const modal = $('#modal');
  modal.hidden = false;
  $('#modal-body').innerHTML = '<div style="padding:40px;text-align:center"><span class="spinner"></span></div>';
  $('#modal-title').textContent = `Depot ${depot}`;
  $('#modal-badges').innerHTML = '';
  $('#modal-hero').style.backgroundImage = '';

  let d;
  try {
    d = await api(`/api/depot/${depot}`);
  } catch (e) {
    $('#modal-body').innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
    return;
  }
  state.detail = d;

  $('#modal-title').textContent = d.name;
  $('#modal-hero').style.backgroundImage = `url('${cardArt(d, 'hero') || cardArt(d, 'header') || ''}')`;
  $('#modal-badges').innerHTML = [
    d.year ? `<span class="badge plain">${esc(d.year)}</span>` : '',
    `<span class="badge plain">${d.versions} versions</span>`,
    `<span class="badge plain">Depot ${d.depot}</span>`,
    d.appid ? `<span class="badge plain">App ${d.appid}</span>` : '',
    d.reset ? '<span class="badge reset">Depot reset</span>' : '',
    d.has_key ? '<span class="badge key">Key available</span>' : '<span class="badge nokey">No key yet</span>',
    d.confidence === 'likely' ? '<span class="badge guess">Name is a best guess</span>' : '',
    heartButton(d, 'modal-heart'),
  ].filter(Boolean).join('');
  syncHearts();

  const latest = d.version_list[0];
  renderDetailBody(d, latest.version, latest.variants[0].crc);
}

function renderDetailBody(d, version, crc) {
  const entry = d.version_list.find(v => v.version === version) || d.version_list[0];
  const multi = entry.variants.length > 1;

  $('#modal-body').innerHTML = `
    <div class="kv">
      <div><span>Versions preserved</span><strong>${d.versions}</strong></div>
      <div><span>Newest version</span><strong>v${d.max_version}</strong></div>
      <div><span>First seen</span><strong>${shortDate(d.first_date)}</strong></div>
      <div><span>Last update</span><strong>${shortDate(d.last_date)}</strong></div>
      <div><span>Already cached</span><strong>${bytes(d.cached_bytes)}</strong></div>
      ${d.developer ? `<div><span>Developer</span><strong>${esc(d.developer)}</strong></div>` : ''}
      ${d.publisher ? `<div><span>Publisher</span><strong>${esc(d.publisher)}</strong></div>` : ''}
      ${d.franchise ? `<div><span>Series</span><strong>${esc(d.franchise)}</strong></div>` : ''}
      ${d.released ? `<div><span>Released</span><strong>${esc(d.released)}</strong></div>` : ''}
    </div>
    ${(d.genres || []).length ? `<div class="genre-tags">${
      d.genres.map(g => `<span class="genre-tag" data-genre-name="${esc(g)}">${esc(g)}</span>`)
        .join('')}</div>` : ''}

    ${(d.siblings || []).length > 1 ? `
      <div class="section-title">This game's other depots</div>
      <div class="note" style="background:rgba(255,255,255,.04);border-color:var(--line);color:var(--muted)">
        ${esc(d.name)} is spread across ${d.siblings.length} depots — content, language
        packs and tools were shipped separately. Pick the one you want.
      </div>
      <div class="sibling-list">
        ${d.siblings.map(sb => `
          <div class="sibling ${sb.current ? 'current' : ''}" data-sibling="${sb.depot}">
            <code>Depot ${sb.depot}</code>
            <span class="sib-meta">${sb.versions} version${sb.versions === 1 ? '' : 's'}
              · ${shortDate(sb.first_date)} → ${shortDate(sb.last_date)}</span>
            <span>${sb.reset ? '<span class="badge reset">Reset</span>' : ''}
              ${sb.has_key ? '<span class="badge key">Key</span>'
                           : '<span class="badge nokey">No key</span>'}</span>
          </div>`).join('')}
      </div>` : ''}

    ${!d.has_key ? `<div class="note">No decryption key for this depot is bundled with the
      extractor. SteamFlix will still try, falling back to <code>--key 0000…</code> and
      <code>--key 0</code>, which work on some depots. You can also paste a key below.</div>` : ''}

    ${d.reset ? `<div class="note">Valve reset this depot, so some version numbers exist more
      than once. Pick the variant you want — SteamFlix follows that blob's parent-CRC chain back
      to version 0 and downloads only the files on that branch.</div>` : ''}

    <div class="section-title">Choose a version</div>
    <div class="row-flex">
      <label class="field">Version
        <select class="version-select" id="pick-version">
          ${d.version_list.map(v => `
            <option value="${v.version}" ${v.version === version ? 'selected' : ''}>
              v${v.version} · ${shortDate(v.date)}${v.reset ? ` · ${v.variants.length} variants` : ''}
            </option>`).join('')}
        </select>
      </label>
      <label class="field">Only files matching (regex, optional)
        <input class="text-input" id="pick-filter" placeholder="e.g. \\.(mp4|avi|bik)$">
      </label>
      ${!d.has_key ? `<label class="field">Depot key override (optional)
        <input class="text-input" id="pick-key" placeholder="32 hex characters">
      </label>` : ''}
    </div>

    ${multi ? `<div class="section-title">Blob variants for v${version}</div>
      <div class="variant-list" id="variant-list">
        ${entry.variants.map(v => `
          <div class="variant ${v.crc === crc ? 'selected' : ''}" data-crc="${v.crc}">
            <code>${v.crc}</code>
            <span class="v-date">${shortDate(v.mtime)}</span>
          </div>`).join('')}
      </div>` : ''}

    <div class="section-title">Download plan</div>
    <div id="plan-box" class="note" style="background:rgba(255,255,255,.04);border-color:var(--line);color:var(--muted)">
      Steam2 stores versions as deltas, so getting v${version} means fetching every version
      from 0 to ${version}. Press <em>Check size</em> to measure it before committing.
    </div>
    <div class="row-flex" style="margin-top:14px">
      <button class="btn ghost" id="btn-size">Check size</button>
      <button class="btn ghost" id="btn-peek">Inspect contents</button>
      <button class="btn ghost" id="btn-key">Check encryption</button>
      <button class="btn accent" id="btn-download">Download &amp; extract</button>
      ${d.cached_bytes ? `<button class="ghost-btn" id="btn-drop">Delete cached blobs/dats (${bytes(d.cached_bytes)})</button>` : ''}
    </div>
    <div id="peek-box"></div>
    <div id="key-box"></div>

    <div class="link-row">
      <a href="${d.steamdb}" target="_blank" rel="noreferrer">SteamDB depot page</a>
      ${d.steamdb_app ? `<a href="${d.steamdb_app}" target="_blank" rel="noreferrer">SteamDB app page</a>` : ''}
      ${d.appid ? `<a href="https://store.steampowered.com/app/${d.appid}/" target="_blank" rel="noreferrer">Steam store</a>` : ''}
    </div>`;

  $$('[data-sibling]').forEach(el => el.addEventListener('click', () => {
    const depot = Number(el.dataset.sibling);
    if (depot !== d.depot) openDetail(depot);
  }));

  $$('.genre-tag').forEach(tag => tag.addEventListener('click', () => {
    const match = (state.facets && state.facets.genres || [])
      .find(g => g.name === tag.dataset.genreName);
    $('#modal').hidden = true;
    if (match) setFilter('genre', String(match.id));
    else setFilter('q', tag.dataset.genreName);
  }));

  $('#pick-version').addEventListener('change', ev => {
    const v = Number(ev.target.value);
    const ent = d.version_list.find(x => x.version === v);
    renderDetailBody(d, v, ent.variants[0].crc);
  });

  $$('#variant-list .variant').forEach(el => {
    el.addEventListener('click', () => renderDetailBody(d, version, el.dataset.crc));
  });

  const chosenCrc = d.reset ? crc : null;

  $('#btn-size').addEventListener('click', async ev => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Measuring';
    try {
      const plan = await api(`/api/depot/${d.depot}/plan`, {
        method: 'POST', body: { version, sizes: true },
      });
      const free = state.storage ? state.storage.usable : null;
      const tight = free !== null && plan.total_bytes > free;
      $('#plan-box').outerHTML = `
        <div id="plan-box" class="note ${tight ? 'bad' : 'good'}">
          <strong>${bytes(plan.total_bytes)}</strong> across ${plan.blob_count} blobs and
          ${plan.dat_count} dats (versions 0–${version}).
          ${tight ? `That does not fit — only ${bytes(free)} is usable right now.`
                  : `Fits comfortably; ${bytes(free)} usable.`}
        </div>`;
    } catch (e) {
      $('#plan-box').outerHTML = `<div id="plan-box" class="note bad">${esc(e.message)}</div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Check size';
    }
  });

  $('#btn-peek').addEventListener('click', async ev => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Reading blob';
    try {
      const info = await api(`/api/depot/${d.depot}/peek`, {
        method: 'POST', body: { version, crc: chosenCrc },
      });
      const m = info.manifest || {};
      $('#peek-box').innerHTML = `
        <div class="section-title">What is inside v${version}</div>
        <div class="kv">
          <div><span>Files</span><strong>${(m.file_count || 0).toLocaleString()}</strong></div>
          <div><span>Installed size</span><strong>${bytes(m.total_bytes)}</strong></div>
          <div><span>This version's dat</span><strong>${bytes(info.dat_size)}</strong></div>
          <div><span>Manifest app id</span><strong>${m.appid ?? '—'}</strong></div>
          ${info.prev_crc && info.prev_crc !== '00000000'
            ? `<div><span>Parent blob</span><strong>${info.prev_crc}</strong></div>` : ''}
        </div>
        ${(m.root_dirs || []).length
          ? `<div class="note good">Top-level folders: ${esc(m.root_dirs.join(', '))}</div>` : ''}`;
    } catch (e) {
      $('#peek-box').innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Inspect contents';
    }
  });

  $('#btn-key').addEventListener('click', async ev => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Reading blob';
    const qs = new URLSearchParams({ version, ...(chosenCrc ? { crc: chosenCrc } : {}) });
    try {
      const k = await api(`/api/depot/${d.depot}/key?${qs}`);
      renderKeyBox(d, version, chosenCrc, k);
    } catch (e) {
      $('#key-box').innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Check encryption';
    }
  });

  $('#btn-download').addEventListener('click', async ev => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Queueing';
    try {
      const job = await api('/api/jobs', {
        method: 'POST',
        body: {
          depot: d.depot,
          version,
          crc: chosenCrc,
          title: d.name,
          filter: ($('#pick-filter') || {}).value || null,
          key: ($('#pick-key') || {}).value || null,
          extract: true,
        },
      });
      toast(`Queued ${d.name}`, 'good', `v${version} — watch it in Downloads`);
      $('#modal').hidden = true;
      switchView('downloads');
      pollJobs();
    } catch (e) {
      toast('Could not start the download', 'bad', e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Download & extract';
    }
  });

  const drop = $('#btn-drop');
  if (drop) drop.addEventListener('click', async () => {
    await api(`/api/depot/${d.depot}/cache`, { method: 'DELETE' });
    toast('Cached blobs and dats removed', 'good');
    refreshStorage();
    openDetail(d.depot);
  });
}

/* -------------------------------------------------------------------- keys */
function renderKeyBox(d, version, crc, k) {
  const modes = Object.entries(k.modes || {})
    .map(([name, n]) => `${n} ${name}`).join(', ');

  // A key on record is only good news once it has actually decrypted something.
  // "rejected" is the interesting case: the table has wrong entries, and without
  // this the depot would claim to be ready and then fail to extract.
  const rejected = k.needs_key && k.known_key && k.key_verified === null && k.dat_ready;
  const haveKey = (k.known_key || k.bundled) && !rejected;
  const origin = {
    bundled: "the extractor's built-in table",
    trial: 'a key trial on this machine',
    user: 'a list you imported',
    keyfile: 'your key file',
  }[k.key_source] || 'the key store';

  let body;
  if (!k.needs_key) {
    body = `<div class="note good">
        <strong>No key needed.</strong> ${esc(k.verdict)}
        <div style="margin-top:6px">File modes: ${esc(modes)}.</div>
      </div>`;
  } else if (haveKey) {
    const proof = {
      exact: 'Verified against this depot\'s own encrypted data.',
      likely: 'Checked against file magic only — verify the extracted files look right.',
      weak: 'Only weakly checked — verify the extracted files look right.',
      unknown: 'Not verified yet: nothing encrypted is downloaded to test it against.',
    }[k.key_verified] || 'Not verified yet.';
    body = `<div class="note good">
        <strong>Encrypted, and we have a key.</strong>
        ${k.encrypted_files} of ${k.files} files are encrypted. Key from ${esc(origin)}
        ${k.known_key ? `(<code>${esc(k.known_key.slice(0, 8))}…</code>)` : ''}.
        <div style="margin-top:6px">${esc(proof)}</div>
      </div>`;
  } else {
    body = `${rejected ? `<div class="note bad">
        <strong>The key on record does not work.</strong> It came from ${esc(origin)}, but it
        does not decrypt this depot's data, so SteamFlix ignores it and searches for the
        real one instead — both here and during a download.</div>` : ''}
      <div class="note">
        <strong>Encrypted, and no working key is known yet.</strong>
        ${k.encrypted_files} of ${k.files} files need a real key (${esc(modes)}).
        The trial decrypts one small chunk with each of the
        ${(k.candidate_keys || 0).toLocaleString()} keys SteamFlix knows and keeps whichever
        one produces valid data. Widen it by importing keys from a backup —
        <code>keys.cpp</code> tables, <code>depot: key</code> lists, depot→key JSON
        and Steam's own <code>config.vdf</code> all parse.
      </div>
      ${k.dat_ready ? '' : `<div class="note">The trial needs real encrypted bytes, so it can
        only run once at least one version's dat is downloaded. Start the download and it
        runs automatically as part of the job.</div>`}
      <div class="row-flex" style="margin-top:10px">
        <button class="ghost-btn" id="btn-trial" ${k.dat_ready ? '' : 'disabled'}>
          Run key trial now</button>
        <button class="ghost-btn" id="btn-import">Import keys from a backup</button>
        <button class="ghost-btn" id="btn-sweep">Retry every cached depot</button>
      </div>
      <div id="key-import"></div>`;
  }

  $('#key-box').innerHTML = `<div class="section-title">Encryption</div>${body}`;

  const trial = $('#btn-trial');
  if (trial) trial.addEventListener('click', async ev => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Testing keys';
    try {
      const r = await api(`/api/depot/${d.depot}/key/trial`, {
        method: 'POST', body: { version, crc },
      });
      let html;
      if (r.key && r.confidence === 'exact') {
        html = `<div class="note good">Found a working key after ${r.tried} attempt(s):
          <code>${esc(r.key)}</code> (${esc(r.label || '')}). Saved, and the depot is now
          marked as keyed.</div>`;
      } else if (r.key) {
        html = `<div class="note">Found a likely key after ${r.tried} attempt(s):
          <code>${esc(r.key)}</code>. Only file magic backed it up, not an exact
          decompression, so check the extracted files look right.</div>`;
      } else if (!r.needs_key) {
        html = `<div class="note good">${esc(r.reason)}</div>`;
      } else {
        html = `<div class="note bad">${esc(r.reason)}</div>`;
      }
      $('#key-box').insertAdjacentHTML('beforeend', html);
    } catch (e) {
      $('#key-box').insertAdjacentHTML('beforeend', `<div class="note bad">${esc(e.message)}</div>`);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Run key trial now';
    }
  });

  const sweep = $('#btn-sweep');
  if (sweep) sweep.addEventListener('click', async ev => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Sweeping';
    try {
      const r = await api('/api/keys/sweep', { method: 'POST' });
      toast(`Checked ${r.checked} cached depot(s)`, r.recovered ? 'good' : '',
            r.recovered ? `${r.recovered} key(s) recovered` : 'no new keys this time');
    } catch (e) {
      toast('Sweep failed', 'bad', e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Retry every cached depot';
    }
  });

  const imp = $('#btn-import');
  if (imp) imp.addEventListener('click', () => {
    $('#key-import').innerHTML = `
      <div class="section-title">Paste keys from your backup</div>
      <textarea class="text-input" id="key-text" rows="5" style="width:100%;min-width:0"
        placeholder="One per line. Any of these shapes work:&#10;441 a1d20b...&#10;a1d20bcd94ce6105d0e2256fe06b2b22&#10;{ 441, { 0xa1,0x0d,... } }"></textarea>
      <div class="row-flex" style="margin-top:8px">
        <input class="text-input" id="key-path" placeholder="…or a path to a key file"
               style="flex:1">
        <button class="btn ghost" id="key-save">Import</button>
      </div>`;
    $('#key-save').addEventListener('click', async ev => {
      const btn = ev.currentTarget;
      btn.disabled = true;
      try {
        const r = await api('/api/keys/import', {
          method: 'POST',
          body: { text: $('#key-text').value, path: $('#key-path').value || null },
        });
        toast(`Imported ${r.imported} key(s)`, 'good', `${r.total_keys} keys available`);
        $('#key-import').innerHTML = '';
      } catch (e) {
        toast('Import failed', 'bad', e.message);
      } finally {
        btn.disabled = false;
      }
    });
  });
}

/* ----------------------------------------------------------------- player */
async function openPlayer(item) {
  const modal = $('#player');
  modal.hidden = false;
  $('#player-stage').innerHTML = '<div class="stage-empty"><span class="spinner"></span></div>';
  $('#player-body').innerHTML = '';

  let content;
  try {
    content = await api(`/api/content/${encodeURIComponent(item.folder)}`);
  } catch (e) {
    $('#player-stage').innerHTML = `<div class="stage-empty">${esc(e.message)}</div>`;
    return;
  }
  state.content = content;

  const c = content.counts;
  const summary = [
    c.run ? `${c.run} executable${c.run === 1 ? '' : 's'}` : '',
    c.video ? `${c.video} video${c.video === 1 ? '' : 's'}` : '',
    c.audio ? `${c.audio} audio` : '',
    c.image ? `${c.image} images` : '',
  ].filter(Boolean).join(' · ');

  $('#player-body').innerHTML = `
    <div class="job-head" style="margin-bottom:14px">
      <strong>${esc(item.name)}</strong>
      <span class="job-state done">v${item.version}</span>
      <span class="job-meta">${content.file_count} files · ${bytes(content.total_bytes)}${summary ? ' · ' + summary : ''}</span>
      <div class="job-actions">
        ${content.launcher
          ? `<button class="btn accent" style="padding:8px 16px;font-size:13px" id="btn-launch">
               Play ${esc(content.launcher.name)}</button>` : ''}
        <button class="ghost-btn" id="btn-verify">Verify files</button>
        <button class="ghost-btn" id="btn-openfolder">Open folder</button>
        <button class="ghost-btn" id="btn-delete">Delete</button>
      </div>
    </div>
    <div id="verify-box"></div>
    ${content.mode === 'browse' && !content.launcher
      ? `<div class="note">No executable and no video in this depot — it is data only
         (${esc(Object.entries(c).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(', '))}).
         You can still open any file below.</div>` : ''}
    <div class="section-title">Files</div>
    <div class="file-list" id="file-list">
      ${content.entries.map((f, i) => `
        <div class="file-row" data-i="${i}">
          <span class="file-kind">${f.kind}</span>
          <span class="file-name">${esc(f.name)}</span>
          <span class="file-path">${esc(f.dir)}</span>
          <span class="file-size">${bytes(f.size)}</span>
        </div>`).join('')}
    </div>
    ${content.truncated ? '<div class="log-time" style="margin-top:8px">Showing the first 4000 files.</div>' : ''}`;

  const featured = content.featured_video;
  if (featured) playFile(featured);
  else $('#player-stage').innerHTML =
    `<div class="stage-empty">${content.launcher
      ? 'Ready to launch. Press Play above, or pick a file to preview.'
      : 'Pick a file below to preview it.'}</div>`;

  $$('#file-list .file-row').forEach(row => {
    row.addEventListener('click', () => {
      $$('#file-list .file-row').forEach(r => r.classList.remove('active'));
      row.classList.add('active');
      playFile(content.entries[Number(row.dataset.i)]);
    });
  });

  const launchBtn = $('#btn-launch');
  if (launchBtn) launchBtn.addEventListener('click', async ev => {
    const btn = ev.currentTarget;
    const label = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Starting';
    try {
      await runFile(content.launcher.rel, content.launcher.name);
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  });

  // The extractor exits zero after writing whatever it managed, so "it looked
  // fine" is not the same as "the game is all there". This asks the manifest.
  $('#btn-verify').addEventListener('click', async ev => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Checking';
    try {
      const r = await api(`/api/installed/${encodeURIComponent(item.folder)}/verify`,
                          { method: 'POST' });
      const kind = r.complete === true ? 'good' : r.complete === false ? 'bad' : '';
      const list = [...(r.missing || []).map(m => m.file),
                    ...(r.short || []).map(m => `${m.file} (truncated)`)].slice(0, 12);
      $('#verify-box').innerHTML = `
        <div class="note ${kind}">
          <strong>${esc(r.verdict)}</strong>
          <div style="margin-top:6px">
            ${r.files_on_disk} file(s) on disk${
              r.files_expected ? ` of ${r.files_expected} in the manifest` : ''}.
          </div>
          ${list.length ? `<div class="sub" style="margin-top:8px">${
             list.map(f => esc(f)).join('<br>')}${
             (r.missing_count || 0) + (r.short_count || 0) > list.length
               ? `<br>…and ${(r.missing_count + r.short_count) - list.length} more` : ''}</div>` : ''}
        </div>`;
    } catch (e) {
      $('#verify-box').innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Verify files';
    }
  });

  $('#btn-openfolder').addEventListener('click', () =>
    api('/api/open', { method: 'POST', body: { path: item.path } }).catch(e => toast(e.message, 'bad')));

  $('#btn-delete').addEventListener('click', async () => {
    if (!confirm(`Delete the extracted files for ${item.name}? The blob/dat cache stays.`)) return;
    await api(`/api/installed/${encodeURIComponent(item.folder)}`, { method: 'DELETE' });
    modal.hidden = true;
    toast('Deleted', 'good');
    renderLibrary();
  });
}

function playFile(f) {
  const stage = $('#player-stage');
  const url = `/api/file/${f.rel.split('/').map(encodeURIComponent).join('/')}`;

  if (f.kind === 'video' && f.web) {
    stage.innerHTML = `<video controls autoplay src="${url}"></video>`;
  } else if (f.kind === 'audio' && f.web) {
    stage.innerHTML = `<audio controls autoplay src="${url}"></audio>`;
  } else if (f.kind === 'image') {
    stage.innerHTML = `<img src="${url}" alt="${esc(f.name)}">`;
  } else if (f.kind === 'text') {
    stage.innerHTML = '<pre>Loading…</pre>';
    fetch(url).then(r => r.text()).then(t => {
      stage.innerHTML = `<pre>${esc(t.slice(0, 120000))}</pre>`;
    });
  } else {
    const label = f.kind === 'video'
      ? 'This video format does not play in a browser.'
      : f.kind === 'run' ? 'Executable.' : 'No in-browser preview for this file type.';
    stage.innerHTML = `<div class="stage-empty">
        <div>${esc(label)}</div>
        <button class="btn ghost" style="margin-top:14px" id="stage-open">
          ${f.kind === 'run' ? 'Run it' : 'Open with Windows'}</button>
      </div>`;
    $('#stage-open').addEventListener('click', async ev => {
      const btn = ev.currentTarget;
      const label = btn.textContent;
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span> Starting';
      try {
        await runFile(f.rel, f.name, f.kind !== 'run');
      } finally {
        btn.disabled = false;
        btn.textContent = label;
      }
    });
  }
}

/* ---------------------------------------------------------------- launching */
async function runFile(rel, name, shell = false) {
  let r;
  try {
    r = await api('/api/launch', { method: 'POST', body: { rel, shell } });
  } catch (e) {
    toast(`Could not start ${name}`, 'bad', e.message);
    return false;
  }
  if (r.ok === false) {
    // Started and died immediately: almost always a missing runtime on a
    // modern machine, and the server's message names the exit code.
    toast(`${name} would not run`, 'bad', r.error);
    return false;
  }
  toast(`Launched ${name}`, 'good', r.note || (r.pid ? `pid ${r.pid}` : ''));
  return true;
}

/* --------------------------------------------------------------- first run */
// Asked once, before anything is fetched: how much should the first screen
// pull? Answering writes the same two options the Status panel exposes, so
// there is no separate "first run mode" to keep in sync afterwards.
function askFirstRun(total) {
  return new Promise(resolve => {
    const panel = $('#welcome');
    $('#welcome-count').textContent = (total || 0).toLocaleString();
    panel.hidden = false;
    $$('#welcome .pick').forEach(btn => btn.addEventListener('click', () => {
      const full = btn.dataset.pick === 'full';
      state.opts.light = !full;
      state.opts.lazyArt = !full;
      state.opts.greeted = true;
      saveOptions();
      panel.hidden = true;
      resolve(full);
    }, { once: true }));
  });
}

/* ---------------------------------------------------------------- shutdown */
async function shutdownServer(closeTab) {
  const box = $('#power-result');
  box.innerHTML = '<div class="note">Stopping…</div>';
  try {
    const r = await api('/api/shutdown', { method: 'POST', body: { cancel_jobs: true } });
    Object.values(state.timers).forEach(t => clearInterval(t));
    box.innerHTML = `<div class="note good">Server stopped${
      r.cancelled_jobs ? ` — ${r.cancelled_jobs} download(s) cancelled` : ''}.
      You can close this tab.</div>`;
    if (closeTab) {
      // Only works for a tab the app itself opened; the message above covers
      // the case where the browser refuses.
      setTimeout(() => { window.close(); }, 600);
    }
  } catch (e) {
    // A server that dies mid-response is a success, not a failure.
    Object.values(state.timers).forEach(t => clearInterval(t));
    box.innerHTML = '<div class="note good">Server stopped. You can close this tab.</div>';
    if (closeTab) setTimeout(() => { window.close(); }, 600);
  }
}

/* ----------------------------------------------------------------- torrent */
async function refreshTorrentPanel() {
  const box = $('#torrent-body');
  if (!box) return;
  let t;
  try { t = await api('/api/torrent'); } catch (e) { return; }
  state.torrent = t;

  if (!t.available) {
    box.innerHTML = `<div class="note">No <code>steam2.torrent</code> next to SteamFlix,
      so there is no fallback if the mirrors go down.
      ${t.error ? `<div style="margin-top:6px">${esc(t.error)}</div>` : ''}</div>`;
    return;
  }
  box.innerHTML = `
    <div class="status-row"><span>Torrent</span><span>${esc(t.name)}</span></div>
    <div class="status-row"><span>Files</span><span>${t.files.toLocaleString()}</span></div>
    <div class="status-row"><span>Indexed</span>
      <span>${t.indexed ? t.indexed.toLocaleString() : 'not yet'}</span></div>
    <div class="status-row"><span>Trackers</span><span>${t.trackers}</span></div>
    <div class="status-row"><span>Used when</span>
      <span>${t.enabled ? 'every mirror fails' : 'never (disabled)'}</span></div>
    ${seedRows(t.seed)}
    <div class="row-flex" style="margin-top:10px">
      <button class="ghost-btn" id="btn-swarm">Check the swarm</button>
    </div>
    <div id="swarm-result"></div>`;

  $('#btn-swarm').addEventListener('click', async ev => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Asking trackers';
    try {
      const r = await api('/api/torrent/peers', { method: 'POST' });
      $('#swarm-result').innerHTML = r.peers
        ? `<div class="note good">${r.peers} peer(s) sharing the archive right now.</div>`
        : '<div class="note bad">No peers answered. The mirrors are the only route today.</div>';
    } catch (e) {
      $('#swarm-result').innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Check the swarm';
    }
  });
}

// What SteamFlix is giving back, in the same panel that says what it takes.
function seedRows(s) {
  if (!s) return '';
  if (!s.enabled) {
    return `<div class="status-row"><span>Seeding</span><span>off</span></div>`;
  }
  if (!s.running) {
    return `<div class="status-row"><span>Seeding</span>
      <span>${esc(s.reason || 'not running')}</span></div>`;
  }
  const verifying = s.state === 'verifying' && s.to_check
    ? ` (checking ${s.checked.toLocaleString()}/${s.to_check.toLocaleString()})` : '';
  // Two different things are worth knowing: how much we can serve, and whether
  // anyone outside can actually ask for it.
  const reach = s.reachable
    ? `port ${s.port}, forwarded`
    : `port ${s.port}, not forwarded`;
  return `
    <div class="status-row"><span>Seeding</span>
      <span>${bytes(s.bytes)} in ${s.pieces.toLocaleString()} piece(s)${verifying}</span></div>
    <div class="status-row"><span>Uploaded</span>
      <span>${bytes(s.uploaded_total)}${s.ratio !== null && s.ratio !== undefined
        ? ` (ratio ${s.ratio})` : ''}</span></div>
    <div class="status-row"><span>Peers</span>
      <span>${s.peers.length} connected, ${s.peers_served} since start</span></div>
    <div class="status-row"><span>Reachable</span><span>${reach}</span></div>`;
}

/* --------------------------------------------------------------- settings */
// Everything on the Settings screen writes straight through to the server, so
// a change takes effect on the next request rather than the next restart.
let settingsSaveTimer = null;

async function loadSettings() {
  let data;
  try { data = await api('/api/settings'); } catch (e) { return; }
  state.settings = data.settings;
  state.mirrorStatus = data.mirror_status;
  paintSettings();
}

function paintSettings() {
  const c = state.settings;
  if (!c) return;

  $$('#source-mode .seg-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.source === c.source_mode));
  $('#set-torrent-busy').checked = !!c.torrent_when_busy;
  $('#set-busy-threshold').value = c.torrent_busy_threshold;

  $('#set-seed-enabled').checked = !!c.seed_enabled;
  $('#set-seed-portmap').checked = !!c.seed_portmap;
  $('#set-seed-port').value = c.seed_port;
  $('#set-seed-kbps').value = c.seed_up_kbps;
  $('#set-seed-peers').value = c.seed_max_peers;
  $('#set-seed-slots').value = c.seed_slots;
  paintSeedLive();

  $('#set-delay').value = c.request_delay_ms;
  $('#set-rpm').value = c.max_requests_per_minute;
  $('#set-threads').value = c.download_threads;
  $('#set-segments').value = c.segments_per_file;
  $('#set-resolve-delay').value = c.resolve_delay_ms;
  $('#set-resolve-threads').value = c.resolve_threads;

  $('#set-proxy-enabled').checked = !!c.proxy_enabled;
  $('#set-proxy-mode').value = c.proxy_mode;
  $('#set-proxy-source').value = c.proxy_source || '';
  if (document.activeElement !== $('#set-proxy-list')) {
    $('#set-proxy-list').value = (c.proxy_list || []).join('\n');
  }

  paintMirrors();
  paintTrafficNote();
}

// The seeding panel is the one place that can honestly answer "is this
// actually working?", so it says what is being served and who can reach it.
async function paintSeedLive() {
  const box = $('#seed-live');
  if (!box) return;
  let s;
  try { s = await api('/api/torrent/seed'); } catch (e) { return; }
  state.seed = s;
  if (!s.available) {
    box.innerHTML = `<div class="note">There is no <code>steam2.torrent</code>
      beside SteamFlix, so there is nothing to seed into.</div>`;
    return;
  }
  if (!s.enabled || !s.running) {
    box.innerHTML = `<div class="note">Not seeding right now${
      s.reason ? ` — ${esc(s.reason)}` : ''}.</div>`;
    return;
  }
  const verifying = s.state === 'verifying' && s.to_check
    ? `<div class="note">Checking which pieces are complete:
       ${s.checked.toLocaleString()} of ${s.to_check.toLocaleString()}.</div>` : '';
  // A piece is 16 MiB, so a library of small blobs can legitimately have
  // nothing whole to share yet. Say so rather than looking broken.
  const nothing = !s.pieces ? `<div class="note">Nothing complete to share yet.
      A shareable piece is 16 MiB of the torrent and has to be whole, which
      usually means a downloaded game rather than a single blob.</div>` : '';
  const reach = s.reachable
    ? `<div class="note good">Port ${s.port} is forwarded by
       ${esc((s.mapping.method || '').toUpperCase())}${s.mapping.external_ip
         ? ` (${esc(s.mapping.external_ip)})` : ''}, so other peers can connect in.</div>`
    : `<div class="note">Port ${s.port} is not forwarded, so SteamFlix can only
       upload to peers it connects to itself. Forward TCP ${s.port} to this
       machine, or turn UPnP on at the router, to accept incoming peers too.</div>`;
  box.innerHTML = `
    <div class="status-row"><span>Sharing</span>
      <span>${bytes(s.bytes)} in ${s.pieces.toLocaleString()} verified piece(s)</span></div>
    <div class="status-row"><span>From</span><span>${s.files} local file(s)</span></div>
    <div class="status-row"><span>Uploaded</span>
      <span>${bytes(s.uploaded_total)}${s.ratio !== null && s.ratio !== undefined
        ? ` (ratio ${s.ratio})` : ''}</span></div>
    <div class="status-row"><span>Peers</span>
      <span>${s.peers.length} now, ${s.peers_served} since start</span></div>
    <div class="status-row"><span>Trackers</span>
      <span>${s.trackers_ok} answering, ${s.swarm_peers} peer(s) in the swarm</span></div>
    ${verifying}${nothing}${reach}`;
}

function paintMirrors() {
  const c = state.settings;
  const status = {};
  (state.mirrorStatus || []).forEach(m => { status[m.url] = m; });

  $('#mirror-list').innerHTML = (c.mirrors || []).map((url, i) => {
    const st = status[url];
    const label = !st ? 'not checked'
      : st.healthy ? 'healthy'
      : `benched, retrying in ${st.retry_in}s`;
    return `<div class="mirror-row" data-mirror="${esc(url)}">
      <span class="idx">${i + 1}</span>
      <code>${esc(url)}</code>
      <span class="state ${st ? (st.healthy ? 'up' : 'down') : ''}">${label}</span>
      <span class="acts">
        <button data-move="up" title="Try this one earlier" ${i === 0 ? 'disabled' : ''}>&uarr;</button>
        <button data-move="down" title="Try this one later"
                ${i === c.mirrors.length - 1 ? 'disabled' : ''}>&darr;</button>
        <button data-remove="1" title="Remove"
                ${c.mirrors.length < 2 ? 'disabled' : ''}>&times;</button>
      </span>
    </div>`;
  }).join('');
}

// The two numbers that decide how hard SteamFlix leans on a mirror multiply
// together, so the panel says what the combination actually means.
function paintTrafficNote() {
  const c = state.settings;
  const conns = c.download_threads * c.segments_per_file;
  const perSec = c.request_delay_ms ? Math.round(1000 / c.request_delay_ms) : '∞';
  const heavy = conns > 32 || c.request_delay_ms < 50;
  const box = $('#traffic-note');
  box.className = 'note' + (heavy ? ' bad' : ' good');
  box.innerHTML = heavy
    ? `<strong>That is a lot to ask of a volunteer mirror.</strong>
       Up to ${conns} simultaneous connections and about ${perSec} requests a second.
       Consider fewer files at once, or a longer pause.`
    : `Up to <strong>${conns}</strong> simultaneous connections and about
       <strong>${perSec}</strong> requests a second, capped at
       ${c.max_requests_per_minute} a minute.`;
}

function settingsFromForm() {
  return {
    torrent_when_busy: $('#set-torrent-busy').checked,
    torrent_busy_threshold: Number($('#set-busy-threshold').value),
    seed_enabled: $('#set-seed-enabled').checked,
    seed_portmap: $('#set-seed-portmap').checked,
    seed_port: Number($('#set-seed-port').value),
    seed_up_kbps: Number($('#set-seed-kbps').value),
    seed_max_peers: Number($('#set-seed-peers').value),
    seed_slots: Number($('#set-seed-slots').value),
    request_delay_ms: Number($('#set-delay').value),
    max_requests_per_minute: Number($('#set-rpm').value),
    download_threads: Number($('#set-threads').value),
    segments_per_file: Number($('#set-segments').value),
    resolve_delay_ms: Number($('#set-resolve-delay').value),
    resolve_threads: Number($('#set-resolve-threads').value),
    proxy_enabled: $('#set-proxy-enabled').checked,
    proxy_mode: $('#set-proxy-mode').value,
    proxy_source: $('#set-proxy-source').value.trim(),
    proxy_list: $('#set-proxy-list').value,
  };
}

async function saveSettings(patch, quiet = false) {
  try {
    const r = await api('/api/settings', {
      method: 'PUT', body: patch || settingsFromForm(),
    });
    state.settings = r.settings;
    state.mirrorStatus = r.mirror_status;
    paintSettings();
    if (!quiet) {
      $('#settings-saved').textContent = 'Saved';
      setTimeout(() => { $('#settings-saved').textContent = ''; }, 1600);
    }
  } catch (e) {
    toast('Could not save settings', 'bad', e.message);
  }
}

function queueSettingsSave() {
  clearTimeout(settingsSaveTimer);
  settingsSaveTimer = setTimeout(() => saveSettings(), 500);
}

/* ------------------------------------------------------------ diagnostics */
function diagRow(check) {
  const extra = check.extra || {};
  const mirrors = (extra.mirrors || []).map(m =>
    `${m.mirror} — ${m.state}${m.ms ? ` (${m.ms} ms)` : ''}`).join('<br>');
  return `<div class="diag ${check.state}">
    <span class="dot2"></span>
    <strong>${esc(check.check)}</strong>
    <div>
      <div class="detail">${esc(check.detail)}</div>
      ${check.fix ? `<div class="fix">${esc(check.fix)}</div>` : ''}
      ${mirrors ? `<div class="sub">${mirrors}</div>` : ''}
    </div>
  </div>`;
}

async function runDiagnostics(deep) {
  const box = $('#diag-result');
  box.innerHTML = `<div class="note"><span class="spinner"></span>
    ${deep ? 'Checking mirrors and the swarm…' : 'Checking…'}</div>`;
  let d;
  try {
    d = await api(`/api/diagnostics?deep=${deep ? 1 : 0}`);
  } catch (e) {
    box.innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
    return;
  }
  state.diagnostics = d;
  const word = { ok: 'Everything checks out', warning: 'Working, with notes',
                 bad: 'Something needs attention' }[d.state];
  box.innerHTML = `
    <div class="diag-head">
      <span class="big ${d.state}">${esc(word)}</span>
      <span class="result-count">${d.summary.ok} ok · ${d.summary.warning} notes
        · ${d.summary.bad} problems</span>
    </div>
    <div class="diag-list">${d.checks.map(diagRow).join('')}</div>`;
}

/* ------------------------------------------------------------------ views */
function switchView(view, keepScroll = false) {
  state.view = view;
  $$('.view').forEach(v => { v.hidden = v.id !== `view-${view}`; });
  $$('.nav-link').forEach(a => a.classList.toggle('active', a.dataset.view === view));
  if (!keepScroll) window.scrollTo({ top: 0, behavior: 'smooth' });

  if (view === 'browse' && !$('#browse-grid').children.length) loadBrowse(true);
  if (view === 'browse' && !state.facets) refreshFacets();
  if (view === 'library') renderLibrary();
  if (view === 'settings') loadSettings();
  saveSession();
}

/* ------------------------------------------------------------------ wiring */
function wire() {
  $$('[data-view]').forEach(el => el.addEventListener('click', ev => {
    ev.preventDefault();
    switchView(el.dataset.view);
  }));

  let searchTimer;
  $('#search').addEventListener('input', ev => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.browse.q = ev.target.value.trim();
      if (state.view !== 'browse') switchView('browse');
      loadBrowse(true);
    }, 260);
  });

  $('#scope').addEventListener('change', ev => {
    state.browse.scope = ev.target.value;
    loadBrowse(true);
  });
  $('#sort').addEventListener('change', ev => {
    state.browse.sort = ev.target.value;
    loadBrowse(true);
  });
  ['genre', 'developer', 'publisher', 'year'].forEach(key => {
    $(`#${key}`).addEventListener('change', ev => setFilter(key, ev.target.value));
  });

  // The heart works the same wherever it is drawn: cards, the hero, the dialog.
  document.addEventListener('click', ev => {
    const btn = ev.target.closest('[data-fav]');
    if (!btn) return;
    ev.stopPropagation();
    ev.preventDefault();
    toggleFavourite(Number(btn.dataset.fav));
  });

  $('#hero-dots').addEventListener('click', ev => {
    const dot = ev.target.closest('[data-hero]');
    if (dot) showHero(Number(dot.dataset.hero));
  });

  $('#arrange').addEventListener('click', async ev => {
    const btn = ev.target.closest('[data-arrange]');
    if (!btn || btn.dataset.arrange === state.home.arrange) return;
    $$('#arrange .seg-btn').forEach(b => b.classList.toggle('active', b === btn));
    state.home.arrange = btn.dataset.arrange;
    saveSession();
    try {
      await loadHome();
    } catch (e) {
      toast('Could not rearrange the home screen', 'bad', e.message);
    }
  });
  $('#browse-chips').addEventListener('click', ev => {
    const btn = ev.target.closest('[data-drop]');
    if (!btn) return;
    if (btn.dataset.drop === 'all') clearFilters();
    else setFilter(btn.dataset.drop, '');
  });
  $('#load-more').addEventListener('click', () => loadBrowse(false));
  $('#more-shelves').addEventListener('click', loadMoreShelves);

  // Loading options. Turning light start off reloads the home screen at full
  // width straight away, so the effect is visible rather than theoretical.
  const optLight = $('#opt-light');
  const optLazy = $('#opt-lazyart');
  optLight.checked = state.opts.light;
  optLazy.checked = state.opts.lazyArt;
  optLight.addEventListener('change', ev => {
    state.opts.light = ev.target.checked;
    saveOptions();
    state.browse.limit = PAGE.browseLimit;
    loadHome().catch(e => toast('Could not reload the home screen', 'bad', e.message));
  });
  optLazy.addEventListener('change', ev => {
    state.opts.lazyArt = ev.target.checked;
    saveOptions();
    if (!state.opts.lazyArt) $$('.card-art.pending').forEach(applyArt);
  });

  window.addEventListener('scroll', () => {
    // Light start means "fetch on request", so auto-paging is off and the
    // Load more button is the only thing that pulls the next page.
    if (state.opts.light) return;
    if (state.view !== 'browse' || state.browse.loading) return;
    if (state.browse.offset >= state.browse.total) return;
    if (window.innerHeight + window.scrollY > document.body.offsetHeight - 700) loadBrowse(false);
  });

  const toggle = (btn, panel) => {
    $(btn).addEventListener('click', ev => {
      ev.stopPropagation();
      const p = $(panel);
      const wasHidden = p.hidden;
      $$('.dropdown-panel').forEach(x => { x.hidden = true; });
      p.hidden = !wasHidden;
      if (!p.hidden && panel === '#logs-panel') pollLogs();
      if (!p.hidden && panel === '#status-panel') refreshTorrentPanel();
    });
  };
  toggle('#logs-btn', '#logs-panel');
  toggle('#status-btn', '#status-panel');
  toggle('#power-btn', '#power-panel');

  $('#btn-shutdown').addEventListener('click', () => shutdownServer(false));
  $('#btn-shutdown-exit').addEventListener('click', () => shutdownServer(true));

  $('#nav-toggle').addEventListener('click', ev => {
    ev.stopPropagation();
    const nav = $('#nav');
    nav.classList.toggle('open');
    ev.currentTarget.setAttribute('aria-expanded', String(nav.classList.contains('open')));
  });
  // Picking a destination on a phone should put the menu away again.
  $('#nav').addEventListener('click', () => $('#nav').classList.remove('open'));

  /* ---------------------------------------------------------- settings */
  $('#source-mode').addEventListener('click', ev => {
    const btn = ev.target.closest('[data-source]');
    if (!btn) return;
    saveSettings({ source_mode: btn.dataset.source });
  });

  ['set-torrent-busy', 'set-busy-threshold', 'set-delay', 'set-rpm', 'set-threads',
   'set-segments', 'set-resolve-delay', 'set-resolve-threads', 'set-proxy-enabled',
   'set-proxy-mode', 'set-proxy-source', 'set-proxy-list', 'set-seed-enabled',
   'set-seed-portmap', 'set-seed-port', 'set-seed-kbps', 'set-seed-peers',
   'set-seed-slots'].forEach(id => {
    const el = $(`#${id}`);
    if (!el) return;
    el.addEventListener('change', () => saveSettings());
    if (el.type === 'number' || el.tagName === 'TEXTAREA' || el.type === 'text') {
      el.addEventListener('input', () => {
        // Reflect the traffic maths as it is typed, but only write once the
        // typing stops.
        if (el.type === 'number') {
          Object.assign(state.settings, settingsFromForm());
          paintTrafficNote();
        }
        queueSettingsSave();
      });
    }
  });

  $('#btn-seed-verify').addEventListener('click', async ev => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Checking';
    try {
      await api('/api/torrent/seed/verify', { method: 'POST' });
      // The scan runs in the background, so look again once it has had a
      // moment rather than reporting the state it was in before the click.
      setTimeout(paintSeedLive, 1200);
    } catch (e) {
      toast('Could not re-check the library', 'bad', e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Re-check the library';
    }
  });

  $('#settings-reset').addEventListener('click', async () => {
    try {
      const r = await api('/api/settings/reset', { method: 'POST' });
      state.settings = r.settings;
      paintSettings();
      toast('Settings back to defaults', 'good');
    } catch (e) { toast('Could not reset', 'bad', e.message); }
  });

  $('#mirror-list').addEventListener('click', ev => {
    const row = ev.target.closest('[data-mirror]');
    const btn = ev.target.closest('button');
    if (!row || !btn) return;
    const list = [...state.settings.mirrors];
    const i = list.indexOf(row.dataset.mirror);
    if (i < 0) return;
    if (btn.dataset.remove) {
      if (list.length < 2) return;
      list.splice(i, 1);
    } else {
      const to = btn.dataset.move === 'up' ? i - 1 : i + 1;
      if (to < 0 || to >= list.length) return;
      [list[i], list[to]] = [list[to], list[i]];
    }
    saveSettings({ mirrors: list });
  });

  async function testMirror(url) {
    const box = $('#mirror-result');
    box.innerHTML = '<div class="note"><span class="spinner"></span> Testing…</div>';
    try {
      const r = await api('/api/mirrors/test', { method: 'POST', body: { url } });
      box.innerHTML = r.ok
        ? `<div class="note good">${esc(r.url)} answered in ${r.ms} ms
             ${r.ranges ? 'and supports range requests' : 'but ignored a range request'}.</div>`
        : `<div class="note bad">${esc(r.url)} — ${esc(r.error || r.note ||
             ('HTTP ' + r.status))}</div>`;
      return r;
    } catch (e) {
      box.innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
      return null;
    }
  }

  $('#mirror-test').addEventListener('click', () => {
    const url = $('#mirror-new').value.trim();
    if (url) testMirror(url);
  });

  $('#mirror-add').addEventListener('click', async () => {
    const url = $('#mirror-new').value.trim();
    if (!url) return;
    // A dead mirror in the list slows every download until it gets benched, so
    // it is tested before it goes in - but the user can still insist.
    const r = await testMirror(url);
    const clean = (r && r.url) || url;
    if (r && !r.ok && !confirm(`${clean} did not answer like a Steam2 mirror.\nAdd it anyway?`)) {
      return;
    }
    const list = [...state.settings.mirrors];
    if (!list.includes(clean)) list.push(clean);
    await saveSettings({ mirrors: list });
    $('#mirror-new').value = '';
  });

  async function refreshProxies(usePublic) {
    const box = $('#proxy-result');
    box.innerHTML = `<div class="note"><span class="spinner"></span>
      Checking proxies — this takes a moment…</div>`;
    try {
      const r = await api('/api/proxies/refresh', {
        method: 'POST', body: { public: !!usePublic },
      });
      box.innerHTML = r.working
        ? `<div class="note good">${r.working} working of ${r.checked} checked${
             r.fetched ? ` (${r.fetched} pulled from the public list)` : ''}.</div>`
        : `<div class="note bad">None of the ${r.checked} proxies answered.
             ${usePublic ? 'Public lists go stale fast; try again later.' :
                           'Check the addresses, or top up from the public list.'}</div>`;
      $('#proxy-status').textContent = `${r.working} in the pool`;
    } catch (e) {
      box.innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
    }
  }

  $('#proxy-check').addEventListener('click', () => refreshProxies(false));
  $('#proxy-public').addEventListener('click', () => refreshProxies(true));

  $('#diag-run').addEventListener('click', () => runDiagnostics(true));
  $('#diag-quick').addEventListener('click', () => runDiagnostics(false));

  $('#btn-allart').addEventListener('click', ev => {
    const n = loadAllArt();
    toast(n ? `Fetching ${n} cover${n === 1 ? '' : 's'}` : 'Every cover on screen is loaded',
          'good');
    ev.currentTarget.blur();
  });
  document.addEventListener('click', ev => {
    if (!ev.target.closest('.dropdown')) $$('.dropdown-panel').forEach(p => { p.hidden = true; });
  });

  $('#log-category').addEventListener('change', pollLogs);
  $('#log-level').addEventListener('change', pollLogs);
  $('#log-clear').addEventListener('click', async () => {
    await api('/api/logs/clear', { method: 'POST' });
    pollLogs();
  });

  $('#storage-btn').addEventListener('click', () => switchView('library'));
  $('#open-library-folder').addEventListener('click', () =>
    api('/api/open', { method: 'POST', body: { path: state.storage.path } })
      .catch(e => toast(e.message, 'bad')));

  $('#clear-jobs').addEventListener('click', async () => {
    await api('/api/jobs/clear', { method: 'POST' });
    $('#job-list').innerHTML = '';
    pollJobs();
  });

  document.addEventListener('click', ev => {
    const cancel = ev.target.closest('[data-cancel]');
    if (cancel) api(`/api/jobs/${cancel.dataset.cancel}/cancel`, { method: 'POST' }).then(pollJobs);
    const log = ev.target.closest('[data-log]');
    if (log) showJobLog(log.dataset.log);
    const open = ev.target.closest('[data-open]');
    if (open) api('/api/open', { method: 'POST', body: { path: open.dataset.open } })
      .catch(e => toast(e.message, 'bad'));
    const play = ev.target.closest('[data-playjob]');
    if (play) {
      const depot = Number(play.dataset.playjob);
      loadInstalled().then(items => {
        const found = items.filter(i => i.depot === depot).pop();
        if (found) openPlayer(found); else toast('Nothing extracted for that depot yet', 'bad');
      });
    }
  });

  $$('[data-close]').forEach(el => el.addEventListener('click', () => { $('#modal').hidden = true; }));
  $$('[data-close-player]').forEach(el => el.addEventListener('click', () => {
    $('#player-stage').innerHTML = '';
    $('#player').hidden = true;
  }));
  document.addEventListener('keydown', ev => {
    if (ev.key !== 'Escape') return;
    $('#modal').hidden = true;
    $('#player-stage').innerHTML = '';
    $('#player').hidden = true;
  });
}

wire();
boot();
