# Test raporu: smoke

- Sonuç: **PASS**
- Başlangıç: 2026-10-28 18:44:34
- Bitiş: 2026-10-28 18:44:34
- Süre: 0.22 sn
- Plan dosyası: `~/stm32bench/plans/smoke.toml`
- Açıklama: Firmware'i yükle, açılış mesajını gör, temel komutlara cevap veriyor mu kontrol et.

## Ortam

- Firmware: `~/stm32bench/firmware/build/stm32bench-fw.elf`
  - SHA-256: `32257db3482a2351fc9905047a37ac56bbfe3af0f3dbfffc28e05517a621a7c0`
- Kart UID: `<board-uid-1>` (debug portu, mem 0x1FFF7A10, seri porttaki UID ile aynı)
- ST-Link seri no: `sim`
- bench.py: 0.2.0 (dosya SHA-256 `48b1904aafed2117…`)
- sim: sim
- Sunucu: <host>
- Denetim kaydı hash: `2f1c28af286cf1e706f86afea996c2dc3e5ed51dc23e54a801b14fe9fa53c59c` (runs/audit.jsonl)

| # | Adım | Sonuç | Açıklama | Süre (sn) |
|---|---|---|---|---|
| 1 | flash | PASS | [sim] stm32bench-fw.elf yüklendi ve doğrulandı. | 0.216 |
| 2 | reset | PASS | [sim] Kart resetlendi. | 0.004 |
| 3 | açılış mesajı | PASS | 'BOOT stm32bench-fw v1.2.0 reset=PIN,SFT' | 0.0 |
| 4 | send PING | PASS | 'PONG' | 0.0 |
| 5 | send VER | PASS | 'VER 1.2.0' | 0.0 |
| 6 | send TICK | PASS | 'TICK 4' (değer 4 [1, 600000] aralığında) | 0.0 |
| 7 | send UID | PASS | 'UID <board-uid-1>' | 0.0 |
| 8 | send LED O ON | PASS | 'OK LED O ON' | 0.0 |
| 9 | send LED O OFF | PASS | 'OK LED O OFF' | 0.0 |
| 10 | send FOO | PASS | 'ERR unknown command' | 0.0 |

Seri port dökümü: `serial.log`
