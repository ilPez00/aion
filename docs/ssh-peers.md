# SSH peers — aion instances on other machines

Until now the fleet was one machine's worth: `fleet.discover_local` reads
`~/.aion/instances/*/meta.json`, and `RemoteServer` binds loopback. Anything
further needed `AION_LISTEN=lan`, which puts an endpoint that **executes
prompts** on the network behind one shared bearer token, in cleartext.

SSH peers replace that. A peer is another machine running aion; this cockpit
opens an `ssh -L` tunnel to it and then speaks the HTTP it already spoke.

```
  this machine                                    the peer
  ┌──────────────────────────┐                   ┌──────────────────────┐
  │ HUD / routing / gates    │                   │                      │
  │        │                 │   ssh (22)        │                      │
  │        ▼                 │═══════════════════│                      │
  │ 127.0.0.1:8901 ──────────┼───── encrypted ───┼──▶ 127.0.0.1:8765    │
  │        (tunnel local end)│                   │    RemoteServer      │
  └──────────────────────────┘                   └──────────────────────┘
```

Neither aion binds a public interface. The only listening port on either
machine is sshd.

## Why a tunnel rather than TLS

Building this over HTTPS means shipping certificate generation, distribution,
pinning and rotation, and getting all four right. SSH already has them, plus
`authorized_keys` as an access-control language aion would otherwise have to
invent. The tunnel also means **nothing above the transport changed** —
routing, approval gates, status polling and cancellation all use the same code
paths they use for a local instance.

## Setup

On the machine you want to reach, aion's remote listener has to be running on
loopback port 8765. **The cockpit starts it, so a machine with nobody sitting
at it has nothing listening** — which is backwards, since the machines you
dispatch to are exactly the ones you are not sitting at. On a headless box, or
one running only the web HUD, start the node:

```bash
./aion.sh node --port 8765          # foreground
setsid nohup ./aion.sh node --port 8765 >~/.aion/node.log 2>&1 &   # detached
```

Both ends also need the same `~/.aion/token`: the tunnel proves which machine
you reached, the token proves the caller is a fleet member. Copy it across
(and keep a backup of whatever was there).

On the controller:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/aion_pi5 -C aion-peer   # a key just for this
./aion.sh peers add pi5 gio@10.0.0.5 --key ~/.ssh/aion_pi5 --label "The Pi"
```

`add` prints the line to install on the peer. Put it in the peer's
`~/.ssh/authorized_keys`:

```
restrict,permitopen="127.0.0.1:8765" ssh-ed25519 AAAA... aion-peer
```

`restrict` disables everything — pty, shell, agent forwarding, X11, port
forwarding, user rc files — and `permitopen` re-enables exactly one thing: a
forward to aion on that machine's loopback. **A key installed this way cannot
get a shell and cannot reach any other host or port.** If the controller is
compromised, what the attacker gains is the ability to talk to aion on the
peer, not a foothold on the peer.

Add `from="203.0.113.4"` at the front to pin the source address:

```bash
./aion.sh peers authorize "$(cat ~/.ssh/aion_pi5.pub)" --from 203.0.113.4
```

Then check it:

```bash
./aion.sh peers test pi5
# pi5: OK — raspberrypi · 0 running · harness claude (via 127.0.0.1:8901)
```

## What `test` tells you

The two failures need different fixes, so they are reported separately:

| output | meaning |
|---|---|
| `TUNNEL FAILED — Permission denied (publickey).` | the key is not installed, or is the wrong one |
| `TUNNEL FAILED — Host key verification failed.` | the peer's host key changed, or is not in `known_hosts` |
| `TUNNEL FAILED — connect ... Connection timed out` | firewall, wrong host, or sshd not running |
| `tunnel up ... but no aion answered` | ssh is fine; aion is not running there, `--remote-port` is wrong, or the fleet tokens differ |

That last row is the one worth internalising: **an ssh forward outlives the
remote aion process.** The local port keeps accepting connections after the far
end dies, because ssh only discovers the problem when it tries to connect, one
hop later. Anything that treats "tunnel up" as "peer alive" will route work
into a hole, so aion polls `/status` through the tunnel and treats a silent far
end as offline.

## Using them

Peers appear as instances in the Agents graph, marked `⇄`, and as routing
targets. Drag a task onto one to run it there — dispatch is still fail-closed
and still needs the confirm step, which matters more across a network, not
less. `GET /api/peers` returns the same data for anything else.

## Security notes

- **The token still applies.** SSH proves the machine; the fleet token proves
  the caller is a fleet member. Copy `~/.aion/token` to each peer.
- **Peers come only from `~/.aion/peers.json`.** No HTTP endpoint accepts a
  hostname, so the LAN-reachable HUD cannot make this machine open an SSH
  connection to somewhere its owner never configured.
- **Host keys.** First contact is trust-on-first-use (`accept-new`), which is a
  real if narrow MITM window. `AION_SSH_STRICT=yes` requires the host key to be
  in `known_hosts` already and fails otherwise. A *changed* key is refused in
  both modes.
- **A peer sees your token** when you call it. Peers are trusted with fleet
  membership by definition. They are not trusted with your shell, which is what
  `restrict` is for.
- **Config is argv.** `ssh` reads any argument beginning with `-` as an option,
  so a peer whose host is `-oProxyCommand=…` would execute a command on *this*
  machine. Peers are validated for that before anything is spawned, and the
  target is passed after `--`.

## Files

| path | what |
|---|---|
| `src/aion/sshlink.py` | peers, validation, tunnel supervision, pool |
| `scripts/aion_peers.py` | the `aion.sh peers` CLI |
| `~/.aion/peers.json` | the peer list (respects `$AION_HOME`) |
| `routing.candidates_from_ssh` | peers as routing candidates |
| `GET /api/peers` | peer + tunnel state for the HUD |
