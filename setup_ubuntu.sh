#!/usr/bin/env bash
# stm32bench Ubuntu kurulumu (22.04 / 24.04, masaüstü veya server)
# Kullanım:  bash setup_ubuntu.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== 1/5 Paketler kuruluyor (sudo şifresi isteyebilir) =="
sudo apt-get update
sudo apt-get install -y python3 python3-serial openocd gcc-arm-none-eabi binutils-arm-none-eabi

echo "== 2/5 Kullanıcı yetkileri: dialout (seri port), plugdev (ST-Link) =="
sudo usermod -aG dialout,plugdev "$USER"

echo "== 3/5 Laptop kapağı kapanınca ve boşta kalınca uyumasın =="
sudo mkdir -p /etc/systemd/logind.conf.d
sudo tee /etc/systemd/logind.conf.d/stm32bench.conf >/dev/null <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null

echo "== 4/5 Ayar dosyası ve firmware =="
if [ ! -f bench.toml ]; then
  cp bench.example.toml bench.toml
  echo "bench.toml oluşturuldu (seri portu kontrol etmeyi unutma)."
fi
python3 firmware/build.py

echo "== 5/5 Bitti =="
cat <<'EOF'

Şimdi:
  1) Yeni grup yetkileri için bir kere çıkış yapıp tekrar gir (SSH ise bağlantıyı kapatıp aç)
     veya bilgisayarı yeniden başlat (kapak ayarı da böylece devreye girer).
  2) python3 bench.py ports        -> USB-TTL'in adını bul, bench.toml [serial] port'a yaz
  3) python3 bench.py doctor       -> her satır OK olmalı
  4) python3 bench.py run smoke    -> ilk gerçek test
EOF
