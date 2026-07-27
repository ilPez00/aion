/* hud.js — aion web HUD application layer.
 *
 * Five modules, one shell. Three of them (Files, Vault, System) are the SAME
 * organic graph fed by different adapters; the other two (Agent, LaTeX) are
 * panels. Each graph module also publishes a <table> twin, because a canvas
 * network graph is unreadable to assistive tech and unusable one-handed on a
 * phone — the list is a peer view you can switch to at any time, not a
 * degraded fallback.
 *
 * No framework, no build step, no CDN: the HUD must boot offline from the
 * service worker cache with nothing but what the daemon serves.
 */
'use strict';

/* ── DOM helpers ──────────────────────────────────────────────────────── */
const $ = id => document.getElementById(id);
const el = (tag, props = {}, kids = []) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;      // never innerHTML
    else if (k === 'on') for (const [ev, fn] of Object.entries(v)) n.addEventListener(ev, fn);
    else if (v != null) n.setAttribute(k, v);
  }
  for (const kid of [].concat(kids)) if (kid) n.append(kid);
  return n;
};
const fmtBytes = b => b > 1e9 ? (b / 1e9).toFixed(1) + 'G'
  : b > 1e6 ? (b / 1e6).toFixed(1) + 'M'
  : b > 1e3 ? (b / 1e3).toFixed(1) + 'K' : b + 'B';
const fmtDate = t => t ? new Date(t * 1000).toISOString().slice(0, 10) : '—';

/* Inline SVG icons — the design system forbids emoji as UI icons (they render
   differently per platform, carry no accessible name, and cannot be recoloured
   by the theme). 16px stroke icons, currentColor. */
const ICON = {
  files: 'M3 4h5l2 2h11v12H3z',
  vault: 'M5 3h14v18H5zM9 7h6M9 11h6M9 15h4',
  system: 'M4 4h16v12H4zM8 20h8M12 16v4',
  agent: 'M4 5h16v11H8l-4 4z',
  latex: 'M5 5h14M5 12h9M5 19h14',
  graph: 'M6 18a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM8.5 13.5l7-6',
  list: 'M4 6h16M4 12h16M4 18h16',
};
const icon = name => {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.7');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', ICON[name] || ICON.graph);
  svg.append(p);
  return svg;
};

/* ── fetch with honest errors ─────────────────────────────────────────── */
async function api(path, opts) {
  const r = await fetch(path, opts);
  const text = await r.text();
  let body = null;
  try { body = JSON.parse(text); } catch (_) { /* not json */ }
  if (!r.ok) throw new Error((body && body.error) || `${r.status} ${text.slice(0, 160)}`);
  return body;
}

/* ── app state ────────────────────────────────────────────────────────── */
const MODULES = [
  { id: 'files', label: 'Files', icon: 'files', kind: 'graph' },
  { id: 'agents', label: 'Agents', icon: 'graph', kind: 'graph' },
  { id: 'vault', label: 'Vault', icon: 'vault', kind: 'graph' },
  { id: 'system', label: 'System', icon: 'system', kind: 'graph' },
  { id: 'agent', label: 'Chat', icon: 'agent', kind: 'panel' },
  { id: 'latex', label: 'LaTeX', icon: 'latex', kind: 'panel' },
];

const S = {
  module: localStorage.getItem('aion_module') || 'files',
  view: localStorage.getItem('aion_view') || 'graph',   // graph | list
  dir: localStorage.getItem('aion_dir') || '',
  rows: [],           // list twin of whatever the graph shows
  selected: null,
  sessionId: null,
  streaming: false,
  live: false,
};
let graph = null;

function setStatus(msg, isErr = false) {
  const s = $('status');
  s.textContent = msg;
  s.className = isErr ? 'err' : 'muted';
  // status is aria-live, so failures are announced rather than only coloured
}

/* ── shell ────────────────────────────────────────────────────────────── */
function buildNav() {
  $('nav').replaceChildren(...MODULES.map(m =>
    el('button', {
      class: 'nav-item', id: `nav-${m.id}`, type: 'button',
      title: m.label, on: { click: () => go(m.id) },
    }, [icon(m.icon), el('span', { class: 'nav-label', text: m.label })])));
}

const LOADERS = {
  files: loadFiles, agents: loadAgents, vault: loadVault,
  system: loadSystem, agent: loadAgent, latex: loadLatex,
};

/* Navigate. `push` writes a history entry so the browser back button — the
 * one navigation control every user already knows — walks module and
 * directory history instead of leaving the app. */
function go(id, opts = {}) {
  if (!LOADERS[id]) id = 'files';
  S.module = id;
  localStorage.setItem('aion_module', id);
  for (const m of MODULES) {
    const b = $(`nav-${m.id}`);
    if (m.id === id) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  }
  const mod = MODULES.find(m => m.id === id);
  $('module-title').textContent = mod.label.toUpperCase();
  const wrap = $('canvas-wrap');
  wrap.classList.toggle('panel-mode', mod.kind === 'panel');
  $('graph-tools').hidden = mod.kind !== 'graph';
  $('fs-tools').hidden = id !== 'files';
  $('crumbs').hidden = id !== 'files';
  applyView();
  graph && graph.clearFocus();
  showFocusBadge(null);
  if (opts.push !== false) pushState();
  return LOADERS[id](opts);
}

/* ── history / deep links ─────────────────────────────────────────────── */
/* The URL is the app's address bar: #files?dir=/x/y is shareable, bookmarkable
 * and survives a reload. Without it, "where was I" is lost on every refresh. */
