#!/usr/bin/env bash
# Install Tailscale inside WSL (run on BOTH machines).
# After this: SSH/rsync WSL→WSL on port 22.
set -euo pipefail

if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

sudo tailscale up --ssh --accept-routes
sudo tailscale set --ssh

echo
echo "=== This machine ==="
tailscale status --self
echo
echo "Hostname (MagicDNS): $(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || hostname)"
echo
echo "=== Peers ==="
tailscale status
echo
echo "From the other PC, test:"
echo "  ssh $(whoami)@$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || echo '<this-host>.tailXXXX.ts.net')"
echo
echo "Optional — passwordless SSH (run once per machine, paste pubkey on the other):"
echo "  ssh-copy-id $(whoami)@<other-host>.tailXXXX.ts.net"
