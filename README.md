# stm32bench

STM32F4 Discovery kartını bilgisayardan yükleyen, resetleyen, seri porttan
konuşan ve test planı çalıştıran Python aracı. Hem sen hem Claude (`--json` ile)
kullanabilir. Linux ve Windows'ta aynen çalışır.

## İçindekiler

| Dosya | Ne işe yarar |
|---|---|
| `bench.py` | Ana araç: yükle, resetle, seri porttan gönder/oku, test planı çalıştır, kurtar |
| `bench.example.toml` | Ayar şablonu (`bench.toml` olarak kopyalanır) |
| `plans/smoke.toml` | Temel test: yükle, açılış mesajı, PING/VER/TICK/UID/LED |
| `plans/crash_recovery.toml` | Kartı bilerek çökertir, raporu okur, resetle geri getirir |
| `plans/measure.toml` | Dahili ölçümler: VDD (VREFINT), çip sıcaklığı, kart saati / PC saati oranı |
| `plans/camera_blink.toml` | Telefon kamerasıyla yeşil LED'in frekansı (BLINK 1/2/4) ve LED G ON'da sabit yanması |
| `firmware/` | Kartta çalışan küçük test firmware'i ve `build.py` (make gerekmez) |
| `setup_ubuntu.sh` | Ubuntu'da her şeyi kuran script |
| `CLAUDE.md` | Claude'un bu klasörde uyacağı kurallar |

## Bağlantı

```
Discovery mini-USB (ST-Link)  ->  bilgisayar USB   (yükleme ve reset)

USB-TTL (3.3V)                    Discovery
  RX   <----------------------    PA2  (USART2 TX)
  TX   ---------------------->    PA3  (USART2 RX)
  GND  -----------------------    GND
  VCC  bağlama! (kart USB'den beslenir)
```

USB-TTL'in 3.3V modunda olduğundan emin ol (üzerinde jumper varsa 3V3'e al).
Discovery'deki CN3 jumperları takılı olmalı (ST-Link karta bağlı modu).

## Ubuntu kurulumu

```bash
# Windows'tan sunucuya kopyala (kendi kullanıcı adın ve IP'nle)
scp stm32bench.zip kullanici@sunucu-ip:~
ssh kullanici@sunucu-ip
sudo apt install -y unzip && unzip stm32bench.zip && cd stm32bench
bash setup_ubuntu.sh
# çıkış yapıp tekrar gir, sonra:
python3 bench.py ports        # USB-TTL'in adını bul, bench.toml'a yaz
python3 bench.py doctor
python3 bench.py run smoke
python3 bench.py run crash_recovery
```

`bench.py ports` çıktısındaki `/dev/serial/by-id/...` ismini kullan. `/dev/ttyUSB0`
yeniden başlatınca başka cihaza kayabilir, by-id ismi kaymaz.

## Komutlar

| Komut | Ne yapar |
|---|---|
| `python3 bench.py doctor` | Araçları, ST-Link'i, seri portu kontrol eder |
| `python3 bench.py ports` | Seri portları listeler |
| `python3 bench.py flash dosya.elf` | Yükler ve doğrular |
| `python3 bench.py reset --expect "^BOOT"` | Resetler, açılış mesajını bekler |
| `python3 bench.py send PING --expect "^PONG$"` | Komut gönderir, cevabı kontrol eder |
| `python3 bench.py send TICK --expect "^TICK (\d+)$" --min 1 --max 100000` | Sayısal aralık kontrolü |
| `python3 bench.py read --seconds 3` | Seri porttan okur |
| `python3 bench.py monitor` | Canlı seri monitör (Ctrl+C ile çık) |
| `python3 bench.py run smoke` | `plans/smoke.toml` planını çalıştırır |
| `python3 bench.py run smoke --verify-can-fail` | Ayrıca bilerek bozulmuş haliyle çalıştırır, FAIL vermeli |
| `python3 bench.py recover` | Sağlam firmware'i reset altında yükleyip kartı kurtarır |
| `python3 bench.py audit` | Denetim kaydının değiştirilmediğini kontrol eder |
| `python3 bench.py halt-dump` | Çekirdeği durdurur; register, hata register'ları (açıklamalı), çökme anındaki pc ve yığını gösterir, devam ettirir |
| `python3 bench.py mem 0x40020C14 --count 4` | Bellek/çevre birimi register'ı okur (sadece okuma; option byte bölgesi kapalı) |
| `python3 bench.py gdbserver` | GDB sunucusu açar (insan için, Ctrl+C ile kapanır; `sudo apt install gdb-multiarch`) |

