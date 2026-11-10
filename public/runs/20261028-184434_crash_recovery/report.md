# Test raporu: crash_recovery

- Sonuç: **PASS**
- Başlangıç: 2026-10-28 18:44:34
- Bitiş: 2026-10-28 18:44:36
- Süre: 1.27 sn
- Plan dosyası: `~/stm32bench/plans/crash_recovery.toml`
- Açıklama: Bilerek HardFault oluştur, raporun geldiğini ve resetle kartın kurtulduğunu doğrula.

## Ortam

- Firmware: `~/stm32bench/firmware/build/stm32bench-fw.elf`
  - SHA-256: `32257db3482a2351fc9905047a37ac56bbfe3af0f3dbfffc28e05517a621a7c0`
- Kart UID: `<board-uid-1>` (debug portu, mem 0x1FFF7A10)
- ST-Link seri no: `sim`
- bench.py: 0.2.0 (dosya SHA-256 `48b1904aafed2117…`)
- sim: sim
- Sunucu: <host>
- Denetim kaydı hash: `7a2dd9a9081c6489d8829f1420f44427ab87a001ffb467eda3f641001c9f7cc8` (runs/audit.jsonl)

| # | Adım | Sonuç | Açıklama | Süre (sn) |
|---|---|---|---|---|
| 1 | flash | PASS | [sim] stm32bench-fw.elf yüklendi ve doğrulandı. | 0.216 |
| 2 | yükleme sonrası açılış | PASS | 'BOOT stm32bench-fw v1.2.0 reset=PIN,SFT' | 0.0 |
| 3 | çökme raporu | PASS | 'HARDFAULT pc=0x080006C6 lr=0x080000A7 cfsr=0x00010000 hfsr=0x40000000' | 0.028 |
| 4 | çöktükten sonra komut | PASS | 'PING' gönderildi. | 0.0 |
| 5 | kart susmalı | PASS | 1 sn boyunca çıktı yok (beklenen). | 1.004 |
| 6 | reset | PASS | [sim] Kart resetlendi. | 0.016 |
| 7 | resetten sonra açılış | PASS | 'BOOT stm32bench-fw v1.2.0 reset=PIN,SFT' | 0.0 |
| 8 | kart tekrar cevap veriyor | PASS | 'PONG' | 0.0 |

Seri port dökümü: `serial.log`
