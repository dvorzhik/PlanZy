#!/usr/bin/env bash
# Настройка SSH-доступа пользователя planzy к GitHub через deploy key.
set -eu

sudo touch /opt/planzy/.ssh/config
sudo chown planzy:planzy /opt/planzy/.ssh/config

if ! sudo grep -q "Host github.com" /opt/planzy/.ssh/config; then
    sudo tee -a /opt/planzy/.ssh/config >/dev/null <<'EOF'
Host github.com
    HostName github.com
    User git
    IdentityFile /opt/planzy/.ssh/id_ed25519
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
EOF
fi

sudo chmod 600 /opt/planzy/.ssh/config
echo "=== SSH CONFIG ==="
sudo cat /opt/planzy/.ssh/config
echo "=== TEST GITHUB ==="
sudo -u planzy ssh -o BatchMode=yes -T git@github.com 2>&1 || true