function stateHash() {
  const p = new URLSearchParams();
  if (S.module === 'files' && S.dir) p.set('dir', S.dir);
  if (S.view !== 'graph') p.set('view', S.view);
  const q = p.toString();
  return `#${S.module}${q ? '?' + q : ''}`;
}

function pushState() {
  const h = stateHash();
  if (h !== location.hash) history.pushState({ h }, '', h);
}

function applyHash(push = false) {
  const raw = location.hash.replace(/^#/, '');
  if (!raw) return false;
  const [mod, qs] = raw.split('?');
  const p = new URLSearchParams(qs || '');
  if (p.get('view')) { S.view = p.get('view'); localStorage.setItem('aion_view', S.view); }
  if (p.get('dir')) { S.dir = p.get('dir'); $('dir').value = S.dir; }
  go(LOADERS[mod] ? mod : 'files', { push });
  return true;
}

function applyView() {
  const mod = MODULES.find(m => m.id === S.module);
  const listMode = S.view === 'list' && mod.kind === 'graph';
  $('canvas-wrap').classList.toggle('list-mode', listMode);
  $('view-toggle').setAttribute('aria-pressed', String(listMode));
  $('view-toggle').title = listMode ? 'Show organic graph' : 'Show list view';
  // `btn-label`, not `nav-label`: the rail hides its labels on narrow screens,
  // and this button must keep saying which view it switches TO.
  $('view-toggle').replaceChildren(icon(listMode ? 'graph' : 'list'),
    el('span', { class: 'btn-label', text: listMode ? 'Graph' : 'List' }));
}

function toggleView() {
  S.view = S.view === 'list' ? 'graph' : 'list';
  localStorage.setItem('aion_view', S.view);
  applyView();
  if (S.view === 'graph') graph.fit();
}

/* ── inspector ────────────────────────────────────────────────────────── */
function showSelection(n) {
  S.selected = n;
  // Clicking a hub isolates it and its members. Discoverable without knowing
  // the keyboard shortcut, and clicking the same hub again releases it.
  if (n && n.hub) {
    if (graph._focusAnchor === n) { graph.clearFocus(); showFocusBadge(null); }
    else { graph.focusOn(n); showFocusBadge(n); }
  }
  const body = $('sel-body');
  $('move-box').hidden = !(n && n.path);
  if (!n) {
    body.replaceChildren(el('p', { class: 'muted', text: 'Select a node.' }));
    $('preview').textContent = '—';
    return;
  }
  const dl = el('dl');
  const add = (k, v) => { dl.append(el('dt', { text: k }), el('dd', { text: String(v) })); };
  add('name', n.label);
  if (n.path) add('path', n.path);
  if (n.kind) add('kind', n.kind);
  if (n.hubName) add('cluster', n.hubName);
  if (n.size != null) add('size', fmtBytes(n.size));
  if (n.mtime) add('modified', fmtDate(n.mtime));
  if (n.detail) add('value', n.detail);
  add('links', n.deg ?? 0);
  body.replaceChildren(dl);
  if (n.path) { $('dest').value = n.path; previewFile(n.path); }
  else if (n.noteId) { previewNote(n.noteId); }
  else if (n.taskLog) {
    // The tail of a task's log is the only thing that answers "what is it
    // actually doing" — show it where a file would show its contents.
    $('preview').textContent = n.taskLog.length
      ? n.taskLog.join('\n') : '(no output captured)';
  } else { $('preview').textContent = n.detail || '—'; }
  if (window.innerWidth <= 1023) $('inspector').classList.add('open');
}

async function previewFile(path) {
  $('preview').textContent = 'loading…';
  try {
    const j = await api(`/api/fs/file?path=${encodeURIComponent(path)}`);
    $('preview').textContent = j.content + (j.truncated ? `\n\n… truncated (${fmtBytes(j.size)})` : '');
  } catch (e) { $('preview').textContent = String(e.message); }
}

async function previewNote(name) {
  try {
    const j = await api(`/api/notes/content?name=${encodeURIComponent(name)}`);
    $('preview').textContent = j.text || '(empty)';
  } catch (e) { $('preview').textContent = String(e.message); }
}

async function doMove() {
  const n = S.selected;
  if (!n || !n.path) return;
  const dest = $('dest').value.trim();
  if (!dest || dest === n.path) return;
  setStatus('moving…');
  try {
    const j = await api('/api/fs/move', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ from: n.path, to: dest }),
    });
    setStatus(`moved → ${j.to}`);
    loadFiles();
  } catch (e) { setStatus(e.message, true); }
}

const HINT = 'drag · scroll zoom · click a hub to isolate · 0 fit';

/* Isolation is invisible once applied — the graph just has fewer nodes, which
 * reads as a failed load. A dismissible badge names the state and how to
 * leave it. */
function showFocusBadge(anchor) {
  const bar = $('focus-badge');
  if (!anchor) { bar.hidden = true; return; }
  bar.hidden = false;
  bar.replaceChildren(
    el('span', { text: `isolated: ${anchor.label}` }),
    el('button', { type: 'button', class: 'chip-x', text: 'clear ✕',
                   on: { click: () => { graph.clearFocus(); showFocusBadge(null); } } }));
}

/* ── legend + list ────────────────────────────────────────────────────── */
function renderLegend(items) {
  $('legend').replaceChildren(...items.map(it =>
    el('span', { class: 'legend-chip' }, [
      el('span', { class: 'legend-dot', style: `background:${it.color}` }),
      el('span', { text: it.label }),
    ])));
}

