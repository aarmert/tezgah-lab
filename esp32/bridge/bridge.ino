// tezgah-bridge 0.1: ESP32-S3 line bridge with a precisely timed NRST (public/stress/SPEC-ESP32.md 2).
//
// PC  <-> ESP32-S3 UART0 (CH343 USB "UART" port), 921600 8N1, one command per line.
// ESP <-> STM32 (NUCLEO-C031C6 USART1 PA9/PA10), UART1 TX=GPIO17 RX=GPIO18, 115200 8N1.
// GPIO4 open-drain -> STM32 NRST, released (high-Z) except while a pulse is fired.
//
// The update logic lives in the Python host (stress/). This firmware only moves lines and fires the
// reset at the right microsecond:
//   PING                       -> ESP PONG tezgah-bridge 0.1
//   TX <text>                  -> <text>\r\n to the STM32 (no reply)
//   TXH <hex>                  -> raw bytes to the STM32 (no CR/LF added, no reply)
//   NRST PULSE <us>            -> NRST low for <us> (10..1000000), released -> ESP OK PULSE
//   NRST ARM TX <delay_us> [n] -> 1 ms pulse <delay_us> after the last byte of the n-th following
//                                 TX/TXH has left the UART (n default 1) -> ESP OK ARM
//   NRST ARM RX <delay_us> <p> -> 1 ms pulse <delay_us> after a received line starting with <p>
//                                 is complete -> ESP OK ARM
//   NRST DISARM                -> ESP OK DISARM
//   STAT                       -> ESP STAT rx_lines=.. tx_lines=.. fired=.. overruns=..
//   TIME ON|OFF (extension)    -> ESP OK TIME; while on: ESP TXDONE <n> <t_us> after each TX/TXH
//                                 and ESP RXT <t_us> right before each RX line (esp_timer clock)
//   SELFTEST (extension)       -> ESP SELFTEST n=.. late_us min=.. avg=.. max=.. (timer only, NRST untouched)
// ESP -> PC: RX <text> (one STM32 line, CR stripped, bytes < 0x20 or > 0x7E and '\' as \xHH),
//            ESP READY tezgah-bridge 0.1, ESP OK/ERR ..., ESP NRST FIRED <delay_us> <late_us>,
//            ESP ARMJITTER <late_us>, ESP STAT ...
//
// Timing: Serial1 has no TX ring buffer (setTxBufferSize(0)), so write() returns once every byte is
// in the hardware FIFO and flush() spins until the shifter is idle: that moment (t_done) is the end
// of the stop bit, within a few microseconds. The pulse is planned at t_done + delay. Delays below
// SPIN_US are busy-waited in loop(); longer ones use a one-shot esp_timer that wakes SPIN_US early
// and busy-waits the rest. The edge time is read back from the pad (input+open-drain mode), and
// late_us = edge - plan is reported with every fire.
//
// Build: python3 esp32/build.py --json (arduino-cli, FQBN in esp32/build.py). Upload: see README.md.

#include <Arduino.h>
#include "driver/gpio.h"
#include "esp_timer.h"
#include "esp_rom_sys.h"

#define BRIDGE_ID "tezgah-bridge"
#define BRIDGE_VERSION "0.1"

static const int STM_TX_PIN = 17;          // ESP UART1 TX -> STM32 PA10 (USART1 RX)
static const int STM_RX_PIN = 18;          // ESP UART1 RX <- STM32 PA9  (USART1 TX)
static const gpio_num_t NRST_PIN = GPIO_NUM_4;
static const uint32_t STM_BAUD = 115200;
static const uint32_t PC_BAUD = 921600;

static const size_t PC_LINE_MAX = 2100;    // "TXH " + 2 x 1024 hex digits fits
static const size_t STM_LINE_MAX = 1024;   // a longer STM32 line is emitted in pieces
static const uint32_t FIRE_PULSE_US = 1000;
static const int64_t SPIN_US = 300;        // busy-wait the last part of every delay

static char pc_buf[PC_LINE_MAX + 1];
static size_t pc_len = 0;
static bool pc_overflow = false;

static uint8_t stm_buf[STM_LINE_MAX];
static size_t stm_len = 0;

static uint32_t rx_lines = 0, tx_lines = 0, fired = 0;
static volatile uint32_t overruns = 0;
static bool timing_on = false;

// --- arm state -----------------------------------------------------------------------------
enum ArmKind { ARM_NONE = 0, ARM_TX = 1, ARM_RX = 2 };
static ArmKind arm_kind = ARM_NONE;
static uint32_t arm_delay_us = 0;
static uint32_t arm_count = 0;             // ARM TX: TX/TXH commands still to go
static char arm_prefix[128];

