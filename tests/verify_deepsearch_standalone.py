"""Standalone verify of the DeepSearch harness + store wiring (no pytest needed;
the host's pytest is broken by a pydantic/typing_inspection conflict unrelated to aion)."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from aion.core import Bus, Intent, IntentType
from aion.store import Store
from aion.harnesses import build_harnesses, WebHarness
from aion.core import load_config


async def main():
    cfg = load_config()
    # ensure the web harness is present in config for this test
    if not any(h.get("id") == "web" for h in cfg["harnesses"]):
        cfg["harnesses"].append({"id": "web", "type": "web", "name": "DeepSearch",
                                  "enabled": True, "vram_mb": 0, "tier": "cheap"})
    bus = Bus()
    store = Store(cfg, bus)
    harnesses = build_harnesses(cfg["harnesses"], bus, store.registry, store.store)
    store.harnesses = harnesses

    assert "web" in harnesses, "WebHarness not built"
    print("OK web harness built:", type(harnesses["web"]).__name__)

    # dispatch a search command through the store (the real path)
    store.handle(Intent.command("search latest stable tauri version"))
    # wait for the async task to run + web call to return
    for _ in range(60):
        await asyncio.sleep(0.5)
        web = next((x for x in store.registry.tasks.values() if x.harness == "web"), None)
        if web and web.state.value in ("done", "failed"):
            break

    t = next((x for x in store.registry.tasks.values() if x.harness == "web"), None)
    assert t is not None, "no web task created"
    print("task state:", t.state.value, "| progress:", round(t.progress, 2))
    print("--- logs ---")
    for line in t.log[-8:]:
        print("  ", line[:90])
    assert t.state.value == "done", f"expected done, got {t.state.value}"
    assert any("answer:" in l for l in t.log), "no answer logged"
    print("\nDEEPSEARCH HARNESS VERIFIED: search -> web -> LLM -> cited answer in Tasks view")


asyncio.run(main())
