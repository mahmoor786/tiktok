#!/usr/bin/env bash
# One-command install for a Linux server (Ubuntu or Oracle Linux, ARM or x86).
#   bash setup.sh
# Safe to run again after an update: it keeps your .env, keys and data.
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR="$(pwd)"
say() { printf '\n== %s\n' "$1"; }

# ---- 1. find (or install) Python 3.10+ ----
find_python() {
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      echo "$candidate"; return 0
    fi
  done
  return 1
}
say "Checking Python"
if ! PY="$(find_python)"; then
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y && sudo apt-get install -y python3 python3-venv
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3.11
  fi
  PY="$(find_python)" || { echo "Python 3.10 or newer is needed and could not be installed automatically."; exit 1; }
fi
echo "Using $PY ($($PY --version))"
if ! "$PY" -c 'import venv, ensurepip' 2>/dev/null; then
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y && sudo apt-get install -y python3-venv
  fi
fi

# ---- 2. Python packages in a private folder ----
say "Installing packages"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

# ---- 3. settings file with a random password ----
NEW_PASSWORD=""
if [ ! -f .env ]; then
  NEW_PASSWORD="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(12))')"
  printf 'APP_PASSWORD=%s\nPORT=8080\n' "$NEW_PASSWORD" > .env
  chmod 600 .env
fi
PORT="$(grep -E '^PORT=' .env | tail -1 | cut -d= -f2 | tr -d '[:space:]"')"
PORT="${PORT:-8080}"
mkdir -p data && chmod 700 data
chmod +x run.sh

# ---- 4. open the port on the server's own firewall (never fatal) ----
say "Opening port $PORT on this server"
if command -v firewall-cmd >/dev/null 2>&1 && sudo firewall-cmd --state >/dev/null 2>&1; then
  sudo firewall-cmd --permanent --add-port="${PORT}/tcp" >/dev/null && sudo firewall-cmd --reload >/dev/null && echo "firewalld: opened" || true
elif command -v iptables >/dev/null 2>&1; then
  if ! sudo iptables -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null; then
    # Oracle's Ubuntu images end the INPUT chain with a REJECT rule, so insert near the top.
    sudo iptables -I INPUT 1 -p tcp --dport "$PORT" -j ACCEPT && echo "iptables: opened" || true
    if command -v netfilter-persistent >/dev/null 2>&1; then sudo netfilter-persistent save >/dev/null 2>&1 || true; fi
  else
    echo "iptables: already open"
  fi
fi

# ---- 5. keep it running, and start it after reboots ----
say "Installing the system service"
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  sudo tee /etc/systemd/system/trendpack.service >/dev/null <<UNIT
[Unit]
Description=Trendpack (product research and video packs)
After=network-online.target
Wants=network-online.target

[Service]
User=$(id -un)
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/run.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
  sudo systemctl enable trendpack >/dev/null 2>&1
  sudo systemctl restart trendpack
  sleep 2
  if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then echo "Service is running."; else
    echo "The service did not answer yet. See: sudo journalctl -u trendpack -n 50"; fi
else
  echo "systemd not found. Start it by hand with: ./run.sh"
fi

IP="$(curl -fsS --max-time 5 https://ifconfig.me 2>/dev/null || echo YOUR-SERVER-IP)"
say "Done"
echo "Open:      http://${IP}:${PORT}"
if [ -n "$NEW_PASSWORD" ]; then echo "Password:  ${NEW_PASSWORD}   (saved in .env, change it there any time)"; else
  echo "Password:  unchanged (see APP_PASSWORD in .env)"; fi
echo
echo "If the page does not open: in the Oracle Cloud console add an Ingress Rule for TCP port ${PORT}"
echo "(Networking > Virtual Cloud Networks > your VCN > Security Lists > Default > Add Ingress Rule, source 0.0.0.0/0)."
echo "Then sign in, open Settings, paste the Gemini and Tavily keys and tap 'Save keys and check connections'."
