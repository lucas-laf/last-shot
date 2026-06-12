#!/usr/bin/env bash
# One-shot EC2 setup for the last_shot tracker + shadow executor.
# Run as ubuntu on a fresh Ubuntu 24.04 box. Expects .env scp'd separately.
set -euo pipefail

sudo apt-get update -qq
sudo apt-get install -y -qq git curl python3-venv chrony

# accurate clocks matter for staleness checks and latency math
sudo systemctl enable --now chrony

curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

if [ ! -d "$HOME/last-shot" ]; then
    git clone https://github.com/lucas-laf/last-shot.git "$HOME/last-shot"
fi
cd "$HOME/last-shot"
uv venv --python 3.12 && uv sync --extra dev

[ -f .env ] || { echo "MISSING .env — scp it before starting the service"; exit 1; }

sudo cp deploy/lastshot-tracker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lastshot-tracker
systemctl status lastshot-tracker --no-pager | head -5
