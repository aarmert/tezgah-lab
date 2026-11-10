# Test raporu: measure

- Sonuç: **FAIL**
- Başlangıç: 2026-10-28 18:44:31
- Bitiş: 2026-10-28 18:44:34
- Süre: 2.33 sn
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
| 1 | flash | PASS | stm32bench-fw.elf yüklendi ve doğrulandı. | 0.827 |
| 2 | reset | PASS | Kart resetlendi. | 0.148 |
| 3 | açılış mesajı | PASS | 'BOOT stm32bench-fw v1.2.0 reset=SFT,PIN' | 0.0 |
| 4 | besleme gerilimi (mV) | PASS | 'VDD 2952' (değer 2952 [2850, 3150] aralığında) | 0.007 |
| 5 | çip sıcaklığı (°C) | PASS | 'TEMP 29.6' (değer 29.6 [15, 60] aralığında) | 0.016 |
| 6 | kart saati / PC saati | FAIL | saat oranı 0.9943 beklenen aralık [-2.0, -1.0] dışında; kart 1002 ms, PC 1007.7 ms (±15.4 ms). | 1.023 |

## Hata

- Tür: `value_out_of_range`
- Olası sebep: SysTick reload değeri yanlış hesaplanmış (HSI_HZ / 1000 - 1 olmalı)
- Olası sebep: Saat kaynağı veya frekansı firmware'in varsaydığı gibi değil (HSI 16 MHz, PLL yok)
- Olası sebep: HSI kalibrasyonu bozuk (RCC_CR HSITRIM)
- Olası sebep: Kesmeler uzun süre kapalı kalıyor, SysTick kesmeleri kaçıyor

## Kurtarma

- Sonuç: kart kurtarıldı
- reset: OK BOOT stm32bench-fw v1.2.0 reset=SFT,PIN

Seri port dökümü: `serial.log`
