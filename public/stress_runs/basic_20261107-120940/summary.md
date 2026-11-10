# Stress test `basic` (ab)

**PASS**: 4/4 trials met the pass criterion

- trials: 4, seed: 1, commit: `1e8853f273bbd43a57c3a8993640c8974be7e824`
- firmware manifest sha256: `eba96441fe6c158b7d07785feccc3868849a460dc27de3a990c32b5395562b66`
- reproduce: `python -m stress --json run basic --n 4 --seed 1 --design ab --target c031 --baseline any`

## Outcomes

| outcome | count |
|---|---|
| YENİ | 4 |

## Failure rate

| measure | k / n | rate | 95% upper bound | method |
|---|---|---|---|---|
| pass criterion failed | 0 / 4 | 0.00% | 52.71% | exact zero-failure bound 1-0.05^(1/N); 3/N = 75.00% |
| unsafe (ÖLÜ+YANLIŞ) | 0 / 4 | 0.00% | 52.71% | exact zero-failure bound 1-0.05^(1/N); 3/N = 75.00% |

## Phase x outcome

| phase | YENİ |
|---|---|
| none | 4 |

## Timing (s)

| measure | n | median | p95 | max |
|---|---|---|---|---|
| update_s | 4 | 2.436 | 2.437 | 2.437 |
| throughput_Bps | 4 | 4768.450 | 4771.010 | 4771.400 |

- retries (total): 0
- trials where a fault fired: 0
- trials that needed the ST-Link (recovery): 0

## Wear (erase counts after the last trial)

| counter | value |
|---|---|
| slot_erases_A | 2 |
| slot_erases_B | 2 |
| meta_erases0 | 0 |
| meta_erases1 | 0 |