static esp_timer_handle_t fire_timer = nullptr;
static volatile uint32_t arm_gen = 0;      // bumped by every ARM/DISARM: a stale timer does nothing
static volatile int64_t plan_t = 0;        // planned edge (esp_timer_get_time() clock)
static volatile uint32_t plan_delay = 0;
static volatile uint32_t plan_gen = 0;
static volatile bool fire_done = false;    // set by fire_now(), reported by loop()
static volatile int64_t fire_late = 0;
static volatile uint32_t fire_delay = 0;

// --- NRST ------------------------------------------------------------------------------------
static void nrst_release() { gpio_set_level(NRST_PIN, 1); }

static void nrst_init() {
  gpio_set_level(NRST_PIN, 1);             // level first: no low glitch when the output turns on
  gpio_config_t io = {};
  io.pin_bit_mask = 1ULL << NRST_PIN;
  io.mode = GPIO_MODE_INPUT_OUTPUT_OD;     // open-drain, and the pad can be read back
  io.pull_up_en = GPIO_PULLUP_DISABLE;     // the STM32 has its own NRST pull-up
  io.pull_down_en = GPIO_PULLDOWN_DISABLE;
  io.intr_type = GPIO_INTR_DISABLE;
  gpio_config(&io);
  nrst_release();
}

// Busy-wait until plan, pull NRST low, time the edge on the pad, hold FIRE_PULSE_US, release.
static void fire_now(uint32_t gen) {
  int64_t plan = plan_t;
  while (esp_timer_get_time() < plan) {
  }
  if (gen != arm_gen) return;              // disarmed or re-armed meanwhile
  gpio_set_level(NRST_PIN, 0);
  int64_t edge = esp_timer_get_time();
  for (int i = 0; i < 1000 && gpio_get_level(NRST_PIN) != 0; i++) {
    edge = esp_timer_get_time();
  }
  esp_rom_delay_us(FIRE_PULSE_US);
  nrst_release();
  fire_late = edge - plan;
  fire_delay = plan_delay;
  fire_done = true;
}

static void fire_timer_cb(void *) { fire_now(plan_gen); }

static void schedule_fire(int64_t t_ref, uint32_t delay_us) {
  esp_timer_stop(fire_timer);
  uint32_t gen = arm_gen;
  plan_t = t_ref + (int64_t)delay_us;
  plan_delay = delay_us;
  plan_gen = gen;
  int64_t left = plan_t - esp_timer_get_time();
  if (left <= SPIN_US) {
    fire_now(gen);                         // short delay: spin here (blocks loop() <= SPIN_US)
  } else {
    esp_timer_start_once(fire_timer, (uint64_t)(left - SPIN_US));
  }
}

static void disarm() {
  arm_gen = arm_gen + 1u;
  esp_timer_stop(fire_timer);
  arm_kind = ARM_NONE;
}

// --- output to the PC --------------------------------------------------------------------------
static void pc_line(const char *s) {
  Serial.print(s);
  Serial.print("\r\n");
}

static void emit_rx_line(const uint8_t *data, size_t n, int64_t t_done) {
  static char out[4 + STM_LINE_MAX * 4 + 1];
  static const char HEX_DIGITS[] = "0123456789ABCDEF";
  size_t o = 0;
  out[o++] = 'R';
  out[o++] = 'X';
  out[o++] = ' ';
  for (size_t i = 0; i < n; i++) {
    uint8_t c = data[i];
    if (c < 0x20 || c > 0x7E || c == '\\') {
      out[o++] = '\\';
      out[o++] = 'x';
      out[o++] = HEX_DIGITS[c >> 4];
      out[o++] = HEX_DIGITS[c & 15];
    } else {
      out[o++] = (char)c;
    }
  }
  out[o] = 0;
  if (timing_on) {
    char t[48];
    snprintf(t, sizeof t, "ESP RXT %lld", (long long)t_done);
    pc_line(t);
  }
  pc_line(out);
}

static void report_fire() {
  if (!fire_done) return;
  fire_done = false;
  fired++;
  char s[80];
  snprintf(s, sizeof s, "ESP NRST FIRED %lu %lld", (unsigned long)fire_delay, (long long)fire_late);
  pc_line(s);
  snprintf(s, sizeof s, "ESP ARMJITTER %lld", (long long)fire_late);
  pc_line(s);
}

// --- STM32 side --------------------------------------------------------------------------------
static void stm_line_done(int64_t t_done) {
  size_t n = stm_len;
  stm_len = 0;
  if (n && stm_buf[n - 1] == '\r') n--;
  if (n == 0) return;
  rx_lines++;
  emit_rx_line(stm_buf, n, t_done);
  if (arm_kind == ARM_RX) {
    size_t pl = strlen(arm_prefix);
    if (n >= pl && memcmp(stm_buf, arm_prefix, pl) == 0) {
      arm_kind = ARM_NONE;
      schedule_fire(t_done, arm_delay_us);
    }
  }
}