/* The accessible twin. Same data, same click targets, keyboard-native. */
function renderList(cols, rows, onPick) {
  S.rows = rows;
  const thead = el('thead', {}, el('tr', {}, cols.map(c => el('th', { text: c.label }))));
  const tbody = el('tbody');
  rows.forEach((r, i) => {
    const tr = el('tr', {
      tabindex: '0', role: 'button',
      title: r._title || '',
      on: {
        click: () => onPick(r, i),
        keydown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onPick(r, i); } },
      },
    }, cols.map(c => el('td', { text: String(r[c.key] ?? '—') })));
    tbody.append(tr);
  });
  $('list-view').replaceChildren(
    el('table', { class: 'data' }, [thead, tbody]),
    el('p', { class: 'muted mono-sm', text: `${rows.length} rows` }));
}

/* ── module: Files (graph file manager) ───────────────────────────────── */
async function loadRoots() {
  try {
    const j = await api('/api/fs/roots');
    $('roots').replaceChildren(...j.roots.map(r =>
      el('option', { value: r.path, text: r.name })));
    if (!S.dir) S.dir = j.roots[0] ? j.roots[0].path : j.root;
    $('dir').value = S.dir;
  } catch (e) { setStatus(e.message, true); }
}

/* Clickable path segments. Descending into a directory was previously only
 * possible by typing an absolute path — the single worst thing about the
 * first cut of this module. */
function renderCrumbs(root, dir) {
  const parts = dir.split('/').filter(Boolean);
  const kids = [el('button', { class: 'crumb', type: 'button', title: root,
                               text: '/', on: { click: () => scanDir(root) } })];
  let acc = '';
  for (const seg of parts) {
    acc += '/' + seg;
    const target = acc;
    if (!(target === root || root.startsWith(target) || target.startsWith(root))) continue;
    kids.push(el('span', { class: 'crumb-sep', text: '›' }));
    kids.push(el('button', {
      class: 'crumb', type: 'button', text: seg, title: target,
      on: { click: () => scanDir(target) },
    }));
  }
  kids[kids.length - 1]?.setAttribute('aria-current', 'location');
  $('crumbs').replaceChildren(...kids);
}

function scanDir(dir) {
  S.dir = dir;
  $('dir').value = dir;
  pushState();
  loadFiles();
}

async function loadFiles() {
  if (!S.dir) await loadRoots();
  setStatus('scanning…');
  const params = new URLSearchParams({
    dir: S.dir, depth: $('depth').value, hidden: $('hidden').checked ? '1' : '0',
  });
  try {
    const g = await api('/api/fs/graph?' + params);
    localStorage.setItem('aion_dir', S.dir);
    S.fsRoot = g.root;
    renderCrumbs(S.rootPath || g.root, g.root);

    const hubName = {}, hubOf = {}, best = {};
    g.themes.forEach(t => { hubName[t.id] = t.name; });
    for (const e of [...g.edges].sort((a, b) => b.score - a.score)) {
      if (best[e.file_id] === undefined) { best[e.file_id] = e.score; hubOf[e.file_id] = e.theme_id; }
    }
    const maxDeg = Math.max(1, ...g.themes.map(t => g.edges.filter(e => e.theme_id === t.id).length));

    const nodes = [
      ...g.themes.map(t => ({
        id: `h${t.id}`, label: t.name, hub: true, kind: 'hub', group: t.id,
        weight: g.edges.filter(e => e.theme_id === t.id).length / maxDeg,
      })),
      ...g.files.map(f => ({
        id: `f${f.id}`, label: f.title, path: f.path, kind: f.kind,
        group: hubOf[f.id] ?? 0, hubName: hubName[hubOf[f.id]] || 'unclustered',
        size: f.size, mtime: f.mtime,
        weight: Math.min(1, Math.log10(1 + f.size) / 6),
      })),
    ];
    const links = [
      ...g.edges.map(e => ({ source: `h${e.theme_id}`, target: `f${e.file_id}`, weight: e.score, kind: 'hub' })),
      ...g.file_edges.map(e => ({ source: `f${e.source}`, target: `f${e.target}`, weight: e.score, kind: 'sim' })),
    ];
    graph.setData(nodes, links);
    graph.fit();
    renderLegend(g.themes.map(t => ({
      label: t.name, color: getComputedStyle(document.documentElement)
        .getPropertyValue(`--c${(t.id % 8) + 1}`).trim(),
    })));

    const byId = new Map(nodes.map(n => [n.id, n]));
    renderList(
      [{ key: 'hub', label: 'cluster' }, { key: 'title', label: 'file' },
       { key: 'kind', label: 'kind' }, { key: 'sizeh', label: 'size' },
       { key: 'modified', label: 'modified' }],
      g.files.map(f => ({
        hub: hubName[hubOf[f.id]] || 'unclustered', title: f.title, kind: f.kind,
        sizeh: fmtBytes(f.size), modified: fmtDate(f.mtime), _id: `f${f.id}`, _title: f.path,
      })),
      r => { const n = byId.get(r._id); graph.select(n); showSelection(n); });

    setStatus(`${g.files.length} files · ${g.themes.length} clusters · ` +
      `${g.file_edges.length} links${g.truncated ? ' · TRUNCATED' : ''}`);
    $('graph-desc').textContent = graph.describe();
  } catch (e) { setStatus(e.message, true); }
}

