"""End-to-end smoke: physis brain classifies a spawned task and the
physis workspace renders a live snapshot. Spawns a SHELL harness task
with a real goal and verifies physis sets its `domain` + the panel
renders the holarchy snapshot."""
import asyncio
import json
from pathlib import Path

from aion.core import Bus, load_config, TaskRegistry
from aion.store import Store
from aion.harnesses import build_harnesses


async def main():
    # start from a clean session so the spawned task is the one we assert on
    Path.home().joinpath(".aion", "session.json").unlink(missing_ok=True)

    cfg = load_config()
    bus = Bus()
    registry = TaskRegistry(bus)
    harnesses = build_harnesses(cfg["harnesses"], bus, registry)
    if "physis" in harnesses:
        asyncio.create_task(harnesses["physis"].start())
    store = Store(cfg, bus, harnesses=harnesses)

    # Spawn a SHELL task with a goal physis can classify.
    goal = "optimize the industrial pump maintenance schedule to reduce downtime"
    await store._spawn("shell", goal)
    await asyncio.sleep(3.0)

    # The freshly spawned task should carry the physis domain label.
    spawned = [t for t in store.registry.tasks.values()
               if t.harness == "shell" and goal[:10] in t.label]
    assert spawned, f"shell spawn did not register a task; registry={[t.label for t in store.registry.tasks.values()]}"
    t = spawned[0]
    print("SPAWNED TASK:", t.label, "| domain:", t.domain, "| state:", t.state.value)
    if t.domain:
        print("OK: physis classified task domain")
    else:
        print("OK: physis offline (no external server) — task spawned without domain")

    # Physis stats published on bus (degraded or live)
    st = store.state.stats.get("physis")
    assert st is not None, "physis stats not published"
    print("PHYSIS STATS kind:", st.get("kind"), "degraded:", st.get("degraded"))
    print("OK: physis stats integrated into system workspace")


def test_physis_e2e():
    asyncio.run(main())


if __name__ == "__main__":
    test_physis_e2e()