static void poll_stm() {
  int avail = Serial1.available();
  while (avail-- > 0) {
    int c = Serial1.read();
    if (c < 0) break;
    if (c == '\n') {
      stm_line_done(esp_timer_get_time());
      continue;
    }
    stm_buf[stm_len++] = (uint8_t)c;
    if (stm_len >= STM_LINE_MAX) {         // no newline for 1 KB: emit what there is
      int64_t t = esp_timer_get_time();
      rx_lines++;
      emit_rx_line(stm_buf, stm_len, t);
      stm_len = 0;
    }
  }
}

static void on_stm_error(hardwareSerial_error_t e) {
  if (e == UART_FIFO_OVF_ERROR || e == UART_BUFFER_FULL_ERROR) overruns = overruns + 1u;
}

// One TX/TXH to the STM32. Waits for the stop bit only when it matters (armed or TIME ON).
static void stm_send(const uint8_t *data, size_t n) {
  bool trigger = false;
  if (arm_kind == ARM_TX) {
    if (arm_count <= 1) {
      trigger = true;
      arm_kind = ARM_NONE;
    } else {
      arm_count--;
    }
  }
  Serial1.write(data, n);                  // no TX ring buffer: returns when all bytes are in the FIFO
  tx_lines++;
  if (trigger || timing_on) {
    Serial1.flush();                       // spins until the TX shifter is idle
    int64_t t_done = esp_timer_get_time();
    if (trigger) schedule_fire(t_done, arm_delay_us);
    if (timing_on) {
      char s[64];
      snprintf(s, sizeof s, "ESP TXDONE %lu %lld", (unsigned long)tx_lines, (long long)t_done);
      pc_line(s);
    }
  }
}

// --- PC commands --------------------------------------------------------------------------------
static bool parse_u32(const char *s, uint32_t *out, const char **end) {
  uint64_t v = 0;
  const char *p = s;
  if (*p < '0' || *p > '9') return false;
  while (*p >= '0' && *p <= '9') {
    v = v * 10 + (uint64_t)(*p - '0');
    if (v > 0xFFFFFFFFull) return false;
    p++;
  }
  *out = (uint32_t)v;
  *end = p;
  return true;
}

