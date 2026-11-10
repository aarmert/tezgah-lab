# Stress test `naive_control` (naive)

**PASS**: naive control showed 11/20 unsafe outcomes (ÖLÜ/YANLIŞ): the test can see failures

- trials: 20, seed: 7, commit: `1e8853f273bbd43a57c3a8993640c8974be7e824`
- firmware manifest sha256: `eba96441fe6c158b7d07785feccc3868849a460dc27de3a990c32b5395562b66`
- reproduce: `python -m stress --json run naive_control --n 20 --seed 7 --design naive --target c031 --reset-source esp --baseline any`

## Outcomes

| outcome | count |
|---|---|
| YANLIŞ | 11 |
| BEKLİYOR | 7 |
| YENİ | 2 |

## Failure rate

| measure | k / n | rate | 95% upper bound | method |
|---|---|---|---|---|
| pass criterion failed | 11 / 20 | 55.00% | 74.13% | Clopper-Pearson one-sided 95%; two-sided CP95 [31.53%, 76.94%] |
| unsafe (ÖLÜ+YANLIŞ) | 11 / 20 | 55.00% | 74.13% | Clopper-Pearson one-sided 95%; two-sided CP95 [31.53%, 76.94%] |

## Phase x outcome

| phase | YANLIŞ | BEKLİYOR | YENİ |
|---|---|---|---|
| DATA | 2 | 0 | 0 |
| ERASE | 8 | 7 | 0 |
| HDR | 1 | 0 | 2 |

## Timing (s)

| measure | n | median | p95 | max |
|---|---|---|---|---|
| update_s | 2 | 1.419 | 1.423 | 1.423 |
| recovery_s | 11 | 10.185 | 10.199 | 10.199 |
| resume_s | 7 | 2.754 | 2.762 | 2.764 |
| throughput_Bps | 2 | 4856.350 | 4860.265 | 4860.700 |

- retries (total): 0
- trials where a fault fired: 20
- trials that needed the ST-Link (recovery): 11

## Wear (erase counts after the last trial)

| counter | value |
|---|---|
| slot_erases_A | 89 |
| slot_erases_B | 56 |
| meta_erases0 | - |
| meta_erases1 | - |
