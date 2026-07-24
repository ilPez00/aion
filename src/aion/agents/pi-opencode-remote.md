This prompt configures the Pi's opencode instance to treat this machine as its primary remote executor.

Copy this to the Pi's `~/.config/opencode/opencode.json`:

```json
{
  "remote": {
    "enabled": true,
    "default_host": "192.168.1.3",
    "default_port": 22,
    "user": "gio",
    "identity_file": "~/.ssh/id_ed25519",
    "autoprompt": true,
    "autoprompt_template": "ssh gio@192.168.1.3 'cd {cwd} && {command}'"
  },
  "agent": {
    "mode": "remote-first",
    "fallback_to_local": false
  }
}
```

To apply remotely from here:

```bash
ssh gio@192.168.1.5 'mkdir -p ~/.config/opencode && cat > ~/.config/opencode/opencode.json << '\''EOF'\''
{
  "remote": true,
  "default_host": "192.168.1.3",
  "autoprompt": true
}
EOF'
```
