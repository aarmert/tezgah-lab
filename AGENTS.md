# Ajan kuralları (tezgah)

Bu klasör bir STM32 test tezgahına bağlı. Karta sadece `bench.py` üzerinden dokun.
Tezgahın gözü bir Android telefon: kamera aracı `camera/` altında.

## Klasörler
- `bench.py`, `bench.toml`, `plans/`, `runs/`: test tezgahı aracı, ayarı, planlar, sonuçlar
- `firmware/`: karttaki test firmware'i (`stm32bench-fw`) ve `build.py`
- `camera/`: telefon kamerası aracı (Bun + TypeScript, scrcpy/adb/ffmpeg). `cam_blink` adımı bunu kullanır.
- `.opencode/plugins/android-camera/`: OpenCode için `android_take_photo` aracı (`camera/` kodunu kullanır)
- `docs/raporlar/`: deneme raporları

## Donanım
- Kart: STM32F407G-DISC1 (STM32F407VG, Cortex-M4), LED'ler PD12-PD15
- Seri test kanalı: USART2, PA2=TX, PA3=RX, 115200 8N1 (USB-TTL üzerinden)
- Saat: HSI 16 MHz
- Kamera: USB hata ayıklaması açık Android telefon (adb seri `<ADB_SERIAL>`), Android 12+

## Komutlar (her zaman --json kullan)
- Derle: `python firmware/build.py --json`
- Test planı çalıştır: `python bench.py --json run smoke`
- Yeni özelliği doğrularken: `python bench.py --json run <plan> --verify-can-fail`
- Tek komut dene: `python bench.py --json send "PING" --expect "^PONG$"`
- Reset ve açılış kontrolü: `python bench.py --json reset --expect "^BOOT"`
- Kart cevap vermiyorsa: `python bench.py --json recover`
- Kart donmuş/çökmüşse nerede olduğunu gör: `python bench.py --json halt-dump`
  (pc, hata register'ları ve açıklamaları, çökme anındaki `stacked_frame`, yığındaki kod adresleri;
  çekirdeği sonra devam ettirir). Önce bunu çalıştır, sonra resetle: reset kanıtı siler.
- Bellek/register oku: `python bench.py --json mem 0x40020C14 --count 1` (sadece okuma, izinli bölgeler)
- `bench.py gdbserver` sadece insan içindir; çalıştırma.

Her sonuçta `ok`, `error_type`, `summary`, `likely_causes` alanları var. Başarısız bir
`run` sonrası `report_dir` içindeki `serial.log` dosyasını oku.

## Kamera
- Fotoğraf çek: `cd camera && ~/.bun/bin/bun scripts/test-camera.ts <ADB_SERIAL> back`
  (son satır JPEG yolunu verir; dosyayı açıp bak). Telefon karta bakmıyorsa `front` dene.
- Video çek: `cd camera && ~/.bun/bin/bun scripts/record-video.ts <ADB_SERIAL> back 4 30`
  (saniye, fps; son satır MP4 yolu). LED ölçümü için bunu elle değil `cam_blink` adımıyla yap.
- OpenCode'da aynı iş için `android_take_photo` aracı var.
- Kamera kodunu değiştirdiysen: `cd camera && ~/.bun/bin/bun test && ~/.bun/bin/bun run typecheck`.
  Testler sahte adb/scrcpy kullanır, telefon gerekmez.

## Yasaklar
- `bench.py`, `bench.toml`, `plans/` ve `runs/` içindeki dosyaları DEĞİŞTİRME. Bir test
  yanlışsa bunu insana söyle; testi geçmek için testi değiştirmek yasak. Bu `camera/tests/` için de geçerli.
- `STM32_Programmer_CLI` veya `openocd` komutlarını doğrudan çalıştırma.
- Option byte, okuma koruması (RDP), mass erase işlemlerine asla dokunma.
- Firmware'de PA13/PA14 (SWD) pinlerini başka amaçla kullanma.

## Yeni test önerme
Yeni bir test gerekiyorsa `plans_proposed/` klasörüne yaz ve insana haber ver.
İnsan onaylayınca `plans/` klasörüne taşır. Her yeni test `--verify-can-fail` ile
negatif kontrolden geçmeli: bilerek bozulduğunda FAIL vermeyen test hiçbir şey kanıtlamaz.

## Çalışma döngüsü
1. Değişikliği firmware/ altında yap.
2. Derle; hata varsa düzelt.
3. İlgili planı çalıştır.
4. FAIL ise önce `likely_causes` ve `serial.log`'a bak, sonra düzelt. Aynı hatada
   3 denemeden sonra dur ve insana özet ver.