/* ── module: Agents (aion's own work as a graph) ──────────────────────── */
/* Same visual language as Files, applied to process state: fleet instances
 * are the outermost hubs, harnesses hang off them, tasks hang off harnesses,
 * swarm agents wire to their dependencies. The point of the graph form here
 * is the thing a task list cannot show — six tasks queued behind one stalled
 * harness, or one box saturated while the rest of the fleet idles. */
const STATE_GROUP = {
  running: 2, pending: 3, done: 6, failed: 5, cancelled: 5, interrupted: 4,
  working: 2, planning: 2, idle: 3, waiting: 3, blocked: 4,
};
const STATE_GLYPH = {
  running: '▶', pending: '·', done: '✓', failed: '✗', cancelled: '⊘',
  interrupted: '⏸', working: '▶', planning: '◌', idle: '·', waiting: '⌛',
  blocked: '⊘',
};

async function loadAgents() {
  setStatus('reading fleet…');
  try {
    const a = await api('/api/agents');
    if (a.error) { setStatus(a.error, true); return; }
    S.agents = a;

    const nodes = [], links = [];
    const usedHarness = new Set(a.tasks.map(t => t.harness));

    for (const i of a.instances) {
      nodes.push({
        id: `i${i.id}`, label: `${i.id}${i.alive ? '' : ' (offline)'}`, hub: true,
        kind: 'hub', group: i.alive ? 2 : 0, weight: 1,
        detail: `${i.hostname || 'local'} · pid ${i.pid || '—'} · ` +
                `${i.alive ? 'live' : 'offline'} · ${i.running_count} running`,
      });
    }
    // Harnesses that never ran anything would trebble the node count for no
    // information, so only those with work (or currently active) appear.
    for (const h of a.harnesses) {
      const active = a.instances.some(i => i.active_harness === h.id);
      if (!usedHarness.has(h.id) && !active) continue;
      nodes.push({
        id: `h${h.id}`, label: h.name, hub: true, kind: 'hub',
        group: h.orphan ? 5 : (h.enabled ? 1 : 0),
        weight: Math.min(1, 0.3 + a.tasks.filter(t => t.harness === h.id).length / 8),
        detail: `${h.tier} · ${h.type}${h.vram_mb ? ` · ${h.vram_mb}MB` : ''}` +
                `${h.orphan ? ' · ORPHAN (not in config)' : ''}` +
                `${h.requires_approval ? ' · gated' : ''}`,
      });
      for (const i of a.instances) {
        if (i.active_harness === h.id) {
          links.push({ source: `i${i.id}`, target: `h${h.id}`, weight: 0.9, kind: 'hub' });
        }
      }
    }
    for (const t of a.tasks) {
      const id = `t${t.instance}:${t.id}`;
      nodes.push({
        id, label: `${STATE_GLYPH[t.state] || '·'} ${t.label || t.id}`.slice(0, 42),
        kind: 'metric', group: STATE_GROUP[t.state] ?? 0,
        weight: 0.25 + 0.75 * (t.progress || 0),
        detail: `${t.state} · ${Math.round((t.progress || 0) * 100)}%` +
                `${t.eta ? ` · eta ${t.eta}s` : ''}${t.domain ? ` · ${t.domain}` : ''}`,
        taskLog: t.log, state: t.state, instance: t.instance, harness: t.harness,
      });
      if (nodes.some(n => n.id === `h${t.harness}`)) {
        links.push({ source: `h${t.harness}`, target: id,
                     weight: 0.4 + 0.6 * (t.progress || 0), kind: 'hub' });
      } else {
        links.push({ source: `i${t.instance}`, target: id, weight: 0.4, kind: 'hub' });
      }
    }
    // Swarm dependency DAG. `deps` holds agent NAMES, not ids (see the
    // comment on SwarmAgent.dependencies), so resolve through a name index —
    // treating them as ids silently yields a swarm with no edges at all,
    // which is the one thing this view exists to show.
    const swarmByName = new Map(a.swarm.map(s => [s.name, s]));
    for (const s of a.swarm) {
      nodes.push({
        id: `s${s.id}`, label: s.name || s.id, kind: 'config',
        group: STATE_GROUP[s.status] ?? 0, weight: 0.3 + 0.7 * (s.progress || 0),
        detail: `${s.status} · ${s.goal || ''}`.slice(0, 120),
        swarmGoal: s.goal, taskLog: s.logs,
      });
      for (const d of (s.deps || [])) {
        const dep = swarmByName.get(d);
        if (dep) links.push({ source: `s${dep.id}`, target: `s${s.id}`, weight: 0.8, kind: 'hub' });
        else setStatus(`swarm agent "${s.name}" waits on missing "${d}"`, true);
      }
      // Attach the swarm to its instance so it is not a floating island.
      if (s.instance) links.push({ source: `i${s.instance}`, target: `s${s.id}`, weight: 0.3, kind: 'sim' });
    }

    graph.setData(nodes, links);
    graph.fit();
    renderLegend([
      ['live / running', 2], ['queued', 3], ['stalled', 4],
      ['failed', 5], ['done', 6], ['harness', 1],
    ].map(([label, c]) => ({ label, color: swatch(c) })));

    renderList(
      [{ key: 'state', label: 'state' }, { key: 'label', label: 'task' },
       { key: 'harness', label: 'harness' }, { key: 'instance', label: 'instance' },
       { key: 'pct', label: 'progress' }],
      a.tasks.map(t => ({
        state: t.state, label: t.label || t.id, harness: t.harness,
        instance: t.instance, pct: `${Math.round((t.progress || 0) * 100)}%`,
        _id: `t${t.instance}:${t.id}`, _title: (t.log || []).slice(-1)[0] || '',
      })),
      r => { graph.reveal(r._id); });

    const s = a.summary;
    setStatus(`${s.live_instances}/${s.instances} live · ${s.tasks} tasks · ` +
      `${s.active} active${s.by_state.interrupted ? ` · ${s.by_state.interrupted} interrupted` : ''}`);
    $('graph-desc').textContent = graph.describe();
  } catch (e) { setStatus(e.message, true); }
}

