#!/usr/bin/env bash
# stm32bench Ubuntu setup (22.04 / 24.04, desktop or server)
# Usage:  bash setup_ubuntu.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== 1/5 Installing packages (may ask for the sudo password) =="
sudo apt-get update
sudo apt-get install -y curl openocd gcc-arm-none-eabi binutils-arm-none-eabi

echo "== 2/5 User groups: dialout (serial port), plugdev (ST-Link) =="
sudo usermod -aG dialout,plugdev "$USER"

echo "== 3/5 Do not sleep when the laptop lid closes or the machine is idle =="
sudo mkdir -p /etc/systemd/logind.conf.d
sudo tee /etc/systemd/logind.conf.d/stm32bench.conf >/dev/null <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null

echo "== 4/5 Config file and firmware =="
if [ ! -f bench.toml ]; then
  cp bench.example.toml bench.toml
  echo "bench.toml created (remember to check the serial port)."
fi
# uv: the Python environment. It uses the version in .python-version (downloading it if needed)
# and installs the packages at the uv.lock versions into .venv.
if ! command -v uv >/dev/null && [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
"$UV" sync --locked
"$UV" run firmware/build.py

echo "== 5/5 Done =="
cat <<'EOF'

Next:
  1) Log out and back in once for the new groups (over SSH, close and reopen the connection)
     or reboot (that also applies the lid setting).
  2) uv run bench.py ports        -> find the USB-TTL adapter's name, put it in bench.toml [serial] port
  3) uv run bench.py doctor       -> every line should be OK
  4) uv run bench.py run smoke    -> first real test
EOF
