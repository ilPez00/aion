/* organic.js — the cockpit's one graph renderer.
 *
 * Every visualisation in the HUD (files, vault, telemetry) is the same thing:
 * nodes pulled together by relationships and pushed apart by crowding, drawn
 * as soft blobs on curved links. One engine, three data adapters.
 *
 * Why hand-rolled instead of d3-force / cytoscape
 * -----------------------------------------------
 * The HUD is an offline-first PWA served over a token-gated LAN socket. A CDN
 * <script> tag breaks the offline boot (the service worker cannot precache a
 * cross-origin opaque response usefully) and leaks the page's shape to a third
 * party on every load. Vendoring a 300KB library to get one layout algorithm
 * is the other bad option. This is ~350 lines and has no supply chain.
 *
 * Performance
 * -----------
 * Repulsion is the expensive force (naively O(n^2)). Nodes are bucketed into a
 * spatial hash sized to the interaction radius, so each node only repels its
 * neighbours — near-linear at the 600-node cap. Layout runs on an alpha that
 * cools to zero and then STOPS: an idle graph burns no CPU, which matters when
 * this is pinned on a phone or a second monitor all day.
 *
 * Accessibility
 * -------------
 * A canvas graph is opaque to assistive tech, so the canvas is a focusable
 * `role="application"` with arrow-key traversal between nodes, and every view
 * that uses it also ships a real <table> twin (see hud.js). The graph is never
 * the only representation of anything.
 */
'use strict';

const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;

/* Shape encodes node kind so hue is never the sole channel (WCAG 1.4.1). */
const KIND_SHAPE = {
  hub: 'ring', dir: 'square', code: 'circle', doc: 'circle', config: 'diamond',
  image: 'triangle', media: 'triangle', data: 'square', archive: 'diamond',
  note: 'circle', metric: 'circle', other: 'circle',
};

/* Level-of-detail budget: how many nodes may be drawn at once.
 *
 * This scales with the viewport, but SUB-LINEARLY in its area. Straight
 * area/constant was the first attempt and it is wrong: it assumes twice the
 * pixels means twice the elements you can parse, so a 2560x1400 monitor got a
 * budget of ~690 and culled almost nothing — the clutter came straight back on
 * exactly the screens big enough to provoke it. A larger display at the same
 * viewing distance mostly buys angular size, not proportionally more things
 * the eye can track, so the count grows on a 0.6 exponent and then stops.
 *
 * Reference point is 1200x700 — a typical HUD pane, not a full screen.
 *
 *     1200x700   ->  100      390x700 (phone)  ->   51
 *     1920x1080  ->  172      2560x1400        ->  240 (at the cap)
 *
 * LOD_MAX_NODES is the real ceiling: past a couple of hundred marks the
 * picture is a texture again no matter how big the monitor is.
 */
const LOD_REF_AREA = 1200 * 700;
const LOD_REF_NODES = 100;
const LOD_AREA_EXP = 0.6;      // double the screen area -> ~1.5x the nodes
const LOD_MIN_NODES = 24;      // a phone in landscape still shows a graph
const LOD_MAX_NODES = 240;     // beyond this the eye is not reading it anyway
const LOD_LABEL_FACTOR = 6;    // a label needs ~6 nodes' worth of room to sit in
const LOD_MARGIN_PX = 80;      // fade in before the centre crosses the edge
const LOD_HYSTERESIS = 1.18;   // deadband so boundary nodes do not blink

/* How much is "the right amount" is a matter of eyesight, screen and taste, so
 * it is a control rather than a constant. Multiplies the computed budget. */
const LOD_DETAIL = { sparse: 0.5, normal: 1, dense: 2, all: Infinity };

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

class OrganicGraph {
  constructor(canvas, opts = {}) {
    this.cv = canvas;
    this.ctx = canvas.getContext('2d', { alpha: false });
    this.nodes = [];
    this.links = [];
    this.onSelect = opts.onSelect || (() => {});
    this.onHover = opts.onHover || (() => {});
    this.onFocusChange = opts.onFocusChange || (() => {});
    // Dragging one node onto another is a gesture, not a layout accident —
    // the Agents view uses it to route a task to an instance.
    this.onDrop = opts.onDrop || (() => {});
    // Fires when the number of deferred nodes changes. Without this the LOD
    // badge never appeared: _lod() called `this.onLod`, which nothing ever
    // assigned, so the graph quietly drew a subset and never said so. The
    // headless harness read lodInfo() directly and so never caught it.
    this.onLod = opts.onLod || (() => {});
    this.selected = null;
    this.hovered = null;
    this.focusIdx = -1;          // keyboard cursor, independent of selection
    this.tx = 0; this.ty = 0; this.scale = 1;
    this.detail = opts.detail || 'normal';
    // Starts at 0, not undefined: `hidden !== this._lodHidden` would
    // otherwise be true on the first paint of every graph and fire a
    // "0 deferred" callback that nothing wants.
    this._lodHidden = 0;
    this.alpha = 0;
    this.tick = 0;
    this.filter = '';
    this._raf = null;
    this._palette = [1, 2, 3, 4, 5, 6, 7, 8].map(i => cssVar(`--c${i}`, '#5ad1ff'));
    this._theme = {
      bg: cssVar('--bg', '#0a0f14'), dim: cssVar('--dim', '#9aabbb'),
      fg: cssVar('--fg', '#dbe6f0'), faint: cssVar('--faint', '#6b7d8d'),
      accent: cssVar('--accent', '#5ad1ff'), border: cssVar('--border', '#25384a'),
    };
    this._bindResize();
    this._bindPointer();
    this._bindKeys();
  }

