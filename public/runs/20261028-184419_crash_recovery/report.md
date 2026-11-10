# Test raporu: crash_recovery

- Sonuç: **PASS**
- Başlangıç: 2026-10-28 18:44:19
- Bitiş: 2026-10-28 18:44:22
- Süre: 2.19 sn
- Plan dosyası: `~/stm32bench/plans/crash_recovery.toml`
- Açıklama: Bilerek HardFault oluştur, raporun geldiğini ve resetle kartın kurtulduğunu doğrula.

## Ortam

- Firmware: `~/stm32bench/firmware/build/stm32bench-fw.elf`
  - SHA-256: `32257db3482a2351fc9905047a37ac56bbfe3af0f3dbfffc28e05517a621a7c0`
- Kart UID: `<board-uid-2>` (debug portu, mem 0x1FFF7A10)
- ST-Link seri no: `<stlink-serial>`
- bench.py: 0.2.0 (dosya SHA-256 `48b1904aafed2117…`)
- openocd: 0.12.0
- Sunucu: <host>
- Denetim kaydı hash: `3a61a7d69215074eb0e1dff294404c70ab0f5b7a4d9750c8edc8cc984bee4b85` (runs/audit.jsonl)

| # | Adım | Sonuç | Açıklama | Süre (sn) |
|---|---|---|---|---|
| 1 | flash | PASS | stm32bench-fw.elf yüklendi ve doğrulandı. | 0.826 |
| 2 | yükleme sonrası açılış | PASS | 'BOOT stm32bench-fw v1.2.0 reset=SFT,PIN' | 0.001 |
| 3 | çökme raporu | PASS | 'HARDFAULT pc=0x080006C6 lr=0x080000D7 cfsr=0x00010000 hfsr=0x40000000' | 0.022 |
| 4 | çöktükten sonra komut | PASS | 'PING' gönderildi. | 0.002 |
| 5 | kart susmalı | PASS | 1 sn boyunca çıktı yok (beklenen). | 1.008 |
| 6 | reset | PASS | Kart resetlendi. | 0.155 |
| 7 | resetten sonra açılış | PASS | 'BOOT stm32bench-fw v1.2.0 reset=SFT,PIN' | 0.002 |
| 8 | kart tekrar cevap veriyor | PASS | 'PONG' | 0.016 |

Seri port dökümü: `serial.log`
