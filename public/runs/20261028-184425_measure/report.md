# Test raporu: measure

- Sonuç: **PASS**
- Başlangıç: 2026-10-28 18:44:25
- Bitiş: 2026-10-28 18:44:31
- Süre: 6.18 sn
- Plan dosyası: `~/stm32bench/plans/measure.toml`
- Açıklama: VDD ve TEMP dahili ölçümleri aralıkta mı, kartın saati PC saatiyle uyuşuyor mu kontrol et.

## Ortam

- Firmware: `~/stm32bench/firmware/build/stm32bench-fw.elf`
  - SHA-256: `32257db3482a2351fc9905047a37ac56bbfe3af0f3dbfffc28e05517a621a7c0`
- Kart UID: `<board-uid-2>` (debug portu, mem 0x1FFF7A10)
- ST-Link seri no: `<stlink-serial>`
- bench.py: 0.2.0 (dosya SHA-256 `48b1904aafed2117…`)
- openocd: 0.12.0
- Sunucu: <host>
- Denetim kaydı hash: `e895479c544487c1180e351c1b3486f7258475364d943da23c263672e9d17e1a` (runs/audit.jsonl)

| # | Adım | Sonuç | Açıklama | Süre (sn) |
|---|---|---|---|---|
| 1 | flash | PASS | stm32bench-fw.elf yüklendi ve doğrulandı. | 0.816 |
| 2 | reset | PASS | Kart resetlendi. | 0.155 |
| 3 | açılış mesajı | PASS | 'BOOT stm32bench-fw v1.2.0 reset=SFT,PIN' | 0.002 |
| 4 | besleme gerilimi (mV) | PASS | 'VDD 2954' (değer 2954 [2850, 3150] aralığında) | 0.016 |
| 5 | çip sıcaklığı (°C) | PASS | 'TEMP 29.6' (değer 29.6 [15, 60] aralığında) | 0.016 |
| 6 | kart saati / PC saati | PASS | oran 0.9948: kart 4980 ms, PC 5005.9 ms (±13.4 ms) ([0.97, 1.03] aralığında) | 5.019 |

Seri port dökümü: `serial.log`