  /* ── data ──────────────────────────────────────────────────────────── */
  setData(nodes, links) {
    // Preserve positions of nodes that survive a refresh, so re-scanning a
    // directory nudges the layout instead of detonating it.
    const prev = new Map(this.nodes.map(n => [n.id, n]));
    const { width: w, height: h } = this._size();
    this.nodes = nodes.map((n, i) => {
      const old = prev.get(n.id);
      const a = (i / Math.max(nodes.length, 1)) * Math.PI * 2;
      // Seed on a ring, not at the centre: coincident points have an
      // undefined repulsion direction and explode on the first frame.
      const r = n.hub ? 60 : 180 + (i % 7) * 22;
      return {
        ...n,
        x: old ? old.x : w / 2 + Math.cos(a) * r,
        y: old ? old.y : h / 2 + Math.sin(a) * r,
        vx: 0, vy: 0,
        r: n.hub ? 12 + 10 * (n.weight || 0) : 4 + 7 * (n.weight || 0),
        shape: KIND_SHAPE[n.kind] || 'circle',
        color: this._palette[(n.group ?? 0) % this._palette.length],
        phase: (i * 0.7) % (Math.PI * 2),
      };
    });
    const byId = new Map(this.nodes.map(n => [n.id, n]));
    this.links = links
      .map(l => ({ s: byId.get(l.source), t: byId.get(l.target),
                   w: l.weight ?? 0.5, kind: l.kind || 'sim' }))
      .filter(l => l.s && l.t);
    for (const n of this.nodes) { n.deg = 0; n.anchor = null; }
    for (const l of this.links) {
      l.s.deg++; l.t.deg++;
      // Remember each node's strongest hub. Without an explicit anchor the
      // layout only knows "pull toward things you link to", and since hubs
      // all sit near the centre a file drifts to whichever one it happens to
      // land beside — the picture stops matching the clustering.
      if (l.kind !== 'hub') continue;
      const [hub, leaf] = l.s.hub ? [l.s, l.t] : [l.t, l.s];
      if (!hub.hub || leaf.hub) continue;
      if (!leaf.anchor || l.w > leaf.anchorW) { leaf.anchor = hub; leaf.anchorW = l.w; }
    }
    this.hubs = this.nodes.filter(n => n.hub);
    this._rankImportance();
    this.selected = this.selected && byId.get(this.selected.id) || null;
    this.focusIdx = -1;
    this.reheat();
  }

  /* Static per-node importance, used only to decide drawing order under the
     LOD budget. Recomputed when the graph changes, never while panning — a
     rank that moves with the viewport makes nodes strobe. */
  _rankImportance() {
    for (const n of this.nodes) {
      n._imp = (n.hub ? 1000 : 0) + (n.weight || 0) * 10 + (n.deg || 0) * 0.5;
    }
  }

  /* Apply a live update without rebuilding the graph.
   *
   * `setData` re-seeds anything it has not seen, so calling it every time a
   * task ticks would make the layout twitch continuously and throw away the
   * user's pan/zoom context. This mutates the nodes that actually changed,
   * adds/removes the rest, and only warms the simulation slightly — the
   * picture stays where the user put it and just breathes.
   *
   * `pulse` marks nodes whose STATE changed (not merely progress), so the eye
   * is drawn to the meaningful transitions rather than to every increment.
   */
  patch({ update = [], add = [], remove = [], links = null } = {}) {
    const byId = new Map(this.nodes.map(n => [n.id, n]));
    let structural = false;

    for (const u of update) {
      const n = byId.get(u.id);
      if (!n) { add.push(u); continue; }
      const stateChanged = u.group !== undefined && u.group !== n.group;
      Object.assign(n, u);
      n.r = n.hub ? 12 + 10 * (n.weight || 0) : 4 + 7 * (n.weight || 0);
      if (u.group !== undefined) {
        n.color = this._palette[(u.group ?? 0) % this._palette.length];
      }
      if (stateChanged) this._pulse(n);
    }

    if (remove.length) {
      const gone = new Set(remove);
      this.nodes = this.nodes.filter(n => !gone.has(n.id));
      this.links = this.links.filter(l => !gone.has(l.s.id) && !gone.has(l.t.id));
      structural = true;
    }

    if (add.length) {
      const { width: w, height: h } = this._size();
      for (const a of add) {
        if (byId.has(a.id)) continue;
        // New work enters near its anchor if it has one, so a task appears
        // beside its harness instead of flying in from the edge.
        const host = a.near ? byId.get(a.near) : null;
        const node = {
          ...a,
          x: (host ? host.x : w / 2) + (Math.random() - 0.5) * 40,
          y: (host ? host.y : h / 2) + (Math.random() - 0.5) * 40,
          vx: 0, vy: 0,
          r: a.hub ? 12 + 10 * (a.weight || 0) : 4 + 7 * (a.weight || 0),
          shape: KIND_SHAPE[a.kind] || 'circle',
          color: this._palette[(a.group ?? 0) % this._palette.length],
          phase: (this.nodes.length * 0.7) % (Math.PI * 2),
        };
        this.nodes.push(node);
        byId.set(node.id, node);
        this._pulse(node);
      }
      structural = true;
    }

    if (links) {
      this.links = links
        .map(l => ({ s: byId.get(l.source), t: byId.get(l.target),
                     w: l.weight ?? 0.5, kind: l.kind || 'sim' }))
        .filter(l => l.s && l.t);
      structural = true;
    }

    if (structural) {
      for (const n of this.nodes) { n.deg = 0; n.anchor = null; }
      for (const l of this.links) {
        l.s.deg++; l.t.deg++;
        if (l.kind !== 'hub') continue;
        const [hub, leaf] = l.s.hub ? [l.s, l.t] : [l.t, l.s];
        if (!hub.hub || leaf.hub) continue;
        if (!leaf.anchor || l.w > leaf.anchorW) { leaf.anchor = hub; leaf.anchorW = l.w; }
      }
      this.hubs = this.nodes.filter(n => n.hub);
    }
    this._rankImportance();
    // A nudge, not a reheat: enough for new nodes to find room, not enough to
    // rearrange the map under the user's cursor.
    this.alpha = Math.max(this.alpha, structural ? 0.30 : 0.10);
    this._start();
  }

