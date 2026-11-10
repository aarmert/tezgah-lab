# Stress test `reset_sweep` (ab)

**PASS**: 1493/1493 trials met the pass criterion

> Interrupted (Ctrl-C): partial results.

- trials: 1493, seed: 2026, commit: `6939fa3cd4bf55c960911ad9a5d59ff9dece3ad5`
- firmware manifest sha256: `eba96441fe6c158b7d07785feccc3868849a460dc27de3a990c32b5395562b66`
- reproduce: `python -m stress --json run reset_sweep --n 3001 --seed 2026 --design ab --target c031 --baseline any --max-points-per-phase 800`

## Outcomes

| outcome | count |
|---|---|
| BEKLİYOR | 960 |
| YENİ | 533 |

## Failure rate

| measure | k / n | rate | 95% upper bound | method |
|---|---|---|---|---|
| pass criterion failed | 0 / 1493 | 0.00% | 0.20% | exact zero-failure bound 1-0.05^(1/N); 3/N = 0.20% |
| unsafe (ÖLÜ+YANLIŞ) | 0 / 1493 | 0.00% | 0.20% | exact zero-failure bound 1-0.05^(1/N); 3/N = 0.20% |

## Phase x outcome

| phase | BEKLİYOR | YENİ |
|---|---|---|
| BOOT | 0 | 400 |
| CONFIRM | 0 | 108 |
| DATA | 408 | 0 |
| ERASE | 394 | 0 |
| HDR | 139 | 0 |
| META | 19 | 25 |

## Timing (s)

| measure | n | median | p95 | max |
|---|---|---|---|---|
| update_s | 533 | 2.464 | 3.472 | 3.497 |
| resume_s | 960 | 3.779 | 3.791 | 3.814 |
| throughput_Bps | 533 | 4765.400 | 4772.100 | 4828.600 |

- retries (total): 0
- trials where a fault fired: 1493
- trials that needed the ST-Link (recovery): 0

## Wear (erase counts after the last trial)

| counter | value |
|---|---|
| slot_erases_A | 1415 |
| slot_erases_B | 1463 |
| meta_erases0 | 36 |
| meta_erases1 | 36 |
