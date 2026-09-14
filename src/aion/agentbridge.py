"""agentbridge.py — one shared agentic history across agents and machines.

Speaks the EthanSK/agent-bridge on-disk protocol natively, stdlib only, so
the cockpit interoperates with real agent-bridge installs (same files) and
stands alone where none is installed:

- inbox: ``~/.agent-bridge/inbox/<target>/<uuid>.json`` (BridgeMessage —
  id/from/to/type/content/timestamp/replyTo/ttl/target/fromTarget)
- learnings: ``~/.agent-bridge/shared-context/learnings.ndjson``
  (LearningEntry — id/ts/machine/harness/title/body/tags/scope/v)
- ack: move to ``.archive/<target>/`` + append the id to ``.processed``
  (their watcher's consumed ledger — prevents re-delivery loops)
- sync: append-only union by id over SSH. Never rewrites, never deletes, so
  concurrent appends on either side cannot clobber each other.

Interop rules: entries always carry uuid ids (pushes/syncs idempotent);
remote mutations are appends piped over ``ssh`` (BatchMode, timeouts) — this
machine never rewrites a peer's files. Searches are pure local reads.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import socket
import subprocess
import uuid
from pathlib import Path

LEARNINGS_REL = Path("shared-context/learnings.ndjson")
DEFAULT_TTL = 86400  # 1 day, 0 = no expiry (matches upstream default)

_SEGMENT = re.compile(r"^[\w](?:[\w.\-]*[\w])?$", re.UNICODE)


def home() -> Path:
    """Bridge root. Overridable for tests via AGENT_BRIDGE_HOME."""
    return Path(os.environ.get("AGENT_BRIDGE_HOME",
                               str(Path.home() / ".agent-bridge")))


def local_machine() -> str:
    return socket.gethostname()


def utcnow() -> str:
    """UTC, ISO 8601, microsecond resolution.

    The microseconds are load-bearing, not decoration. read_inbox promises
    "oldest first" and sorts on this field; truncating to whole seconds gave
    two messages sent in the same second identical timestamps. The sort then
    fell through to st_mtime_ns — which the kernel reports identically for
    writes that close together — and finally to the message id, a uuid4. Two
    messages a microsecond apart came back in random order.
    """
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat().replace("+00:00", "Z"))


def valid_target(target: str) -> bool:
    """Mirror upstream isValidTarget: no traversal, no empties, sane chars."""
    if not target or not isinstance(target, str) or len(target) > 256:
        return False
    if ".." in target or "//" in target:
        return False
    if target.startswith("/") or target.endswith("/"):
        return False
    return all(bool(_SEGMENT.match(seg)) for seg in target.split("/"))


def _parse_ts(ts: str) -> float:
    try:
        return datetime.datetime.fromisoformat(
            ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _expired(msg: dict, now: float) -> bool:
    ttl = msg.get("ttl", DEFAULT_TTL)
    try:
        ttl = int(ttl)
    except (TypeError, ValueError):
        return False
    if not ttl:
        return False
    return _parse_ts(msg.get("timestamp", "")) + ttl < now


# ── inbox ─────────────────────────────────────────────────────────────────

def send_local(target: str, content: str, from_: str,
               type: str = "message", to: str = "",
               ttl: int = DEFAULT_TTL, reply_to: str | None = None,
               from_target: str = "aion",
               root: Path | None = None) -> Path:
    """Queue one message in a local inbox. Returns the file written."""
    if not valid_target(target):
        raise ValueError(f"invalid bridge target {target!r}")
    if type not in ("message", "command", "response", "reply"):
        raise ValueError(f"invalid bridge type {type!r}")
    root = root or home()
    msg = {"id": str(uuid.uuid4()), "from": from_, "to": to or target,
           "type": type, "content": content, "timestamp": utcnow(),
           "replyTo": reply_to, "ttl": ttl, "target": target,
           "fromTarget": from_target}
    d = root / "inbox" / target
    d.mkdir(parents=True, exist_ok=True)
    p = d / (msg["id"] + ".json")
    p.write_text(json.dumps(msg, indent=2))
    return p


def read_inbox(target: str, include_archived: bool = False,
               root: Path | None = None) -> list[dict]:
    """Pending messages, oldest first. Skips malformed/TTL-expired files —
    never raises on a corrupt inbox (absence of a signal is not a signal)."""
    root = root or home()
    dirs = [root / "inbox" / target]
    if include_archived:
        dirs.append(root / "inbox" / ".archive" / target)
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    out: list[dict] = []
    for d in dirs:
        try:
            files = sorted(d.iterdir())
        except OSError:
            continue
        for f in files:
            if not f.is_file() or not f.name.endswith(".json"):
                continue
            try:
                m = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(m, dict) or not m.get("id"):
                continue
            if _expired(m, now):
                continue
            try:
                m["_mtime_ns"] = f.stat().st_mtime_ns
            except OSError:
                m["_mtime_ns"] = 0
            out.append(m)
    out.sort(key=lambda m: (_parse_ts(m.get("timestamp", "")),
                            m.pop("_mtime_ns", 0), m.get("id", "")))
    return out


def ack(target: str, msg_id: str, root: Path | None = None) -> bool:
    """Consume one message: move to .archive/<target>/ + ledger the id in
    .processed (upstream's own consumed ledger — stops re-delivery)."""
    root = root or home()
    src = root / "inbox" / target / (msg_id + ".json")
    if not src.is_file():
        return False
    dest_dir = root / "inbox" / ".archive" / target
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        src.rename(dest_dir / src.name)
        with open(root / "inbox" / ".processed", "a") as fh:
            fh.write(msg_id + "\n")
    except OSError:
        return False
    return True


# ── learnings (shared agentic history) ────────────────────────────────────

def learnings_file(root: Path | None = None) -> Path:
    return (root or home()) / LEARNINGS_REL


def _norm_tags(tags) -> list[str]:
    seen: list[str] = []
    for t in tags or []:
        t = str(t).strip().lower()
        if t and t not in seen:
            seen.append(t)
    return seen


def add_learning(title: str, body: str, tags=(),
                 harness: str = "aion", machine: str | None = None,
                 root: Path | None = None) -> dict:
    """Append one entry locally. Returns the entry (uuid id = dedupe key)."""
    title, body = title.strip(), body.strip()
    if not title or not body:
        raise ValueError("learning needs a title and a body")
    entry = {"id": str(uuid.uuid4()), "ts": utcnow(),
             "machine": machine or local_machine(),
             "harness": (harness or "unknown").strip() or "unknown",
             "title": title, "body": body, "tags": _norm_tags(tags),
             "scope": "global", "v": 1}
    lf = learnings_file(root)
    lf.parent.mkdir(parents=True, exist_ok=True)
    with open(lf, "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def iter_learnings(root: Path | None = None):
    """Yield valid entries, file order. Malformed lines are skipped, and
    entries missing id/title are not history — they are junk."""
    try:
        fh = open(learnings_file(root))
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(e, dict) and e.get("id") and e.get("title"):
                yield e


def search_learnings(query: str = "", tag: str | None = None, limit: int = 20,
                     root: Path | None = None) -> list[dict]:
    """Local substring search over title+body+tags (case-insensitive),
    optional exact tag filter. Most recent last-wins: file order, tail cut."""
    q = query.strip().lower()
    tag = (tag or "").strip().lower()
    hits = []
    for e in iter_learnings(root):
        if tag and tag not in [str(t).lower() for t in e.get("tags", [])]:
            continue
        if q:
            hay = " ".join([str(e.get("title", "")), str(e.get("body", "")),
                            " ".join(str(t) for t in e.get("tags", []))]).lower()
            if q not in hay:
                continue
        hits.append(e)
    return hits[-limit:] if limit else hits


def merge_missing(root: Path | None = None,
                  lines: list[str] | None = None) -> list[str]:
    """Lines from elsewhere that are absent locally (by id). Pure merge core:
    callers append the result — sync stays append-only on both ends."""
    have = {e.get("id") for e in iter_learnings(root)}
    out: list[str] = []
    for line in lines or []:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (isinstance(e, dict) and e.get("id") and e.get("title")
                and e["id"] not in have):
            have.add(e["id"])
            out.append(json.dumps(e))
    return out


SSH_OPTS = ["-o", "ConnectTimeout=8", "-o", "BatchMode=yes"]


def _ssh(host: str, remote_cmd: str, stdin: str | None = None,
         timeout: int = 60) -> tuple[int, str]:
    p = subprocess.run(["ssh", *SSH_OPTS, host, remote_cmd], input=stdin,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout


def send_remote(host: str, target: str, content: str, from_: str,
                root: Path | None = None, **kw) -> bool:
    """Deliver one message to a peer's inbox (mkdir + stdin write — the peer's
    own files are appended, never rewritten)."""
    msg = {"id": str(uuid.uuid4()), "from": from_, "to": kw.get("to", target),
           "type": kw.get("type", "message"), "content": content,
           "timestamp": utcnow(), "replyTo": kw.get("reply_to"),
           "ttl": kw.get("ttl", DEFAULT_TTL), "target": target,
           "fromTarget": kw.get("from_target", "aion")}
    if not valid_target(target):
        raise ValueError(f"invalid bridge target {target!r}")
    dest = f"~/.agent-bridge/inbox/{target}/{msg['id']}.json"
    rc, _ = _ssh(host, f"mkdir -p ~/.agent-bridge/inbox/{target} && cat > {dest}",
                 stdin=json.dumps(msg, indent=2))
    return rc == 0


def sync_with(host: str, root: Path | None = None) -> dict:
    """Bidirectional append-only reconcile with one peer. Pull their lines,
    append what we lack; push our lines they lack. Returns counts."""
    root = root or home()
    rc, remote_text = _ssh(
        host, "cat ~/.agent-bridge/shared-context/learnings.ndjson 2>/dev/null")
    pulled, pushed = 0, 0
    if rc == 0 and remote_text.strip():
        missing = merge_missing(root, remote_text.splitlines())
        if missing:
            lf = learnings_file(root)
            lf.parent.mkdir(parents=True, exist_ok=True)
            with open(lf, "a") as fh:
                fh.write("\n".join(missing) + "\n")
            pulled = len(missing)
    try:
        local_ids = {e.get("id") for e in iter_learnings(root)}
    except OSError:
        local_ids = set()
    # ask the peer which local ids it already has, then push only the rest
    rc2, remote_ids = _ssh(
        host, "python3 -c \"import json;[print(json.loads(l).get('id','')) "
               "for l in open('.agent-bridge/shared-context/learnings.ndjson')]\" "
               "2>/dev/null")
    have_remote = set(remote_ids.split()) if rc2 == 0 else set()
    to_push = []
    for e in iter_learnings(root):
        if e.get("id") not in have_remote:
            to_push.append(json.dumps(e))
    if to_push:
        rc3, _ = _ssh(
            host, "mkdir -p ~/.agent-bridge/shared-context && "
                  "cat >> ~/.agent-bridge/shared-context/learnings.ndjson",
            stdin="\n".join(to_push) + "\n")
        if rc3 == 0:
            pushed = len(to_push)
    return {"host": host, "pulled": pulled, "pushed": pushed}


def fleet_hosts(config_path: str | None = None) -> list[str]:
    """Peer tailscale aliases from the fleet truth (randomesh CONFIG export).
    Env AGENT_BRIDGE_HOSTS (space-separated) wins when set."""
    env = os.environ.get("AGENT_BRIDGE_HOSTS", "").split()
    if env:
        return env
    path = Path(config_path or Path.home() / "dev/randomesh/fleet.json")
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    hosts = []
    for n in data.get("nodes", []) or []:
        ts = n.get("tailscale", "")
        if ts and ts not in hosts:
            hosts.append(ts)
    return hosts