const swatch = i => getComputedStyle(document.documentElement)
  .getPropertyValue(`--c${(i % 8) + 1}`).trim();

/* ── module: Vault (notes graph) ──────────────────────────────────────── */
async function loadVault() {
  setStatus('reading vault…');
  try {
    const g = await api('/api/notes');
    const maxDeg = Math.max(1, ...g.nodes.map(n => n.degree || 0));
    // Colour by tag family so clusters read; nodes with no tag share a group.
    const tags = [...new Set(g.nodes.flatMap(n => n.tags || []))];
    const nodes = g.nodes.map(n => ({
      id: n.id, label: n.label, noteId: n.id, kind: 'note',
      group: n.tags && n.tags.length ? tags.indexOf(n.tags[0]) + 1 : 0,
      weight: (n.degree || 0) / maxDeg,
      hub: (n.degree || 0) >= maxDeg * 0.8,
      detail: `${n.degree || 0} links · ${n.backlinks || 0} backlinks`,
    }));
    const links = g.edges.map(e => ({ source: e.s, target: e.t, weight: 0.6, kind: 'sim' }));
    graph.setData(nodes, links);
    graph.fit();
    renderLegend(tags.slice(0, 8).map((t, i) => ({
      label: '#' + t,
      color: getComputedStyle(document.documentElement).getPropertyValue(`--c${(i + 2) % 8 + 1}`).trim(),
    })));
    const byId = new Map(nodes.map(n => [n.id, n]));
    renderList(
      [{ key: 'label', label: 'note' }, { key: 'degree', label: 'links' },
       { key: 'backlinks', label: 'backlinks' }, { key: 'tags', label: 'tags' }],
      g.nodes.map(n => ({
        label: n.label, degree: n.degree || 0, backlinks: n.backlinks || 0,
        tags: (n.tags || []).join(' ') || '—', _id: n.id,
      })),
      r => { const n = byId.get(r._id); graph.select(n); showSelection(n); });
    setStatus(`${g.nodes.length} notes · ${g.edges.length} wikilinks`);
    $('graph-desc').textContent = graph.describe();
  } catch (e) { setStatus(e.message, true); }
}

/* ── module: System (telemetry as an organic constellation) ───────────── */
/* Telemetry is not naturally a network, so the graph earns its keep by
 * encoding load two ways at once: node size AND colour band, orbiting a host
 * core. Per-core satellites hang off the CPU node, so a hot core is visible
 * as a bulge in one limb rather than a number in a list of sixteen. The list
 * twin carries the exact percentages, which is what you want when comparing. */
function band(pct) { return pct >= 85 ? 5 : pct >= 60 ? 3 : 2; }   // err / warn / ok

async function loadSystem() {
  try {
    const s = await api('/api/system');
    const metrics = [
      { id: 'cpu', label: `CPU ${s.cpu}%`, v: s.cpu },
      { id: 'mem', label: `RAM ${s.mem}%`, v: s.mem },
      { id: 'disk', label: `DISK ${s.disk}%`, v: s.disk },
    ];
    if (s.gpu) metrics.push({ id: 'gpu', label: `GPU ${s.gpu.util}%`, v: s.gpu.util });
    const nodes = [{ id: 'host', label: 'HOST', hub: true, kind: 'hub', group: 0, weight: 1,
                     detail: `${s.date} ${s.time}` }];
    const links = [];
    for (const m of metrics) {
      nodes.push({ id: m.id, label: m.label, kind: 'metric', group: band(m.v),
                   weight: Math.max(0.15, m.v / 100), detail: `${m.v}%`, hub: true });
      links.push({ source: 'host', target: m.id, weight: m.v / 100, kind: 'hub' });
    }
    (s.per_core || []).forEach((c, i) => {
      nodes.push({ id: `core${i}`, label: `c${i}`, kind: 'metric', group: band(c),
                   weight: Math.max(0.1, c / 100), detail: `${c}%` });
      links.push({ source: 'cpu', target: `core${i}`, weight: c / 100, kind: 'sim' });
    });
    for (const k of ['net_up', 'net_down']) {
      nodes.push({ id: k, label: `${k === 'net_up' ? '↑' : '↓'} ${fmtBytes(s[k])}`,
                   kind: 'metric', group: 6, weight: 0.35, detail: fmtBytes(s[k]) });
      links.push({ source: 'host', target: k, weight: 0.4, kind: 'hub' });
    }
    graph.setData(nodes, links);
    if (!graph._sysFitted) { graph.fit(); graph._sysFitted = true; }
    renderLegend([
      { label: 'nominal <60%', color: getComputedStyle(document.documentElement).getPropertyValue('--c2').trim() },
      { label: 'loaded 60–85%', color: getComputedStyle(document.documentElement).getPropertyValue('--c3').trim() },
      { label: 'saturated >85%', color: getComputedStyle(document.documentElement).getPropertyValue('--c5').trim() },
    ]);
    renderList(
      [{ key: 'metric', label: 'metric' }, { key: 'value', label: 'value' }, { key: 'state', label: 'state' }],
      [...metrics.map(m => ({ metric: m.id.toUpperCase(), value: `${m.v}%`,
                              state: m.v >= 85 ? 'saturated' : m.v >= 60 ? 'loaded' : 'nominal' })),
       ...(s.per_core || []).map((c, i) => ({ metric: `core ${i}`, value: `${c}%`,
                              state: c >= 85 ? 'saturated' : c >= 60 ? 'loaded' : 'nominal' })),
       { metric: 'net up', value: fmtBytes(s.net_up), state: '—' },
       { metric: 'net down', value: fmtBytes(s.net_down), state: '—' }],
      () => {});
    setStatus(`${s.date} ${s.time}`);
    $('graph-desc').textContent = graph.describe();
  } catch (e) { setStatus(e.message, true); }
}

