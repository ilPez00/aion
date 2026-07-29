/* organic_harness.js — headless driver for scripts/static/organic.js.
 *
 * Run by tests/test_organic_layout.py. The graph renderer is the one part of
 * the HUD with real algorithmic content (force layout, spatial hashing,
 * cluster anchoring) and no other way to test it — a browser is not available
 * in CI, so this shims just enough DOM/canvas for the module to load, then
 * asserts on the numbers the layout produces.
 *
 * Reads a graph payload as JSON on stdin, prints one JSON object on stdout.
 */
'use strict';

const NOOP = () => {};

// Canvas size drives the LOD budget, so it has to be settable: the budget
// curve is sub-linear in area and capped, and a fixed 1200x700 shim could
// not tell a correct curve from a straight line through one point.
const CANVAS_W = Number(process.env.CANVAS_W || 1200);
const CANVAS_H = Number(process.env.CANVAS_H || 700);

function mkCtx() {
  // Every 2D context call is a no-op; we only care that _draw() runs clean.
  return new Proxy({}, {
    get: (_, k) => {
      if (k === 'createRadialGradient') return () => ({ addColorStop: NOOP });
      if (k === 'canvas') return null;
      return NOOP;
    },
    set: () => true,
  });
}

function mkEl(tag = 'div') {
  return {
    tagName: tag, style: {}, children: [], attrs: {}, className: '', textContent: '',
    clientWidth: CANVAS_W, clientHeight: CANVAS_H, width: 0, height: 0,
    getContext: mkCtx, addEventListener: NOOP, removeEventListener: NOOP,
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    removeAttribute(k) { delete this.attrs[k]; },
    append(...k) { this.children.push(...k); },
    replaceChildren(...k) { this.children = k; },
    getBoundingClientRect: () => ({ left: 0, top: 0, width: CANVAS_W, height: CANVAS_H }),
    setPointerCapture: NOOP, focus: NOOP, remove: NOOP, matches: () => false,
  };
}

global.matchMedia = () => ({ matches: process.env.REDUCED_MOTION === '1' });
global.devicePixelRatio = 1;
global.ResizeObserver = class { observe() {} };
global.requestAnimationFrame = () => 0;
global.cancelAnimationFrame = NOOP;
global.getComputedStyle = () => ({ getPropertyValue: () => '#5ad1ff' });
global.document = {
  documentElement: mkEl(), createElement: mkEl,
  createElementNS: () => mkEl('svg'), getElementById: () => mkEl(),
  addEventListener: NOOP,
};
global.window = global;

require(process.env.ORGANIC_JS);

