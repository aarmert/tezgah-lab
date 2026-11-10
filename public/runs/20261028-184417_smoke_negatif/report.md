# Test raporu: smoke

- Sonuç: **FAIL**
- Başlangıç: 2026-10-28 18:44:17
- Bitiş: 2026-10-28 18:44:19
- Süre: 2.42 sn
- Plan dosyası: `~/stm32bench/plans/smoke.toml`
- Açıklama: Firmware'i yükle, açılış mesajını gör, temel komutlara cevap veriyor mu kontrol et.

## Ortam

- Firmware: `~/stm32bench/firmware/build/stm32bench-fw.elf`
  - SHA-256: `32257db3482a2351fc9905047a37ac56bbfe3af0f3dbfffc28e05517a621a7c0`
- Kart UID: `<board-uid-2>` (debug portu, mem 0x1FFF7A10, seri porttaki UID ile aynı)
- ST-Link seri no: `<stlink-serial>`
- bench.py: 0.2.0 (dosya SHA-256 `48b1904aafed2117…`)
- openocd: 0.12.0
- Sunucu: <host>
- Denetim kaydı hash: `c006bac30ca6fd522d99d9f9c52bbdfa9bb74f5b5fe968d143916ce006377247` (runs/audit.jsonl)

| # | Adım | Sonuç | Açıklama | Süre (sn) |
|---|---|---|---|---|
| 1 | flash | PASS | stm32bench-fw.elf yüklendi ve doğrulandı. | 0.831 |
| 2 | reset | PASS | Kart resetlendi. | 0.162 |
| 3 | açılış mesajı | PASS | 'BOOT stm32bench-fw v1.2.0 reset=SFT,PIN' | 0.0 |
| 4 | send PING | PASS | 'PONG' | 0.009 |
| 5 | send VER | PASS | 'VER 1.2.0' | 0.016 |
| 6 | send TICK | PASS | 'TICK 35' (değer 35 [1, 600000] aralığında) | 0.016 |
| 7 | send UID | PASS | 'UID <board-uid-2>' | 0.016 |
| 8 | send LED O ON | PASS | 'OK LED O ON' | 0.016 |
| 9 | send LED O OFF | PASS | 'OK LED O OFF' | 0.016 |
| 10 | send FOO | FAIL | 'FOO' komutuna 1 sn içinde /^ERR unknown command$__BU_SATIR_ESLESMEMELI__/ desenine uyan satır gelmedi. | 1.024 |

## Hata

- Tür: `expect_timeout`
- Olası sebep: Cevap geldi ama beklenen desene uymadı: aşağıdaki satırlara bak

```
Son seri trafik:
> VER
< VER 1.2.0
> TICK
< TICK 35
> UID
< UID <board-uid-2>
> LED O ON
< OK LED O ON
> LED O OFF
< OK LED O OFF
> FOO
< ERR unknown command
```

## Kurtarma

- Sonuç: kart kurtarıldı
- reset: OK BOOT stm32bench-fw v1.2.0 reset=SFT,PIN

Seri port dökümü: `serial.log`