/* ── module: Agent (chat) + LaTeX ─────────────────────────────────────── */
function loadAgent() {
  $('panel-view').replaceChildren($('chat-root'));
  $('chat-root').hidden = false;
  $('latex-root').hidden = true;
  if (!S.sessionId) initSessions();
  setStatus('agent ready');
}

function loadLatex() {
  $('panel-view').replaceChildren($('latex-root'));
  $('latex-root').hidden = false;
  $('chat-root').hidden = true;
  setStatus('latex ready');
}

async function initSessions() {
  try {
    const d = await api('/api/sessions');
    if (d.sessions.length) {
      S.sessionId = d.sessions[0].id;
      const sess = await api(`/api/session?id=${S.sessionId}`);
      $('messages').replaceChildren(...(sess.messages || []).map(m =>
        el('div', { class: `msg ${m.role}`, text: m.content })));
    } else {
      S.sessionId = (await api('/api/session/new')).id;
    }
  } catch (e) { setStatus(e.message, true); }
}

function sendMessage() {
  const input = $('chat-input');
  const text = input.value.trim();
  if (!text || S.streaming) return;
  input.value = '';
  $('messages').append(el('div', { class: 'msg user', text }));
  const out = el('div', { class: 'msg assistant', text: '' });
  const cursor = el('span', { class: 'streaming-cursor', text: '▊' });
  out.append(cursor);
  $('messages').append(out);
  $('messages').scrollTop = $('messages').scrollHeight;

  S.streaming = true;
  const src = new EventSource(
    `/api/agent/stream?session=${encodeURIComponent(S.sessionId)}&text=${encodeURIComponent(text)}`);
  let full = '';
  src.addEventListener('token', e => {
    try {
      const d = JSON.parse(e.data);
      if (!d.token) return;
      full += d.token;
      out.textContent = full;
      out.append(cursor);
      $('messages').scrollTop = $('messages').scrollHeight;
    } catch (_) {}
  });
  const finish = () => { src.close(); S.streaming = false; cursor.remove(); };
  src.addEventListener('done', finish);
  src.onerror = finish;
}

async function compileLatex() {
  const src = $('latex-area').value;
  if (!src) return;
  $('latex-log').textContent = 'compiling…';
  try {
    const d = await api('/api/latex', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ src }),
    });
    if (d.ok && d.pdf) {
      $('latex-log').textContent = 'OK';
      $('latex-preview').hidden = false;
      $('latex-preview').src = d.pdf + '?' + Date.now();
    } else {
      $('latex-log').textContent = (d.log || 'error').slice(-1200);
      $('latex-preview').hidden = true;
    }
  } catch (e) { $('latex-log').textContent = e.message; }
}

/* ── command palette ──────────────────────────────────────────────────── */
/* Ctrl-K, the same key the TUI cockpit uses, so one reflex works in both.
 * Searches every corpus at once — modules, harnesses, tasks, task logs,
 * notes, filenames, file contents — and every hit carries the coordinates to
 * jump to it. This is the answer to "searchable and easily navigable": you
 * stop needing to know which module a thing lives in. */
const PAL = { open: false, items: [], idx: 0, seq: 0 };

function openPalette(prefill = '') {
  PAL.open = true;
  $('palette').hidden = false;
  const inp = $('pal-input');
  inp.value = prefill;
  inp.focus();
  inp.select();
  runPalette(prefill);
}

function closePalette() {
  PAL.open = false;
  $('palette').hidden = true;
  $('graph-canvas').focus();
}

let palTimer = null;
function schedulePalette(q) {
  clearTimeout(palTimer);
  // Content search touches the disk; debounce so typing doesn't queue a scan
  // per keystroke.
  palTimer = setTimeout(() => runPalette(q), 140);
}

async function runPalette(q) {
  const seq = ++PAL.seq;
  const list = $('pal-list');
  if (!q.trim()) {
    PAL.items = MODULES.map(m => ({
      type: 'module', label: m.label, sub: `go to ${m.id}`, module: m.id, node: null }));
    PAL.idx = 0;
    return paintPalette();
  }
  try {
    const params = new URLSearchParams({ q });
    if (S.dir) params.set('dir', S.dir);
    const r = await api('/api/search/all?' + params);
    if (seq !== PAL.seq) return;             // a newer query already landed
    PAL.items = r.results;
    PAL.idx = 0;
    paintPalette();
  } catch (e) {
    list.replaceChildren(el('div', { class: 'pal-empty err', text: e.message }));
  }
}

const TYPE_TAG = {
  module: 'GO', harness: 'HARNESS', task: 'TASK', instance: 'NODE',
  swarm: 'SWARM', note: 'NOTE', file: 'FILE',
};