static int hexval(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

static void cmd_selftest() {
  // Timer-only: schedule one-shots like an armed fire, measure how late they wake. NRST untouched.
  const int N = 20;
  int64_t mn = INT64_MAX, mx = INT64_MIN, sum = 0;
  for (int i = 0; i < N; i++) {
    uint32_t d = 50 + (uint32_t)i * 250;
    int64_t plan = esp_timer_get_time() + d;
    if ((int64_t)d > SPIN_US) {
      int64_t wake = plan - SPIN_US;
      while (esp_timer_get_time() < wake) {
        delayMicroseconds(20);
      }
    }
    while (esp_timer_get_time() < plan) {
    }
    int64_t late = esp_timer_get_time() - plan;
    mn = late < mn ? late : mn;
    mx = late > mx ? late : mx;
    sum += late;
  }
  char s[96];
  snprintf(s, sizeof s, "ESP SELFTEST n=%d late_us min=%lld avg=%lld max=%lld", N, (long long)mn,
           (long long)(sum / N), (long long)mx);
  pc_line(s);
}

static void handle_pc_line(char *line) {
  if (strcmp(line, "PING") == 0) {
    pc_line("ESP PONG " BRIDGE_ID " " BRIDGE_VERSION);
  } else if (strcmp(line, "TX") == 0 || strncmp(line, "TX ", 3) == 0) {
    static uint8_t buf[PC_LINE_MAX + 2];
    const char *t = line[2] ? line + 3 : "";
    size_t n = strlen(t);
    memcpy(buf, t, n);
    buf[n++] = '\r';
    buf[n++] = '\n';
    stm_send(buf, n);
  } else if (strncmp(line, "TXH ", 4) == 0) {
    static uint8_t buf[PC_LINE_MAX / 2];
    const char *h = line + 4;
    size_t hl = strlen(h), n = 0;
    if (hl == 0 || hl % 2) {
      pc_line("ESP ERR TXH");
      return;
    }
    for (size_t i = 0; i < hl; i += 2) {
      int a = hexval(h[i]), b = hexval(h[i + 1]);
      if (a < 0 || b < 0) {
        pc_line("ESP ERR TXH");
        return;
      }
      buf[n++] = (uint8_t)(a * 16 + b);
    }
    stm_send(buf, n);
  } else if (strncmp(line, "NRST PULSE ", 11) == 0) {
    uint32_t us;
    const char *e;
    if (!parse_u32(line + 11, &us, &e) || *e || us < 10 || us > 1000000) {
      pc_line("ESP ERR PULSE");
      return;
    }
    gpio_set_level(NRST_PIN, 0);
    int64_t end = esp_timer_get_time() + (int64_t)us;
    while (end - esp_timer_get_time() > 3000) {
      delay(1);                            // long pulse: let the other tasks run
    }
    while (esp_timer_get_time() < end) {
    }
    nrst_release();
    pc_line("ESP OK PULSE");
  } else if (strncmp(line, "NRST ARM TX ", 12) == 0) {
    uint32_t d, n = 1;
    const char *e;
    if (!parse_u32(line + 12, &d, &e) || (*e && *e != ' ')) {
      pc_line("ESP ERR ARM");
      return;
    }
    if (*e == ' ' && (!parse_u32(e + 1, &n, &e) || *e || n < 1)) {
      pc_line("ESP ERR ARM");
      return;
    }
    disarm();
    arm_kind = ARM_TX;
    arm_delay_us = d;
    arm_count = n;
    pc_line("ESP OK ARM");
  } else if (strncmp(line, "NRST ARM RX ", 12) == 0) {
    uint32_t d;
    const char *e;
    if (!parse_u32(line + 12, &d, &e) || *e != ' ' || !e[1] || strlen(e + 1) >= sizeof arm_prefix) {
      pc_line("ESP ERR ARM");
      return;
    }
    disarm();
    arm_kind = ARM_RX;
    arm_delay_us = d;
    strcpy(arm_prefix, e + 1);
    pc_line("ESP OK ARM");
  } else if (strcmp(line, "NRST DISARM") == 0) {
    disarm();
    pc_line("ESP OK DISARM");
  } else if (strcmp(line, "STAT") == 0) {
    char s[128];
    snprintf(s, sizeof s, "ESP STAT rx_lines=%lu tx_lines=%lu fired=%lu overruns=%lu", (unsigned long)rx_lines,
             (unsigned long)tx_lines, (unsigned long)fired, (unsigned long)overruns);
    pc_line(s);
  } else if (strcmp(line, "TIME ON") == 0 || strcmp(line, "TIME OFF") == 0) {
    timing_on = line[6] == 'N';
    pc_line("ESP OK TIME");
  } else if (strcmp(line, "SELFTEST") == 0) {
    cmd_selftest();
  } else if (line[0]) {
    pc_line("ESP ERR unknown command");
  }
}

static void poll_pc() {
  while (Serial.available() > 0) {
    int c = Serial.read();
    if (c < 0) break;
    if (c == '\n') {
      if (pc_len && pc_buf[pc_len - 1] == '\r') pc_len--;
      pc_buf[pc_len] = 0;
      if (pc_overflow) {
        pc_line("ESP ERR LINE");
      } else {
        handle_pc_line(pc_buf);
      }
      pc_len = 0;
      pc_overflow = false;
      continue;
    }
    if (pc_len < PC_LINE_MAX) {
      pc_buf[pc_len++] = (char)c;
    } else {
      pc_overflow = true;
    }
    poll_stm();                            // keep STM32 RX timestamps fresh during a long command line
  }
}

// --- Arduino -------------------------------------------------------------------------------------
void setup() {
  // UART1 first, so the STM32's RX (PA10) sees an idle-high line as early as possible.
  Serial1.setRxBufferSize(4096);
  Serial1.setTxBufferSize(0);              // write() goes straight to the FIFO (see Timing above)
  Serial1.begin(STM_BAUD, SERIAL_8N1, STM_RX_PIN, STM_TX_PIN);
  Serial1.setRxFIFOFull(1);                // every byte reaches the ring buffer at once
  Serial1.onReceiveError(on_stm_error);
  gpio_pullup_en((gpio_num_t)STM_RX_PIN);  // the STM32's TX floats while it is in reset

  nrst_init();

  esp_timer_create_args_t ta = {};
  ta.callback = fire_timer_cb;
  ta.dispatch_method = ESP_TIMER_TASK;
  ta.name = "nrst";
  esp_timer_create(&ta, &fire_timer);

  Serial.setRxBufferSize(4096);
  Serial.setTxBufferSize(8192);
  Serial.begin(PC_BAUD);
  delay(20);
  pc_line("");
  pc_line("ESP READY " BRIDGE_ID " " BRIDGE_VERSION);
}

void loop() {
  poll_stm();
  poll_pc();
  report_fire();
}
