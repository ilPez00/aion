"""mesh_panel.py — render the RandoMesh workspace (monitor + package control).

Pure rendering: takes the already-collected snapshot (meshmon + meshsrv, via
the app's background collector) and returns Rich markup. No network, no
filesystem — called on render ticks, so it must never block.

`focus` is the service name the app has selected; the row gets a ▌ marker and
the verb keys act on it. `pending` is the armed install/disable sentence.
Control itself lives in app._handle_mesh_command / meshsrv; this is render-only.
"""

from __future__ import annotations

from typing import Any

WIDTH = 54


def _glyph(reachable: bool, load1: float) -> str:
    if not reachable:
        return "○"  # down
    if load1 > 2.0:
        return "◆"  # hot
    if load1 > 0.5:
        return "●"  # live, working
    return "·"  # idle


def _bar(pct: int, theme: dict) -> str:
    filled = max(0, min(10, round(pct / 10)))
    color = theme.get("ok", "#7CFFB2") if pct < 85 else (
        theme.get("warn", "#FFD479") if pct < 95 else theme.get("err", "#FF8A8A"))
    return f"[{color}]{'█' * filled}{'░' * (10 - filled)}[{theme.get('faint', '#6b7d8d')}][/]"


def render_mesh(data: dict[str, Any], theme: dict, focus: str = "",
                pending: str = "", age: str = "") -> str:
    # the collector nests the node snapshot under "mesh"; tolerate being handed
    # either shape so callers from the dashboard path keep rendering too.
    m = data.get("mesh") if isinstance(data.get("mesh"), dict) else data
    nodes = m.get("nodes", [])
    total = m.get("total", len(nodes))
    reachable = m.get("reachable", 0)
    out: list[str] = []

    ts = f"  [{theme.get('faint', '#6b7d8d')}]{age}[/]" if age else ""
    title = f"[b {theme.get('accent', '#5ad1ff')}]⏣ RandoMesh[/]  " \
            f"[{theme.get('dim', '#9aabbb')}]{reachable}/{total} nodes up[/]{ts}"
    out.append(title)
    out.append("")

    if not nodes:
        out.append(f"[{theme.get('faint', '#6b7d8d')}]no mesh data[/]")

    # Sort: reachable first, then by load descending (hot nodes to the top).
    order = sorted(nodes, key=lambda n: (not n.get("reachable", False), -n.get("load1", 0.0)))
    for n in order:
        name = n.get("name", "?")
        role = n.get("role", "")
        g = _glyph(n.get("reachable", False), n.get("load1", 0.0))
        if not n.get("reachable", False):
            out.append(f"  [{theme.get('faint', '#6b7d8d')}]{g} {name}  [{theme.get('err', '#FF8A8A')}]DOWN[/] — {n.get('note', '')}[/]")
            continue
        ram = n.get("ram_pct", 0)
        disk = n.get("disk_pct", 0)
        load = n.get("load1", 0.0)
        line = (f"  {g} [{theme.get('fg', '#dbe6f0')}]{name}[/] "
                f"[{theme.get('dim', '#9aabbb')}]{role}[/]\n"
                f"     load {load:.2f}  ram {_bar(ram, theme)} {ram}%  "
                f"disk {_bar(disk, theme)} {disk}%")
        out.append(line)

    # pansa storage block (folded in from nas backend)
    storage = (m.get("storage") or data.get("storage")) or {}
    if storage.get("reachable"):
        out.append("")
        out.append(f"[{theme.get('dim', '#9aabbb')}]storage (pansa):[/]")
        for sh in storage.get("shares", []):
            used = sh.get("used_pct", 0)
            out.append(f"  {sh.get('name', '?')} {_bar(used, theme)} {sh.get('used_gb', 0)}/{sh.get('total_gb', 0)}G ({used}%)")

    # Phase 2: mesh services + fleet packages (CONFIG.md-declared lifecycle)
    services = data.get("services") or {}
    svc_list = services.get("services", []) if isinstance(services, dict) else []
    if svc_list:
        out.append("")
        up = services.get("up", sum(1 for s in svc_list if s.get("running")))
        out.append(f"[{theme.get('dim', '#9aabbb')}]services {up}/{len(svc_list)}[/]")
        for s in svc_list:
            name = s.get("name", "?")
            host = s.get("host", "")
            mark = "▌" if name == focus else " "
            kind = "" if s.get("kind", "service") == "service" \
                else f" [{theme.get('accent', '#5ad1ff')}]{s['kind']}[/]"
            if s.get("running"):
                out.append(f"  {mark}[{theme.get('ok', '#7CFFB2')}]●[/] "
                           f"[{theme.get('fg', '#dbe6f0')}]{name}[/] "
                           f"[{theme.get('dim', '#9aabbb')}]{host}:{s.get('probe_value', '')}[/]{kind}")
            else:
                out.append(f"  {mark}[{theme.get('faint', '#6b7d8d')}]○[/] "
                           f"[{theme.get('fg', '#dbe6f0')}]{name}[/] "
                           f"[{theme.get('dim', '#9aabbb')}]{host}:{s.get('probe_value', '')} "
                           f"{s.get('detail', 'down')}[/]{kind}")
        out.append(f"[{theme.get('faint', '#6b7d8d')}]j/k select · s start · "
                   f"x stop · r restart · e enable · d disable · i install[/]")
        if pending:
            out.append(f"[{theme.get('warn', '#FFD479')}]  ⚠ armed: {pending}[/]")

    # Phase 3: aggregated agent sessions / memories / docs (mesh agg collection)
    agg = data.get("agg") or {}
    if agg:
        out.append("")
        out.append(f"[{theme.get('accent', '#5ad1ff')}]▤ agent aggregate (mesh agg)[/]")
        if agg.get("exists"):
            items = agg.get("items", 0)
            by_node = agg.get("by_node") or {}
            by_kind = agg.get("by_kind") or {}
            node_line = "  ".join(f"{n}:{c}" for n, c in by_node.items()) or "—"
            out.append(f"  [{theme.get('fg', '#dbe6f0')}]{items} items[/]  "
                       f"[{theme.get('dim', '#9aabbb')}]{node_line}[/]")
            kind_line = "  ".join(f"{k}:{c}" for k, c in by_kind.items()) or ""
            if kind_line:
                out.append(f"  [{theme.get('dim', '#9aabbb')}]{kind_line}[/]")
            if agg.get("recent"):
                out.append(f"[{theme.get('faint', '#6b7d8d')}]  ↳ filter by `mesh agg status|search`[/]")
        else:
            out.append(f"  [{theme.get('warn', '#FFD479')}]not collected[/]  "
                       f"[{theme.get('faint', '#6b7d8d')}]run: aion mesh agg collect[/]")

    # Phase 4: model serving inventory (what's where, size, vram fit)
    st = data.get("agg") or {}
    if st.get("exists") and st.get("model_total"):
        gb = st.get("model_gb", 0.0)
        out.append("")
        out.append(f"[{theme.get('accent', '#5ad1ff')}]▧ model serving[/] "
                   f"[{theme.get('fg', '#dbe6f0')}]{st['model_total']} files "
                   f"{gb:.1f}GB[/]")
        for entry in (st.get("model_by_host") or [])[:6]:
            out.append(
                f"[{theme.get('dim', '#9aabbb')}]{entry.get('node','?'):8}[/] "
                f"[{theme.get('fg', '#dbe6f0')}]{entry.get('name','')[:24]:24}[/] "
                f"[{theme.get('ok' if entry.get('gpu') else 'dim', '#7CFFB2')}]{entry.get('gb',0):5.1f}GB"
                f"[{theme.get('dim', '#9aabbb')}] "
                f"{entry.get('hint','?')} → {entry.get('path','')}[/]")
        if st.get("model_total", 0) > 6:
            out.append(f"[{theme.get('faint', '#6b7d8d')}]  ↳ "
                       f"{st['model_total']-6} more (aion mesh agg search <term>)[/]")

    # Fleet-manager sections (meshmon.snapshot_sections — facts-backed).
    # machines/services render above via the legacy views because the verb-key
    # selection logic binds to them; these three are additive read-only views
    # that appear only when the facts layer answered.
    sec = data.get("sections") or {}
    if sec.get("source") == "facts":
        programs = sec.get("programs") or []
        if programs:
            out.append("")
            out.append(f"[{theme.get('accent', '#5ad1ff')}]▧ programs[/]")
            for p in programs:
                pk = p.get("packages") or {}
                models = p.get("ollama_models") or []
                extra = ""
                if models:
                    tail = f" +{len(models) - 1}" if len(models) > 1 else ""
                    extra = (f"  [{theme.get('faint', '#6b7d8d')}]"
                             f"{models[0]}{tail}[/]")
                out.append(f"  [{theme.get('fg', '#dbe6f0')}]{p.get('name', '?')}[/] "
                           f"[{theme.get('dim', '#9aabbb')}]"
                           f"{pk.get('manager', '?')}:{pk.get('explicit', '?')} pkgs · "
                           f"ollama {len(models)} · llama {len(p.get('llama_builds') or [])}[/]"
                           f"{extra}")
        configs = sec.get("configs") or []
        if configs:
            out.append("")
            out.append(f"[{theme.get('accent', '#5ad1ff')}]▧ configs[/]")
            for c in configs:
                dirty = c.get("randomesh_dirty")
                if dirty:
                    mark = (f"[{theme.get('warn', '#FFD479')}]"
                            f"{dirty} dirty[/]")
                else:
                    mark = f"[{theme.get('ok', '#7CFFB2')}]clean[/]"
                out.append(f"  [{theme.get('fg', '#dbe6f0')}]{c.get('name', '?')}[/] "
                           f"[{theme.get('dim', '#9aabbb')}]randomesh[/] {mark} "
                           f"[{theme.get('dim', '#9aabbb')}]· aion[/] "
                           f"{'✓' if c.get('aion_clone') else '✗'} "
                           f"[{theme.get('dim', '#9aabbb')}]· FLEET.md[/] "
                           f"{'✓' if c.get('fleet_md') else '✗'}")
        network = sec.get("network") or []
        if network:
            out.append("")
            out.append(f"[{theme.get('accent', '#5ad1ff')}]▧ network[/]")
            for n in network:
                peers = ", ".join(n.get("peers_online") or []) or "—"
                out.append(f"  [{theme.get('fg', '#dbe6f0')}]{n.get('name', '?')}[/] "
                           f"[{theme.get('dim', '#9aabbb')}]ts {n.get('self_ip', '?')} · "
                           f"peers up: {peers} · "
                           f"ports {len(n.get('listening_ports') or [])}[/]")

    return "\n".join(out)