  _pulse(n) {
    this._pulses = this._pulses || new Map();
    this._pulses.set(n.id, this.tick + 90);
    this._start();
  }

  reheat(a = 1) {
    this.alpha = a;
    if (REDUCED) {                 // solve now, present a settled graph
      for (let i = 0; i < 260 && this.alpha > 0.02; i++) this._step();
      this.alpha = 0;
      this.fit();
    }
    this._start();
  }

  setFilter(q) { this.filter = (q || '').toLowerCase().trim(); this._start(); }

  matches(n) {
    return !this.filter ||
      (n.label || '').toLowerCase().includes(this.filter) ||
      (n.path || '').toLowerCase().includes(this.filter);
  }

  /* ── forces ────────────────────────────────────────────────────────── */
  _step() {
    const { width: w, height: h } = this._size();
    const cx = w / 2, cy = h / 2;
    const n = this.nodes.length;
    if (!n) return;
    const a = this.alpha;

    // 1. spring — related things pull together, strength scaled by weight
    for (const l of this.links) {
      const rest = l.kind === 'hub' ? 74 : 46;
      let dx = l.t.x - l.s.x, dy = l.t.y - l.s.y;
      let d = Math.hypot(dx, dy) || 0.01;
      const f = ((d - rest) / d) * 0.055 * a * (0.4 + l.w);
      dx *= f; dy *= f;
      l.s.vx += dx; l.s.vy += dy;
      l.t.vx -= dx; l.t.vy -= dy;
    }

    // 2. repulsion — spatial hash keeps this near-linear. Only pairs inside
    //    one cell (or an adjacent one) can be close enough to matter.
    const CELL = 60, R2 = CELL * CELL;
    const grid = new Map();
    for (const p of this.nodes) {
      const k = `${Math.floor(p.x / CELL)},${Math.floor(p.y / CELL)}`;
      let bucket = grid.get(k);
      if (!bucket) grid.set(k, bucket = []);
      bucket.push(p);
    }
    for (const p of this.nodes) {
      const gx = Math.floor(p.x / CELL), gy = Math.floor(p.y / CELL);
      for (let ox = -1; ox <= 1; ox++) for (let oy = -1; oy <= 1; oy++) {
        const bucket = grid.get(`${gx + ox},${gy + oy}`);
        if (!bucket) continue;
        for (const q of bucket) {
          if (q === p) continue;
          let dx = p.x - q.x, dy = p.y - q.y;
          let d2 = dx * dx + dy * dy;
          if (d2 > R2) continue;
          if (d2 < 0.01) { dx = (p.phase - q.phase) || 0.1; dy = 0.1; d2 = 0.02; }
          const f = (260 * a * (1 + p.r * 0.06)) / d2;
          p.vx += dx * f; p.vy += dy * f;
        }
      }
    }

    // 3. hub separation — hubs are few, so an exact O(h^2) pass is cheap and
    //    worth it: the spatial hash only sees a 60px neighbourhood, but hubs
    //    must push each other apart across the whole canvas. Without this
    //    they collapse into the middle and every cluster overlaps.
    const hubs = this.hubs || [];
    for (let i = 0; i < hubs.length; i++) {
      for (let j = i + 1; j < hubs.length; j++) {
        const p = hubs[i], q = hubs[j];
        let dx = p.x - q.x, dy = p.y - q.y;
        let d = Math.hypot(dx, dy) || 0.01;
        const want = 210;
        if (d >= want) continue;
        const f = ((want - d) / d) * 0.09 * a;
        p.vx += dx * f; p.vy += dy * f;
        q.vx -= dx * f; q.vy -= dy * f;
      }
    }

    // 4. anchoring — a file is pulled toward its OWN hub, not toward the
    //    canvas centre. This is what makes the picture agree with the
    //    clustering: proximity on screen means membership, not luck.
    //
    //    The constant is balanced against force 1 (springs), and force 1 got
    //    much stronger when fsgraph moved to content-led weighting: the
    //    similarity mesh went from ~0.1 to ~2.2 edges per file, and all that
    //    extra pull drags files across cluster boundaries. Agreement fell to
    //    65% at the old 0.11 and the layout gate caught it. 0.22 restores
    //    ~85% across four corpora without collapsing clusters into spokes.
    for (const p of this.nodes) {
      if (!p.anchor) continue;
      const g = 0.220 * a * (0.5 + (p.anchorW || 0.5));
      p.vx += (p.anchor.x - p.x) * g;
      p.vy += (p.anchor.y - p.y) * g;
    }

    // 5. gravity — only enough to keep disconnected islands on screen. Weak
    //    on files (anchoring already placed them) and weak on hubs (hub
    //    separation already spread them).
    for (const p of this.nodes) {
      const g = (p.hub ? 0.010 : 0.0016) * a;
      p.vx += (cx - p.x) * g;
      p.vy += (cy - p.y) * g;
      p.vx *= 0.82; p.vy *= 0.82;          // damping
      if (p.fixed) { p.vx = p.vy = 0; continue; }
      p.x += Math.max(-24, Math.min(24, p.vx));
      p.y += Math.max(-24, Math.min(24, p.vy));
    }

    this.alpha *= 0.982;
    if (this.alpha < 0.004) this.alpha = 0;
  }

