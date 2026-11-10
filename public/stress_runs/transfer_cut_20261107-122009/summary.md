# Stress test `transfer_cut` (ab)

**PASS**: 10/10 trials met the pass criterion

- trials: 10, seed: 11, commit: `1e8853f273bbd43a57c3a8993640c8974be7e824`
- firmware manifest sha256: `eba96441fe6c158b7d07785feccc3868849a460dc27de3a990c32b5395562b66`
- reproduce: `python -m stress --json run transfer_cut --n 10 --seed 11 --design ab --target c031 --baseline any`

## Outcomes

| outcome | count |
|---|---|
| ESKİ | 10 |

## Failure rate

| measure | k / n | rate | 95% upper bound | method |
|---|---|---|---|---|
| pass criterion failed | 0 / 10 | 0.00% | 25.89% | exact zero-failure bound 1-0.05^(1/N); 3/N = 30.00% |
| unsafe (ÖLÜ+YANLIŞ) | 0 / 10 | 0.00% | 25.89% | exact zero-failure bound 1-0.05^(1/N); 3/N = 30.00% |

## Phase x outcome

| phase | ESKİ |
|---|---|
| cut | 10 |

## Timing (s)

| measure | n | median | p95 | max |
|---|---|---|---|---|

- retries (total): 0
- trials where a fault fired: 0
- trials that needed the ST-Link (recovery): 0

## Wear (erase counts after the last trial)

| counter | value |
|---|---|
| slot_erases_A | 89 |
| slot_erases_B | 66 |
| meta_erases0 | 0 |
| meta_erases1 | 0 |
