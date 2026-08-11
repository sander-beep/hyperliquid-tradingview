#!/usr/bin/env bash
# One-time VPS provisioning for Ubuntu 24.04 (Hetzner CX22).
# Run as root on a fresh box AFTER adding your SSH key.
set -euo pipefail

echo "== base packages =="
apt-get update
apt-get install -y ca-certificates curl git ufw fail2ban unattended-upgrades sqlite3

echo "== docker =="
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

echo "== ssh hardening (keys only) =="
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
systemctl reload ssh

echo "== firewall: SSH + HTTP(S) only =="
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp     # ACME http-01
ufw allow 443/tcp
ufw --force enable

echo "== fail2ban + unattended-upgrades =="
systemctl enable --now fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "== nightly DB backup cron =="
mkdir -p /opt/rsps-bot
cat >/etc/cron.d/rsps-backup <<'EOF'
15 3 * * * root /opt/rsps-bot/scripts/backup.sh >> /var/log/rsps-backup.log 2>&1
EOF

echo
echo "Done. Next:"
echo "  1. git clone the repo to /opt/rsps-bot"
echo "  2. cp .env.example .env && edit && chmod 600 .env"
echo "  3. point DNS A record of \$DOMAIN at this box"
echo "  4. ./scripts/deploy.sh"