function paintPalette() {
  const list = $('pal-list');
  if (!PAL.items.length) {
    list.replaceChildren(el('div', { class: 'pal-empty muted', text: 'no matches' }));
    $('pal-count').textContent = '';
    return;
  }
  list.replaceChildren(...PAL.items.map((it, i) => el('div', {
    class: 'pal-item' + (i === PAL.idx ? ' active' : ''),
    role: 'option', id: `pal-opt-${i}`,
    'aria-selected': String(i === PAL.idx),
    on: { click: () => { PAL.idx = i; runPaletteAction(); } },
  }, [
    el('span', { class: 'pal-tag', text: TYPE_TAG[it.type] || it.type }),
    el('span', { class: 'pal-label', text: it.label }),
    el('span', { class: 'pal-sub', text: it.sub || '' }),
  ])));
  $('pal-count').textContent = `${PAL.items.length}`;
  $('pal-input').setAttribute('aria-activedescendant', `pal-opt-${PAL.idx}`);
  list.children[PAL.idx]?.scrollIntoView({ block: 'nearest' });
}

function movePalette(d) {
  if (!PAL.items.length) return;
  PAL.idx = (PAL.idx + d + PAL.items.length) % PAL.items.length;
  paintPalette();
}

/* Jump to a hit. Files need a directory change before the node can exist, so
 * this awaits the scan and only then reveals — otherwise the reveal fires
 * against the previous graph and silently does nothing. */
async function runPaletteAction() {
  const it = PAL.items[PAL.idx];
  if (!it) return;
  closePalette();
  if (it.type === 'file') {
    const dir = it.node.slice(0, it.node.lastIndexOf('/')) || '/';
    if (dir !== S.dir) { S.dir = dir; $('dir').value = dir; }
    await go('files');
    // node ids in the Files graph are f<index>; find by path instead
    const target = graph.nodes.find(n => n.path === it.node);
    if (target) { graph.reveal(target); showSelection(target); }
    else setStatus(`${it.label} is outside the current scan depth`, true);
    return;
  }
  await go(it.module);
  if (it.node) {
    if (!graph.reveal(it.node)) setStatus(`${it.label} not in view`, true);
    else showSelection(graph.selected);
  }
}

/* ── voice ────────────────────────────────────────────────────────────── */
let recognition = null;
function toggleVoice() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const btn = $('voice-btn');
  if (!SR) { setStatus('voice not supported in this browser', true); return; }
  if (recognition) { recognition.stop(); return; }
  recognition = new SR();
  recognition.continuous = true;
  recognition.interimResults = true;
  let finalText = '', silence = null;
  recognition.onresult = e => {
    clearTimeout(silence);
    silence = setTimeout(() => recognition && recognition.stop(), 1500);
    let interim = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      if (e.results[i].isFinal) finalText += (finalText ? ' ' : '') + e.results[i][0].transcript;
      else interim += e.results[i][0].transcript;
    }
    $('chat-input').value = finalText + interim;
  };
  const stop = () => {
    recognition = null;
    btn.setAttribute('aria-pressed', 'false');
    if (finalText.trim()) sendMessage();
  };
  recognition.onend = stop;
  recognition.onerror = stop;
  btn.setAttribute('aria-pressed', 'true');
  recognition.start();
}

/* ── live channel ─────────────────────────────────────────────────────── */
/* The cockpit and this HUD are different processes, so there is no shared
 * bus to subscribe to. The daemon watches the checkpoint files the cockpit
 * already writes and pushes what moved; here we apply it to whichever view
 * is on screen. Falls back to the existing polling if the socket is down, so
 * the HUD degrades to "slightly stale" rather than "wrong". */
let evtWs = null, wsBackoff = 1000;

function connectEvents() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  // The PTY/event socket is derived from the HTTP port (see WS_PORT in
  // aion_web.py) so two HUDs on one box do not fight over it.
  const port = (Number(location.port) || 8742) + 1;
  try { evtWs = new WebSocket(`${proto}//${location.hostname}:${port}/ws/events`); }
  catch (_) { return; }

  evtWs.onopen = () => { wsBackoff = 1000; setLive(true); };
  evtWs.onmessage = e => {
    let d; try { d = JSON.parse(e.data); } catch (_) { return; }
    if (d.type === 'stats') applyVitals(d);
    else if (d.type === 'agents') applyAgentEvent(d);
    else if (d.type === 'agents_error') setStatus(d.error, true);
  };
  const retry = () => {
    setLive(false);
    evtWs = null;
    setTimeout(connectEvents, wsBackoff);
    wsBackoff = Math.min(wsBackoff * 2, 30000);   // don't hammer a dead daemon
  };
  evtWs.onclose = retry;
  evtWs.onerror = () => { try { evtWs.close(); } catch (_) {} };
}

function setLive(on) {
  S.live = on;
  const dot = $('live-dot');
  dot.className = 'live-dot' + (on ? ' on' : '');
  dot.title = on ? 'live — pushing changes' : 'offline — polling';
}

/* Apply a fleet delta to the Agents graph without rebuilding it. When another
 * module is on screen we only take the summary, so switching to Agents later
 * still lands on current data. */
