# Stress test `reset_sweep` (ab)

**PASS**: 1479/1479 trials met the pass criterion

> Interrupted (Ctrl-C): partial results.

- trials: 1479, seed: 2027, commit: `6939fa3cd4bf55c960911ad9a5d59ff9dece3ad5`
- firmware manifest sha256: `eba96441fe6c158b7d07785feccc3868849a460dc27de3a990c32b5395562b66`
- reproduce: `python -m stress --json run reset_sweep --n 2400 --seed 2027 --design ab --target c031 --reset-source esp --baseline any --max-points-per-phase 800`

## Outcomes

| outcome | count |
|---|---|
| BEKLİYOR | 1018 |
| YENİ | 461 |

## Failure rate

| measure | k / n | rate | 95% upper bound | method |
|---|---|---|---|---|
| pass criterion failed | 0 / 1479 | 0.00% | 0.20% | exact zero-failure bound 1-0.05^(1/N); 3/N = 0.20% |
| unsafe (ÖLÜ+YANLIŞ) | 0 / 1479 | 0.00% | 0.20% | exact zero-failure bound 1-0.05^(1/N); 3/N = 0.20% |

## Phase x outcome

| phase | BEKLİYOR | YENİ |
|---|---|---|
| DATA | 511 | 0 |
| ERASE | 492 | 0 |
| HDR | 15 | 461 |

## Timing (s)

| measure | n | median | p95 | max |
|---|---|---|---|---|
| update_s | 461 | 2.982 | 3.414 | 3.461 |
| resume_s | 1018 | 3.780 | 3.866 | 3.889 |
| throughput_Bps | 461 | 4758.600 | 4764.600 | 4840.900 |

- retries (total): 319
- trials where a fault fired: 1479
- trials that needed the ST-Link (recovery): 0

## Wear (erase counts after the last trial)

| counter | value |
|---|---|
| slot_erases_A | 2670 |
| slot_erases_B | 2705 |
| meta_erases0 | 74 |
| meta_erases1 | 74 |