let raw = '';
process.stdin.on('data', d => { raw += d; });
process.stdin.on('end', () => {
  const g = JSON.parse(raw);

  // Same adapter hud.js uses for the Files module.
  const hubOf = {}, best = {};
  for (const e of [...g.edges].sort((a, b) => b.score - a.score)) {
    if (best[e.file_id] === undefined) { best[e.file_id] = e.score; hubOf[e.file_id] = e.theme_id; }
  }
  const nodes = [
    ...g.themes.map(t => ({ id: 'h' + t.id, label: t.name, hub: true, kind: 'hub', group: t.id, weight: 0.7 })),
    ...g.files.map(f => ({ id: 'f' + f.id, label: f.title, path: f.path, kind: f.kind,
                           group: hubOf[f.id] ?? 0, weight: 0.3 })),
  ];
  const links = [
    ...g.edges.map(e => ({ source: 'h' + e.theme_id, target: 'f' + e.file_id, weight: e.score, kind: 'hub' })),
    ...g.file_edges.map(e => ({ source: 'f' + e.source, target: 'f' + e.target, weight: e.score, kind: 'sim' })),
  ];

  // Callbacks are wired through opts, so the harness must go through opts too
  // -- reading lodInfo() directly is what let a never-assigned this.onLod ship.
  const lodCalls = [];
  const og = new OrganicGraph(mkEl('canvas'), {
    onLod: (info) => lodCalls.push(info),
  });
  if (process.env.DETAIL) og.setDetail(process.env.DETAIL);
  const t0 = Date.now();
  og.setData(nodes, links);
  for (let i = 0; i < 500 && og.alpha > 0; i++) og._step();
  const settleMs = Date.now() - t0;

  og._draw();                                   // must not throw
  og.fit();

  const finite = og.nodes.every(n => Number.isFinite(n.x) && Number.isFinite(n.y));
  const xs = og.nodes.map(n => n.x), ys = og.nodes.map(n => n.y);
  const span = og.nodes.length
    ? Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)) : 0;

  // Does the picture agree with the maths? A file should end up nearest to
  // the hub it was actually clustered into — otherwise proximity on screen
  // means nothing and the whole visualisation is decorative.
  const hubNodes = og.nodes.filter(n => n.hub);
  let agree = 0, total = 0;
  for (const f of g.files) {
    const fn = og.nodes.find(n => n.id === 'f' + f.id);
    if (!fn || !hubNodes.length || hubOf[f.id] === undefined) continue;
    const nearest = hubNodes.reduce((a, b) =>
      Math.hypot(fn.x - a.x, fn.y - a.y) < Math.hypot(fn.x - b.x, fn.y - b.y) ? a : b);
    if (nearest.id === 'h' + hubOf[f.id]) agree++;
    total++;
  }

  // Minimum hub separation — collapsed hubs mean unreadable overlapping clusters.
  let minHubGap = Infinity;
  for (let i = 0; i < hubNodes.length; i++) {
    for (let j = i + 1; j < hubNodes.length; j++) {
      minHubGap = Math.min(minHubGap, Math.hypot(
        hubNodes[i].x - hubNodes[j].x, hubNodes[i].y - hubNodes[j].y));
    }
  }

  // Level-of-detail sweep. Zooming in shrinks the slice of graph inside the
  // viewport, so the same node budget covers a larger SHARE of what is on
  // screen — that share is the property worth asserting, not the raw drawn
  // count (which falls at high zoom simply because less is in view).
  const lodSweep = [];
  for (const k of [0.25, 0.5, 1, 2, 4, 8]) {
    og.fit();
    og._zoomCenter(k);
    og._draw();
    const info = og.lodInfo();
    const hubsInView = og.nodes.filter(n => n.hub && n._vis !== undefined);
    lodSweep.push({
      k, drawn: info.drawn, inView: info.inView, budget: info.budget,
      total: info.total,
      shown: info.inView ? info.drawn / info.inView : 1,
      // A culled hub would remove a whole cluster's label from the map.
      hubsCulled: og.nodes.filter(n => n.hub && n._inView && !n._vis).length,
    });
  }

  // The selection must survive the budget: hiding what the user just clicked
  // (or what search just jumped to) makes the graph lie about its own result.
  og.fit();
  og._zoomCenter(0.25);
  const leastImportant = og.nodes.filter(n => !n.hub)
    .sort((a, b) => a._imp - b._imp)[0];
  let selectionSurvives = true;
  if (leastImportant) {
    og.select(leastImportant, false);
    og._draw();
    selectionSurvives = leastImportant._vis === true;
  }
  og.select(null, false);

  // Captured here, with real data and a zoomed-out view, because the empty
  // case below wipes the graph — describing that would report zero of
  // everything and quietly pass any assertion about it.
  og.fit();
  og._zoomCenter(0.25);
  og._draw();
  const describeText = og.describe();

  // Empty data must not throw either.
  let emptyOk = true;
  try { og.setData([], []); og._draw(); og.fit(); } catch (_) { emptyOk = false; }

  process.stdout.write(JSON.stringify({
    nodes: nodes.length, links: links.length, settleMs, finite, span,
    agree, total, agreePct: total ? Math.round(100 * agree / total) : 0,
    hubs: hubNodes.length,
    minHubGap: Number.isFinite(minHubGap) ? Math.round(minHubGap) : null,
    emptyOk, describe: og.describe(), describeText,
    lodSweep, selectionSurvives, lodCalls: lodCalls.length,
    lodCallMaxHidden: lodCalls.reduce((m, c) => Math.max(m, c.hidden), 0),
    canvas: { w: CANVAS_W, h: CANVAS_H }, detail: og.detail,
    budgetCurve: [[1200,700],[390,700],[1920,1080],[2560,1400],[3840,2160]]
      .map(([w, h]) => ({ w, h, budget: og.budgetFor(w * h) })),
  }));
});
