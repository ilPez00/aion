This prompt configures the Pi's opencode instance to treat this machine as its primary remote executor.

> Addresses are Tailscale aliases (`*-ts`), not LAN IPs: plain-LAN hostnames
> are frequently down, and LAN IPs change. Reach peers by alias.

Copy this to the Pi's `~/.config/opencode/opencode.json`:

```json
{
  "remote": {
    "enabled": true,
    "default_host": "pansa-ts",
    "default_port": 22,
    "user": "gio",
    "identity_file": "~/.ssh/id_ed25519",
    "autoprompt": true,
    "autoprompt_template": "ssh gio@pansa-ts 'cd {cwd} && {command}'"
  },
  "agent": {
    "mode": "remote-first",
    "fallback_to_local": false
  }
}
```

To apply remotely from here:

```bash
ssh gio@pi-ts 'mkdir -p ~/.config/opencode && cat > ~/.config/opencode/opencode.json << '\''EOF'\''
{
  "remote": true,
  "default_host": "pansa-ts",
  "autoprompt": true
}
EOF'
```