function applyAgentEvent(d) {
  S.agentSummary = d.summary;
  if (S.module !== 'agents') return;
  if (d.full || !graph.nodes.length) { loadAgents(); return; }

  const update = [], add = [];
  for (const t of d.changed) {
    const id = `t${t.instance}:${t.id}`;
    const node = {
      id, label: `${STATE_GLYPH[t.state] || '·'} ${t.label || t.id}`.slice(0, 42),
      kind: 'metric', group: STATE_GROUP[t.state] ?? 0,
      weight: 0.25 + 0.75 * (t.progress || 0),
      detail: `${t.state} · ${Math.round((t.progress || 0) * 100)}%` +
              `${t.eta ? ` · eta ${t.eta}s` : ''}`,
      taskLog: t.log, state: t.state, instance: t.instance, harness: t.harness,
    };
    if (t._new) add.push({ ...node, near: `h${t.harness}` });
    else update.push(node);
  }
  graph.patch({ update, add, remove: d.removed.map(k => `t${k}`) });

  // A new task needs an edge to its harness; that is structural, so rebuild
  // the adapter rather than guessing. Cheap, and only on actual arrivals.
  if (add.length || d.removed.length) loadAgents();

  const s = d.summary;
  if (s) {
    setStatus(`${s.live_instances}/${s.instances} live · ${s.tasks} tasks · ` +
      `${s.active} active${s.by_state.interrupted ? ` · ${s.by_state.interrupted} interrupted` : ''}`);
  }
  if (S.selected && S.selected.taskLog) showSelection(graph.nodeById(S.selected.id));
}

/* ── rail vitals (always-on, cheap) ───────────────────────────────────── */
function applyVitals(s) {
  const set = (k, pct, txt) => {
    $(`v-${k}`).textContent = txt;
    const bar = $(`m-${k}`);
    bar.style.width = Math.max(0, Math.min(100, pct)) + '%';
    bar.style.background = `var(--c${band(pct)})`;
  };
  set('cpu', s.cpu, s.cpu + '%');
  set('mem', s.mem, s.mem + '%');
  set('dsk', s.disk, s.disk + '%');
  if (s.gpu) set('gpu', s.gpu.util, s.gpu.util + '%');
  if (S.module === 'system') loadSystem();
}

/* Fallback path only. When the socket is up the daemon pushes stats and this
 * does nothing — no point paying for an HTTP round trip per tick as well. */
async function pollVitals() {
  if (S.live) return;
  try { applyVitals(await api('/api/system')); }
  catch (_) { /* daemon restart — the next tick recovers */ }
}

/* ── boot ─────────────────────────────────────────────────────────────── */
function boot() {
  buildNav();
  graph = new OrganicGraph($('graph-canvas'), {
    onSelect: showSelection,
    onHover: n => { $('hint').textContent = n ? (n.path || n.label) : HINT; },
    onFocusChange: showFocusBadge,
  });

  $('view-toggle').addEventListener('click', toggleView);
  $('refresh').addEventListener('click', () => go(S.module));
  $('fit').addEventListener('click', () => graph.fit());
  $('search').addEventListener('input', e => graph.setFilter(e.target.value));
  $('dir').addEventListener('change', e => { S.dir = e.target.value.trim(); loadFiles(); });
  $('roots').addEventListener('change', e => {
    if (!e.target.value) return;
    S.dir = e.target.value; $('dir').value = S.dir; loadFiles();
  });
  $('depth').addEventListener('change', loadFiles);
  $('hidden').addEventListener('change', loadFiles);
  $('move').addEventListener('click', doMove);
  $('inspector-close').addEventListener('click', () => $('inspector').classList.remove('open'));
  $('send').addEventListener('click', sendMessage);
  $('voice-btn').addEventListener('click', toggleVoice);
  $('chat-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  $('latex-compile').addEventListener('click', compileLatex);

  // Palette wiring
  $('pal-input').addEventListener('input', e => schedulePalette(e.target.value));
  $('pal-input').addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') { e.preventDefault(); movePalette(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); movePalette(-1); }
    else if (e.key === 'Enter') { e.preventDefault(); runPaletteAction(); }
    else if (e.key === 'Escape') { e.preventDefault(); closePalette(); }
  });
  $('palette').addEventListener('click', e => {
    if (e.target.id === 'palette') closePalette();     // click the scrim
  });
  $('pal-open').addEventListener('click', () => openPalette());

  // Browser back/forward walks module + directory history.
  addEventListener('popstate', () => applyHash(false));

  // Global shortcuts. Digits jump modules (the TUI cockpit uses the same
  // convention), Ctrl-K opens the palette exactly as it does in the TUI.
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
      e.preventDefault();
      PAL.open ? closePalette() : openPalette();
      return;
    }
    if (PAL.open) return;
    if (e.target.matches('input, textarea, select')) {
      if (e.key === 'Escape') e.target.blur();
      return;
    }
    const n = parseInt(e.key, 10);
    if (n >= 1 && n <= MODULES.length) { go(MODULES[n - 1].id); return; }
    if (e.key === '/') { e.preventDefault(); $('search').focus(); }
    if (e.key === 'g' || e.key === 'l') toggleView();
    if (e.key === 'r') go(S.module, { push: false });
    if (e.key === '?') $('inspector').classList.toggle('open');
    // Backspace climbs one directory — the file-manager reflex.
    if (e.key === 'Backspace' && S.module === 'files' && S.dir) {
      e.preventDefault();
      const up = S.dir.slice(0, S.dir.lastIndexOf('/')) || '/';
      if (up !== S.dir) scanDir(up);
    }
  });

  loadRoots().then(() => { if (!applyHash(false)) go(S.module, { push: false }); });
  connectEvents();
  pollVitals();
  setInterval(pollVitals, 3000);
  // Safety net only: the socket pushes fleet changes, so this catches the
  // case where the daemon is up but the websocket never connected.
  setInterval(() => { if (S.module === 'agents' && !S.live) loadAgents(); }, 5000);

  if ('serviceWorker' in navigator) {
    addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));
  }
}

boot();