`halt-dump`, `mem` ve `gdbserver` şimdilik sadece `backend = "openocd"` ile çalışır.
Bazı çevre birimi register'larını okumak yan etki yapar (ör. USART `DR` okumak gelen karakteri tüketir).

Her komutun başına `--json` eklenirse sonuç JSON olur (`ok`, `error_type`,
`summary`, `likely_causes`). Donanım olmadan denemek için `--sim` ekle.

Her `run` sonrası `runs/<tarih>_<plan>/` klasöründe `report.md`, `result.json`
ve zaman damgalı `serial.log` oluşur. Bir test başarısız olursa araç kartı önce
resetle, olmazsa sağlam firmware'i yeniden yükleyerek kendisi kurtarmaya çalışır.
Kart çökerse HARDFAULT satırındaki adresi kaynak koddaki fonksiyon ve satıra çevirir.

## Test planı yazmak

```toml
name = "ornek"
firmware = "firmware/build/stm32bench-fw.elf"

[[step]]
action = "flash"            # plandaki firmware'i yükle

[[step]]
action = "reset"

[[step]]
action = "expect"           # bu desene uyan satırı bekle
pattern = '^BOOT '
timeout = 3

[[step]]
action = "send"             # gönder ve cevabı kontrol et
text = "TICK"
expect = '^TICK (\d+)$'
range = [1, 600000]         # parantez içindeki sayı bu aralıkta olmalı

[[step]]
action = "silence"          # bu süre boyunca hiç çıktı gelmemeli
seconds = 1

[[step]]
action = "delay"
seconds = 0.5

[[step]]
action = "clock_check"      # TICK, PC'de 5 sn bekle, tekrar TICK
seconds = 5
range = [0.97, 1.03]        # kart süresi / PC süresi (time.monotonic) bu aralıkta olmalı
```

`clock_check` sonucunda oran, iki süre ve PC ölçümünün belirsizliği (TICK gidiş-dönüşünün
yarısı) yazılır. `--verify-can-fail` son beklenti adımı `clock_check` ise onun aralığını bozar.

### Kamera ile LED ölçümü (`cam_blink`)

```toml
[[step]]
action = "cam_blink"        # telefonla video çek, LED'in yanıp sönme frekansını ölç
seconds = 4                 # kayıt süresi
range = [1.8, 2.2]          # Hz
# duty_range = [0.4, 0.6]   # isteğe bağlı doluluk aralığı
# skip_s = 0.5              # baştaki kareleri atla (pozlama/odak otursun)

[[step]]
action = "cam_blink"        # LED sabit mi? Bölge ve eşik önceki cam_blink adımından alınır
seconds = 3
steady = "on"               # veya "off"
```

- Video `android-camera-tool` (`scripts/record-video.ts`, scrcpy) ile çekilir. Kare zamanları videonun
  kendi zaman damgalarından alınır (ffprobe).
- LED bölgesi: kareler boyunca parlaklığı en çok değişen 30×30 piksellik yer. LED'in parlak bir yüzeydeki
  yansıması daha çok değişiyorsa onu seçer. Frekans aynı çıkar ama işaretli kareye bak. Sabitlemek için
  `bench.toml`'a `[camera] roi = [x, y, genişlik, yükseklik]` ekle.
