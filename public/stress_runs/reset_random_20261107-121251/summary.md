# Stress test `reset_random` (ab)

**PASS**: 20/20 trials met the pass criterion

- trials: 20, seed: 7, commit: `1e8853f273bbd43a57c3a8993640c8974be7e824`
- firmware manifest sha256: `eba96441fe6c158b7d07785feccc3868849a460dc27de3a990c32b5395562b66`
- reproduce: `python -m stress --json run reset_random --n 20 --seed 7 --design ab --target c031 --reset-source esp --baseline any`

## Outcomes

| outcome | count |
|---|---|
| BEKLİYOR | 19 |
| YENİ | 1 |

## Failure rate

| measure | k / n | rate | 95% upper bound | method |
|---|---|---|---|---|
| pass criterion failed | 0 / 20 | 0.00% | 13.91% | exact zero-failure bound 1-0.05^(1/N); 3/N = 15.00% |
| unsafe (ÖLÜ+YANLIŞ) | 0 / 20 | 0.00% | 13.91% | exact zero-failure bound 1-0.05^(1/N); 3/N = 15.00% |

## Phase x outcome

| phase | BEKLİYOR | YENİ |
|---|---|---|
| ERASE | 19 | 1 |

## Timing (s)

| measure | n | median | p95 | max |
|---|---|---|---|---|
| update_s | 1 | 2.445 | 2.445 | 2.445 |
| resume_s | 19 | 3.861 | 3.868 | 3.872 |
| throughput_Bps | 1 | 4762.500 | 4762.500 | 4762.500 |

- retries (total): 20
- trials where a fault fired: 20
- trials that needed the ST-Link (recovery): 0

## Wear (erase counts after the last trial)

| counter | value |
|---|---|
| slot_erases_A | 51 |
| slot_erases_B | 45 |
| meta_erases0 | 0 |
| meta_erases1 | 1 |
