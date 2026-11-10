# Stress test `reset_sweep` (ab)

**PASS**: 24/24 trials met the pass criterion

- trials: 24, seed: 11, commit: `1e8853f273bbd43a57c3a8993640c8974be7e824`
- firmware manifest sha256: `eba96441fe6c158b7d07785feccc3868849a460dc27de3a990c32b5395562b66`
- reproduce: `python -m stress --json run reset_sweep --n 24 --seed 11 --design ab --target c031 --reset-source esp --baseline any --max-points-per-phase 8`

## Outcomes

| outcome | count |
|---|---|
| BEKLİYOR | 16 |
| ESKİ | 1 |
| YENİ | 7 |

## Failure rate

| measure | k / n | rate | 95% upper bound | method |
|---|---|---|---|---|
| pass criterion failed | 0 / 24 | 0.00% | 11.73% | exact zero-failure bound 1-0.05^(1/N); 3/N = 12.50% |
| unsafe (ÖLÜ+YANLIŞ) | 0 / 24 | 0.00% | 11.73% | exact zero-failure bound 1-0.05^(1/N); 3/N = 12.50% |

## Phase x outcome

| phase | BEKLİYOR | ESKİ | YENİ |
|---|---|---|---|
| DATA | 8 | 0 | 0 |
| ERASE | 7 | 1 | 0 |
| HDR | 1 | 0 | 7 |

## Timing (s)

| measure | n | median | p95 | max |
|---|---|---|---|---|
| update_s | 7 | 2.863 | 3.273 | 3.318 |
| resume_s | 16 | 3.779 | 3.865 | 3.865 |
| throughput_Bps | 7 | 4759.200 | 4764.340 | 4764.700 |

- retries (total): 8
- trials where a fault fired: 24
- trials that needed the ST-Link (recovery): 0

## Wear (erase counts after the last trial)

| counter | value |
|---|---|
| slot_erases_A | 125 |
| slot_erases_B | 102 |
| meta_erases0 | 1 |
| meta_erases1 | 1 |