- Karar sayılara göre verilir: eşik = parlaklığın %5 ve %95 değerlerinin ortası, geçişler histerezisli,
  geçiş anı iki kare arasında doğrusal ara değerle bulunur. Frekans yükselen ve düşen kenarlardan hesaplanır.
- Ölçülen frekans kare hızının dörtte birinden büyükse mesaj `UYARI:` ile başlar. Kare hızının yarısını
  aşan frekanslar katlanır, yani daha düşük görünür.
- Her adım rapor klasörüne `cam_stepNN.mp4`, `cam_stepNN_roi.jpg` (bölge işaretli, diğer adaylar sarı
  daire) ve `cam_stepNN.csv` (zaman, parlaklık) yazar.
- OpenCV ve NumPy gerekir: `sudo apt install python3-opencv python3-numpy`. `--sim` ile sentetik video kullanılır.

Desenler regex'tir. TOML'da tek tırnak kullan, ters eğik çizgi kaçırmaya gerek kalmaz.

## Test firmware komutları

`PING`, `VER`, `TICK`, `UID`, `VDD` (besleme, mV), `TEMP` (çip sıcaklığı, °C, 0,1 hassasiyet),
`LED <G|O|R|B> <ON|OFF>`, `BLINK <1-20>`
(yeşil LED'in yanıp sönme hızı, kamera testi için), `CRASH` (bilerek HardFault),
`RESET`. Açılışta `BOOT stm32bench-fw v1.2.0 reset=<sebep>` basar. Yeşil LED
ana döngü dönerken 2 Hz yanıp söner; kart donarsa durur.

Firmware STM32F407G-DISC1 ve STM32F411E-DISCO içindir (LED'ler PD12-PD15). `TEMP` sadece
F407'de doğrudur: F411'de sıcaklık sensörü ADC kanal 16 değil 18'dedir.
Saat olarak dahili 16 MHz HSI kullanır, PLL ayarı yoktur.

## Claude ile kullanım

Bu klasörde `claude` başlat. `CLAUDE.md` kuralları otomatik okunur: karta sadece
`bench.py` ile dokunur, test dosyalarını değiştiremez, option byte'lara dokunmaz.

Testleri Claude'un değiştirmesini kesin engellemek için:

```bash
chmod -R a-w plans bench.toml bench.py
```

ve `bench.toml` içinde `plans_dir_only = true` yap.

Kamera fotoğraf aracını kullanıyorsan nasıl çağrılacağını `CLAUDE.md` sonuna bir satır olarak ekle.

## Windows'ta

1. python.org'dan Python 3.11+ kur ("Add to PATH" işaretli).
2. `py -m pip install pyserial`
3. STM32CubeProgrammer kur (ST-Link sürücüsü de gelir). Derleyici için STM32CubeCLT veya Arm GNU Toolchain.
4. `bench.toml` içinde `backend = "cubeprog"`, `port = "COM5"` (Aygıt Yöneticisi'nden veya `py bench.py ports`).
5. `py firmware\build.py`, `py bench.py doctor`, `py bench.py run smoke`

## Sık hatalar

| Hata | Çözüm |
|---|---|
| `probe_not_found` | USB kablosu, `plugdev` grubu (çıkış yapıp tekrar gir), başka program ST-Link'i tutuyor |
| `target_not_responding` | `python3 bench.py recover` |
| `serial_open_failed` | Port adı yanlış veya `dialout` grubunda değilsin |
| `expect_timeout`, hiç satır yok | TX/RX ters, GND bağlı değil, USB-TTL 5V modunda |
| Bozuk karakterler | Baud hızı uyuşmuyor (115200 olmalı) |

## Durum

Firmware QEMU emülatöründe, araç sahte kartla ve gerçek seri port yolu üzerinden
denendi. Gerçek Discovery kartında ilk kez seninle çalışacak; ilk `run smoke`
sonucunu kontrol et.