  /* ── level of detail ───────────────────────────────────────────────────
   *
   * A 600-node graph drawn all at once is a texture, not a picture. The rule
   * here is CONSTANT SCREEN-SPACE DENSITY: a fixed number of nodes per unit of
   * screen area, regardless of zoom.
   *
   * That is what makes detail zoom-dependent without any tuned zoom curve.
   * Zooming in does not raise a threshold — it shrinks the slice of graph
   * inside the viewport, so fewer nodes compete for the same pixels and more
   * of them clear the budget. Detail appears because there is genuinely room
   * for it, which is exactly the promise the gesture makes.
   *
   * Two things keep it from flickering:
   *   - importance is STATIC per node (weight, degree, hub-ness). Rank by
   *     anything viewport-dependent and nodes strobe as you pan.
   *   - a hysteresis band: a node already drawn keeps its place until it is
   *     well past the budget, so nodes sitting on the boundary do not blink.
   *
   * Hidden nodes stay in the simulation. LOD is a rendering decision, not a
   * layout one — dropping them from the forces would make the whole graph
   * re-settle on every zoom, and spatial memory is most of what makes this
   * navigable. Nothing is hidden from the list view or from search either:
   * the accessible twin stays complete, so LOD never removes information,
   * only defers drawing it.
   */
  _lod() {
    const { width: w, height: h } = this._size();
    const budget = this.budgetFor(w * h);
    const labelBudget = Math.max(6, Math.round(budget / LOD_LABEL_FACTOR));

    // Viewport in world coordinates, with a margin so nodes fade in slightly
    // before their centre crosses the edge rather than popping at it.
    const m = LOD_MARGIN_PX / this.scale;
    const x0 = -this.tx / this.scale - m, y0 = -this.ty / this.scale - m;
    const x1 = (w - this.tx) / this.scale + m, y1 = (h - this.ty) / this.scale + m;

    const sel = this.selected;
    const near = sel ? new Set(this.links.flatMap(
      l => (l.s === sel || l.t === sel) ? [l.s.id, l.t.id] : [])) : null;

    const inView = [];
    for (const p of this.nodes) {
      p._wasVis = p._vis;
      p._vis = false; p._lbl = false; p._inView = false;
      if (p.x < x0 || p.x > x1 || p.y < y0 || p.y > y1) continue;
      p._inView = true;
      inView.push(p);
    }
    // Static importance, so panning never reshuffles a node relative to its
    // peers. Ties break on id to keep the order stable across frames.
    inView.sort((a, b) => (b._imp - a._imp) || (a.id < b.id ? -1 : 1));

    let drawn = 0;
    for (const p of inView) {
      // Things the user is currently working with are never a budget decision:
      // hiding the selection, the keyboard focus or a search hit would make
      // the graph lie about what it just told you it found.
      const forced = p.hub || p === sel || p === this.hovered ||
                     this.nodes[this.focusIdx] === p ||
                     (near && near.has(p.id)) ||
                     (this.filter && this.matches(p));
      const limit = p._wasVis ? budget * LOD_HYSTERESIS : budget;
      if (!forced && drawn >= limit) continue;
      p._vis = true;
      p._lbl = forced || drawn < labelBudget;
      drawn++;
    }
    // Deferred means "on screen, but over budget" — NOT "off screen". An
    // off-viewport node is one pan away and was never hidden from you;
    // counting it here made the number balloon as you zoomed in, which is the
    // exact opposite of what the badge is trying to tell you.
    const hidden = inView.length - drawn;
    this._lodDrawn = drawn;
    this._lodInView = inView.length;
    this._lodBudget = budget;
    this._lodTotal = this.nodes.length;
    if (hidden !== this._lodHidden) {
      this._lodHidden = hidden;
      if (this.onLod) this.onLod(this.lodInfo());
    }
  }

