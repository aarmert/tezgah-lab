# Test raporu: camera_blink

- Sonuç: **PASS**
- Başlangıç: 2026-10-28 19:03:35
- Bitiş: 2026-10-28 19:04:43
- Süre: 68.26 sn
- Plan dosyası: `~/stm32bench/plans/camera_blink.toml`
- Açıklama: BLINK 1/2/4 frekanslarını kamerayla ölç, LED G ON sonrası LED'in sabit yandığını doğrula.

## Ortam

- Firmware: `~/stm32bench/firmware/build/stm32bench-fw.elf`
  - SHA-256: `32257db3482a2351fc9905047a37ac56bbfe3af0f3dbfffc28e05517a621a7c0`
- Kart UID: `<board-uid-2>` (debug portu, mem 0x1FFF7A10)
- ST-Link seri no: `<stlink-serial>`
- bench.py: 0.2.0 (dosya SHA-256 `48b1904aafed2117…`)
- openocd: 0.12.0
- Sunucu: <host>
- Denetim kaydı hash: `cb25bf3313255b77322b84c744366c3e8a89ce48bf06b0b00ad74ce41afce70c` (runs/audit.jsonl)

| # | Adım | Sonuç | Açıklama | Süre (sn) |
|---|---|---|---|---|
| 1 | flash | PASS | stm32bench-fw.elf yüklendi ve doğrulandı. | 0.829 |
| 2 | açılış mesajı | PASS | 'BOOT stm32bench-fw v1.2.0 reset=SFT,PIN' | 0.0 |
| 3 | send BLINK 1 | PASS | 'OK BLINK 1' | 0.013 |
| 4 | kamera: 1 Hz | PASS | 0.995 Hz, doluluk 0.51 (yükselen 4, düşen 3 kenar, kontrast 162); bölge [837, 777, 30, 30] (otomatik), 108 kare, 30.0 fps; video cam_step04.mp4, işaretli kare cam_step04_roi.jpg; adaylar (852,792) std 84.1, (1662,888) std 76.6, (780,804) std 62.6 ([0.9, 1.1] Hz aralığında) | 21.961 |
| 5 | send BLINK 2 | PASS | 'OK BLINK 2' | 0.016 |
| 6 | kamera: 2 Hz | PASS | 1.985 Hz, doluluk 0.50 (yükselen 5, düşen 5 kenar, kontrast 168); bölge [837, 783, 30, 30] (otomatik), 77 kare, 30.0 fps; video cam_step06.mp4, işaretli kare cam_step06_roi.jpg; adaylar (852,798) std 84.1, (1656,888) std 76.4, (780,804) std 62.5 ([1.8, 2.2] Hz aralığında) | 16.607 |
| 7 | send BLINK 4 | PASS | 'OK BLINK 4' | 0.017 |
| 8 | kamera: 4 Hz | PASS | 4.007 Hz, doluluk 0.53 (yükselen 10, düşen 11 kenar, kontrast 168); bölge [837, 783, 30, 30] (otomatik), 78 kare, 30.0 fps; video cam_step08.mp4, işaretli kare cam_step08_roi.jpg; adaylar (852,798) std 83.7, (1656,888) std 76.5, (780,804) std 62.4 ([3.6, 4.4] Hz aralığında) | 16.813 |
| 9 | send LED G ON | PASS | 'OK LED G ON' | 0.018 |
| 10 | kamera: LED sabit yanıyor | PASS | LED sabit açık: seviye 241 (eşik 159, önceki açık/kapalı 243/75), 0 geçiş; bölge [837, 783, 30, 30] (önceki cam_blink adımı), 46 kare, 30.0 fps; video cam_step10.mp4, işaretli kare cam_step10_roi.jpg | 11.845 |

Seri port dökümü: `serial.log`
