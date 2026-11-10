# Test raporu: measure

- Sonuç: **PASS**
- Başlangıç: 2026-10-28 18:44:36
- Bitiş: 2026-10-28 18:44:41
- Süre: 5.22 sn
- Plan dosyası: `~/stm32bench/plans/measure.toml`
- Açıklama: VDD ve TEMP dahili ölçümleri aralıkta mı, kartın saati PC saatiyle uyuşuyor mu kontrol et.

## Ortam

- Firmware: `~/stm32bench/firmware/build/stm32bench-fw.elf`
  - SHA-256: `32257db3482a2351fc9905047a37ac56bbfe3af0f3dbfffc28e05517a621a7c0`
- Kart UID: `<board-uid-1>` (debug portu, mem 0x1FFF7A10)
- ST-Link seri no: `sim`
- bench.py: 0.2.0 (dosya SHA-256 `48b1904aafed2117…`)
- sim: sim
- Sunucu: <host>
- Denetim kaydı hash: `7c6d409da0ff089fb078b98eb4abf8da6ac91160638b5ff6fd32a39d88a851dc` (runs/audit.jsonl)

| # | Adım | Sonuç | Açıklama | Süre (sn) |
|---|---|---|---|---|
| 1 | flash | PASS | [sim] stm32bench-fw.elf yüklendi ve doğrulandı. | 0.216 |
| 2 | reset | PASS | [sim] Kart resetlendi. | 0.005 |
| 3 | açılış mesajı | PASS | 'BOOT stm32bench-fw v1.2.0 reset=PIN,SFT' | 0.0 |
| 4 | besleme gerilimi (mV) | PASS | 'VDD 3000' (değer 3000 [2850, 3150] aralığında) | 0.0 |
| 5 | çip sıcaklığı (°C) | PASS | 'TEMP 30.0' (değer 30 [15, 60] aralığında) | 0.0 |
| 6 | kart saati / PC saati | PASS | oran 1.0001: kart 5001 ms, PC 5000.4 ms (±0.2 ms) ([0.97, 1.03] aralığında) | 5.001 |

Seri port dökümü: `serial.log`