  /* Nodes drawable in `area` px² of viewport, at the current detail setting.
     Separated out so the budget curve is testable without a canvas. */
  budgetFor(area) {
    const mult = LOD_DETAIL[this.detail] ?? 1;
    if (!Number.isFinite(mult)) return Infinity;
    const scaled = LOD_REF_NODES * Math.pow(area / LOD_REF_AREA, LOD_AREA_EXP);
    return Math.max(LOD_MIN_NODES,
                    Math.min(LOD_MAX_NODES * mult, Math.round(scaled * mult)));
  }

  /* sparse | normal | dense | all. The right density depends on the screen,
     the eyes and the graph, so it is the user's call, not a constant. */
  setDetail(name) {
    this.detail = (name in LOD_DETAIL) ? name : 'normal';
    this._start();
  }

  /* What the status line reports, so the graph says when it is holding
     something back rather than quietly appearing smaller than it is. */
  lodInfo() {
    return { drawn: this._lodDrawn || 0, hidden: this._lodHidden || 0,
             total: this._lodTotal || this.nodes.length,
             inView: this._lodInView || 0, budget: this._lodBudget || 0 };
  }

  /* ── render ────────────────────────────────────────────────────────── */
  _draw() {
    const ctx = this.ctx;
    const { width: w, height: h } = this._size();
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.fillStyle = this._theme.bg;
    ctx.fillRect(0, 0, w, h);
    this._lod();
    ctx.save();
    ctx.translate(this.tx, this.ty);
    ctx.scale(this.scale, this.scale);

    const breathe = REDUCED ? 0 : Math.sin(this.tick * 0.03);
    const sel = this.selected;
    const near = sel ? new Set(this.links.flatMap(
      l => (l.s === sel || l.t === sel) ? [l.s.id, l.t.id] : [])) : null;

    // Links first, under everything. Quadratic sag makes the mesh read as
    // organic tissue rather than a circuit diagram, and separates the two
    // link kinds without relying on colour.
    for (const l of this.links) {
      // An edge to a node that is not drawn is a line into nowhere: it reads
      // as a dangling relationship rather than as omitted detail.
      if (!(l.s._vis && l.t._vis)) continue;
      const active = sel && (l.s === sel || l.t === sel);
      const dim = this.filter && !(this.matches(l.s) || this.matches(l.t));
      ctx.globalAlpha = dim ? 0.05 : (active ? 0.85 : 0.24);
      ctx.strokeStyle = active ? this._theme.accent
                              : (l.kind === 'hub' ? l.s.color : this._theme.faint);
      ctx.lineWidth = (active ? 1.6 : 0.7) + l.w * 1.2;
      const mx = (l.s.x + l.t.x) / 2, my = (l.s.y + l.t.y) / 2;
      const nx = -(l.t.y - l.s.y), ny = l.t.x - l.s.x;
      const nl = Math.hypot(nx, ny) || 1;
      const sag = 9 + breathe * 2.5;
      ctx.beginPath();
      ctx.moveTo(l.s.x, l.s.y);
      ctx.quadraticCurveTo(mx + (nx / nl) * sag, my + (ny / nl) * sag, l.t.x, l.t.y);
      ctx.stroke();
    }

    // Nodes. Labels only where they can be read: hubs always, files once
    // zoomed in or when selected/hovered/focused — otherwise a 600-node
    // graph is a wall of overlapping text.
    for (let i = 0; i < this.nodes.length; i++) {
      const p = this.nodes[i];
      if (!p._vis) continue;
      const hit = this.visible(p);
      const isSel = p === sel, isHov = p === this.hovered, isFoc = i === this.focusIdx;
      const linked = near ? near.has(p.id) : false;
      ctx.globalAlpha = hit ? (near && !linked && !isSel ? 0.35 : 1) : 0.07;

      let pulse = p.hub && !REDUCED ? 1 + 0.06 * Math.sin(this.tick * 0.045 + p.phase) : 1;
      // Live change ring: a node whose STATE just moved wears an expanding
      // halo for ~1.5s. Progress ticks alone do not trigger it — otherwise
      // every node would strobe and the signal would carry nothing.
      const pu = this._pulses && this._pulses.get(p.id);
      if (pu) {
        if (this.tick > pu) this._pulses.delete(p.id);
        else {
          const t = 1 - (pu - this.tick) / 90;
          ctx.save();
          ctx.globalAlpha = (1 - t) * 0.85;
          ctx.strokeStyle = p.color;
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.arc(p.x, p.y, p.r + 4 + t * 26, 0, Math.PI * 2);
          ctx.stroke();
          ctx.restore();
          if (REDUCED) this._pulses.delete(p.id);   // no strobing for anyone
        }
      }
      // Landing flash: a palette jump drops you somewhere in a field of
      // hundreds of dots, so the target announces itself for a second.
      if (this._flash && this._flash.node === p) {
        if (this.tick > this._flash.until) this._flash = null;
        else if (!REDUCED) pulse *= 1 + 0.5 * Math.abs(Math.sin(this.tick * 0.2));
        else pulse *= 1.5;
      }
      const r = p.r * pulse;

      // soft halo — the "organic" read comes from this, not from the outline
      const glow = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r * 3.4);
      glow.addColorStop(0, p.color + (p.hub ? '55' : '30'));
      glow.addColorStop(1, p.color + '00');
      ctx.fillStyle = glow;
      ctx.beginPath(); ctx.arc(p.x, p.y, r * 3.4, 0, Math.PI * 2); ctx.fill();

      ctx.fillStyle = p.color;
      ctx.strokeStyle = isSel || isFoc ? this._theme.fg : this._theme.bg;
      ctx.lineWidth = isSel || isFoc ? 2.2 : 1;
      this._shape(p.shape, p.x, p.y, r);
      ctx.fill();
      if (isSel || isFoc || isHov) ctx.stroke();

      if (p._lbl || isSel || isHov || isFoc) {
        ctx.globalAlpha = hit ? 1 : 0.2;
        ctx.font = `${p.hub ? 600 : 400} ${p.hub ? 12 : 10}px ${cssVar('--font', 'monospace')}`;
        ctx.textAlign = 'center';
        ctx.lineWidth = 3;
        ctx.strokeStyle = this._theme.bg;
        const ty = p.y - r - 6;
        ctx.strokeText(p.label, p.x, ty);      // halo keeps text legible on links
        ctx.fillStyle = p.hub ? this._theme.fg : this._theme.dim;
        ctx.fillText(p.label, p.x, ty);
      }
    }
    ctx.restore();
    ctx.globalAlpha = 1;
  }

  _shape(kind, x, y, r) {
    const ctx = this.ctx;
    ctx.beginPath();
    if (kind === 'square') ctx.rect(x - r * 0.8, y - r * 0.8, r * 1.6, r * 1.6);
    else if (kind === 'diamond') {
      ctx.moveTo(x, y - r); ctx.lineTo(x + r, y);
      ctx.lineTo(x, y + r); ctx.lineTo(x - r, y); ctx.closePath();
    } else if (kind === 'triangle') {
      ctx.moveTo(x, y - r); ctx.lineTo(x + r * 0.9, y + r * 0.7);
      ctx.lineTo(x - r * 0.9, y + r * 0.7); ctx.closePath();
    } else if (kind === 'ring') {
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.arc(x, y, r * 0.52, 0, Math.PI * 2, true);
    } else ctx.arc(x, y, r, 0, Math.PI * 2);
  }

  /* ── loop ──────────────────────────────────────────────────────────── */
  _start() {
    if (this._raf) return;
    const loop = () => {
      this.tick++;
      if (this.alpha > 0) this._step();
      this._draw();
      // Keep animating while the layout is hot, something is hovered, or the
      // breathing is visible. Otherwise park — an idle HUD must cost nothing.
      const busy = this.alpha > 0 || this.hovered || this._flash ||
                   (this._pulses && this._pulses.size) ||
                   (!REDUCED && this.selected);
      if (busy) { this._raf = requestAnimationFrame(loop); }
      else { this._raf = null; }
    };
    this._raf = requestAnimationFrame(loop);
  }

  /* ── viewport ──────────────────────────────────────────────────────── */
  _size() {
    return { width: this.cv.clientWidth || 1, height: this.cv.clientHeight || 1 };
  }

  _bindResize() {
    const fit = () => {
      this.dpr = Math.min(devicePixelRatio || 1, 2);
      const { width, height } = this._size();
      this.cv.width = Math.round(width * this.dpr);
      this.cv.height = Math.round(height * this.dpr);
      this._start();
    };
    fit();
    new ResizeObserver(fit).observe(this.cv);
  }

  fit(pad = 60) {
    if (!this.nodes.length) return;
    const xs = this.nodes.map(n => n.x), ys = this.nodes.map(n => n.y);
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const y0 = Math.min(...ys), y1 = Math.max(...ys);
    const { width: w, height: h } = this._size();
    this.scale = Math.max(0.15, Math.min(2.5,
      Math.min((w - pad * 2) / Math.max(x1 - x0, 1), (h - pad * 2) / Math.max(y1 - y0, 1))));
    this.tx = w / 2 - ((x0 + x1) / 2) * this.scale;
    this.ty = h / 2 - ((y0 + y1) / 2) * this.scale;
    this._start();
  }

  _toWorld(cx, cy) {
    const rect = this.cv.getBoundingClientRect();
    return { x: (cx - rect.left - this.tx) / this.scale,
             y: (cy - rect.top - this.ty) / this.scale };
  }

  nodeAt(cx, cy, exclude = null) {
    const { x, y } = this._toWorld(cx, cy);
    let best = null, bd = Infinity;
    for (const n of this.nodes) {
      // Only what is actually drawn is clickable. Picking a culled node would
      // select something the user cannot see and never aimed at. Keyboard
      // traversal deliberately still reaches everything — selecting a node
      // forces it visible, so the keyboard is never limited by the budget.
      if (n === exclude || n._vis === false || !this.visible(n)) continue;
      const d = Math.hypot(n.x - x, n.y - y);
      // generous slop so a 4px file node is still tappable on a phone
      if (d < Math.max(n.r + 10 / this.scale, 14 / this.scale) && d < bd) { bd = d; best = n; }
    }
    return best;
  }

  select(n, notify = true) {
    if (n !== this.selected) this._nbIdx = -1;   // restart neighbour walk
    this.selected = n;
    this.focusIdx = n ? this.nodes.indexOf(n) : -1;
    if (notify) this.onSelect(n);
    this._start();
  }

  /* ── pointer ───────────────────────────────────────────────────────── */
  _bindPointer() {
    const cv = this.cv;
    let drag = null, pan = null, moved = false;
    const pointers = new Map();
    let pinch = 0;

    cv.addEventListener('pointerdown', e => {
      cv.setPointerCapture(e.pointerId);
      pointers.set(e.pointerId, e);
      moved = false;
      if (pointers.size === 2) { pinch = this._pinchDist(pointers); drag = pan = null; return; }
      const hit = this.nodeAt(e.clientX, e.clientY);
      if (hit) { drag = hit; hit.fixed = true; }
      else pan = { x: e.clientX - this.tx, y: e.clientY - this.ty };
    });

    cv.addEventListener('pointermove', e => {
      if (pointers.has(e.pointerId)) pointers.set(e.pointerId, e);
      if (pointers.size === 2) {
        const d = this._pinchDist(pointers);
        if (pinch) this._zoomAt(d / pinch, ...this._pinchCenter(pointers));
        pinch = d;
        return;
      }
      moved = true;
      if (drag) {
        const w = this._toWorld(e.clientX, e.clientY);
        drag.x = w.x; drag.y = w.y; drag.vx = drag.vy = 0;
        this.alpha = Math.max(this.alpha, 0.28);
      } else if (pan) {
        this.tx = e.clientX - pan.x; this.ty = e.clientY - pan.y;
      } else {
        const hit = this.nodeAt(e.clientX, e.clientY);
        if (hit !== this.hovered) { this.hovered = hit; this.onHover(hit); }
        cv.style.cursor = hit ? 'pointer' : 'grab';
      }
      this._start();
    });

    const end = e => {
      pointers.delete(e.pointerId);
      if (pointers.size < 2) pinch = 0;
      if (drag) {
        drag.fixed = false;
        if (!moved) {
          this.select(drag);
        } else {
          // Dropped on top of something? Offer it as a gesture. The graph
          // does not act on it — hud.js decides whether the pair means
          // anything, so the renderer stays free of domain rules.
          const onto = this.nodeAt(e.clientX, e.clientY, drag);
          if (onto) this.onDrop(drag, onto);
        }
        drag = null;
      }
      else if (pan && !moved) this.select(null);
      pan = null;
      this._start();
    };
    cv.addEventListener('pointerup', end);
    cv.addEventListener('pointercancel', end);

    cv.addEventListener('wheel', e => {
      e.preventDefault();
      this._zoomAt(Math.exp(-e.deltaY * 0.0016), e.clientX, e.clientY);
    }, { passive: false });
  }

  _pinchDist(m) { const [a, b] = [...m.values()]; return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY); }
  _pinchCenter(m) { const [a, b] = [...m.values()]; return [(a.clientX + b.clientX) / 2, (a.clientY + b.clientY) / 2]; }

  _zoomAt(k, cx, cy) {
    const rect = this.cv.getBoundingClientRect();
    const px = cx - rect.left, py = cy - rect.top;
    const next = Math.max(0.12, Math.min(5, this.scale * k));
    const ratio = next / this.scale;
    this.tx = px - (px - this.tx) * ratio;
    this.ty = py - (py - this.ty) * ratio;
    this.scale = next;
    this._start();
  }

  /* ── keyboard ──────────────────────────────────────────────────────── */
  /* Nearest visible node in a compass direction from `from`.
   *
   * Scored by distance penalised by angular error, so "right" reaches the
   * node to the right rather than the closest node that happens to be a
   * degree off. Index-order traversal (the obvious implementation) is
   * useless on a force layout, where array order has no spatial meaning. */
  _directional(from, dx, dy) {
    let best = null, bestScore = Infinity;
    for (const n of this.nodes) {
      if (n === from || !this.matches(n)) continue;
      if (!this.visible(n)) continue;
      const vx = n.x - from.x, vy = n.y - from.y;
      const d = Math.hypot(vx, vy);
      if (d < 1) continue;
      const cos = (vx * dx + vy * dy) / d;
      if (cos < 0.35) continue;                 // outside a ~70 degree cone
      const score = d / (cos * cos);
      if (score < bestScore) { bestScore = score; best = n; }
    }
    return best;
  }

  /* Neighbours of the selection, ordered — lets you walk the actual graph
   * (a hub to its members, a file to its similar files) rather than the
   * plane. This is the traversal that matches how the data is structured. */
  neighbours(n) {
    if (!n) return [];
    const out = [];
    for (const l of this.links) {
      if (l.s === n) out.push(l.t);
      else if (l.t === n) out.push(l.s);
    }
    return out.filter(x => this.matches(x));
  }

  cycleNeighbour(dir = 1) {
    const cur = this.selected;
    const list = this.neighbours(cur);
    if (!list.length) return;
    this._nbIdx = ((this._nbIdx ?? -1) + dir + list.length) % list.length;
    const next = list[this._nbIdx];
    this.select(next);
    this.centerOn(next);
  }

  /* Isolate one node and everything it connects to. The single most useful
   * move on a dense graph: the rest drops out so the cluster can be read.
   *
   * Deliberately structural (hub + its linked members) rather than keyed on
   * the `group` field. `group` means "which colour band", and that is only
   * the same thing as "which cluster" in the Files view. In Agents, group
   * encodes task STATE, so grouping by it isolated "all the running things"
   * — 2 nodes out of 23 — instead of "this harness and its tasks". Topology
   * is what the user is pointing at; colour is just how it is drawn. */
  focusOn(node) {
    if (!node) return [];
    const keep = new Set([node.id]);
    for (const n of this.neighbours(node)) keep.add(n.id);
    this._focusSet = keep;
    this._focusAnchor = node;
    this._start();
    return this.nodes.filter(n => keep.has(n.id));
  }

  clearFocus() { this._focusSet = null; this._focusAnchor = null; this._start(); }

  get focused() { return !!this._focusSet; }

  visible(n) {
    if (!this.matches(n)) return false;
    return !this._focusSet || this._focusSet.has(n.id);
  }

  nodeById(id) { return this.nodes.find(n => n.id === id) || null; }

  centerOn(n, zoom = null) {
    if (!n) return;
    const { width: w, height: h } = this._size();
    if (zoom) this.scale = Math.max(0.12, Math.min(5, zoom));
    this.tx = w / 2 - n.x * this.scale;
    this.ty = h / 2 - n.y * this.scale;
    this._start();
  }

  /* Bring a node into view and select it — the landing move for a palette
   * jump or a click in the list twin. */
  reveal(id) {
    const n = typeof id === 'string' ? this.nodeById(id) : id;
    if (!n) return false;
    this.select(n);
    this.centerOn(n, Math.max(this.scale, 1.5));
    this._flash = { node: n, until: this.tick + 60 };
    this._start();
    return true;
  }

  _bindKeys() {
    this.cv.addEventListener('keydown', e => {
      if (!this.nodes.length) return;
      const dirs = {
        ArrowRight: [1, 0], ArrowLeft: [-1, 0],
        ArrowDown: [0, 1], ArrowUp: [0, -1],
      };
      if (dirs[e.key]) {
        e.preventDefault();
        const from = this.selected || this.nodes[0];
        const next = this.selected ? this._directional(from, ...dirs[e.key]) : from;
        if (next) { this.select(next); this.centerOn(next); }
        return;
      }
      if (e.key === 'Tab') {                    // walk the graph, not the plane
        e.preventDefault();
        this.cycleNeighbour(e.shiftKey ? -1 : 1);
      } else if (e.key === 'Home') {
        e.preventDefault();
        const hub = this.hubs && this.hubs[0] || this.nodes[0];
        this.select(hub); this.centerOn(hub);
      } else if (e.key === 'Enter' && this.selected) {
        e.preventDefault();
        // Enter isolates the selection and its links; Enter again releases.
        if (this._focusAnchor === this.selected) this.clearFocus();
        else this.focusOn(this.selected);
        this.onFocusChange(this.focused ? this.selected : null);
      } else if (e.key === 'Escape') {
        if (this._focusSet) { this.clearFocus(); this.onFocusChange(null); }
        else this.select(null);
      } else if (e.key === '+' || e.key === '=') { e.preventDefault(); this._zoomCenter(1.2); }
      else if (e.key === '-') { e.preventDefault(); this._zoomCenter(1 / 1.2); }
      else if (e.key === '0') { e.preventDefault(); this.clearFocus(); this.onFocusChange(null); this.fit(); }
    });
  }

  _zoomCenter(k) {
    const r = this.cv.getBoundingClientRect();
    this._zoomAt(k, r.left + r.width / 2, r.top + r.height / 2);
  }

  /* Public: zoom about the canvas centre. The LOD badge uses this to make
     "zoom in to reveal" an actual button rather than an instruction. */
  zoomBy(k) { this._zoomCenter(k); }

  /* Text description of the current graph, announced to screen readers so the
     canvas is not a silent region. */
  describe() {
    const hubs = this.nodes.filter(n => n.hub).length;
    const focus = this._focusSet ? ' Isolated to one cluster; Escape to release.' : '';
    // Say when drawing is being held back, and say that it is only drawing.
    // A screen reader user must not be told the graph has fewer nodes than it
    // does, and the list view still lists every one of them.
    const lod = this._lodHidden > 0
      ? ` ${this._lodHidden} of the ${this._lodInView} nodes on screen are not ` +
        `drawn, to keep the view legible; zoom in to reveal them, or use the ` +
        `list view, which always lists every node.`
      : '';
    return `${this.nodes.length} nodes in ${hubs} clusters, ${this.links.length} links. ` +
           `Arrow keys move between nodes, Tab walks linked neighbours, ` +
           `Enter on a cluster isolates it.${focus}${lod}`;
  }
}

window.OrganicGraph = OrganicGraph;
window.GRAPH_REDUCED_MOTION = REDUCED;
