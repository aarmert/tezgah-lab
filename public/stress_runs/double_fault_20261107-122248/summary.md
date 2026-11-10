# Stress test `double_fault` (ab)

**PASS**: 10/10 trials met the pass criterion

- trials: 10, seed: 11, commit: `1e8853f273bbd43a57c3a8993640c8974be7e824`
- firmware manifest sha256: `eba96441fe6c158b7d07785feccc3868849a460dc27de3a990c32b5395562b66`
- reproduce: `python -m stress --json run double_fault --n 10 --seed 11 --design ab --target c031 --baseline any`

## Outcomes

| outcome | count |
|---|---|
| BEKLİYOR | 7 |
| ESKİ | 2 |
| YENİ | 1 |

## Failure rate

| measure | k / n | rate | 95% upper bound | method |
|---|---|---|---|---|
| pass criterion failed | 0 / 10 | 0.00% | 25.89% | exact zero-failure bound 1-0.05^(1/N); 3/N = 30.00% |
| unsafe (ÖLÜ+YANLIŞ) | 0 / 10 | 0.00% | 25.89% | exact zero-failure bound 1-0.05^(1/N); 3/N = 30.00% |

## Phase x outcome

| phase | BEKLİYOR | ESKİ | YENİ |
|---|---|---|---|
| BOOT+META | 0 | 1 | 0 |
| CONFIRM+BOOT | 0 | 0 | 1 |
| DATA+BOOT | 2 | 0 | 0 |
| ERASE+META | 1 | 0 | 0 |
| HDR+BOOT | 1 | 0 | 0 |
| HDR+META | 2 | 0 | 0 |
| META+BOOT | 1 | 1 | 0 |

## Timing (s)

| measure | n | median | p95 | max |
|---|---|---|---|---|
| update_s | 1 | 2.500 | 2.500 | 2.500 |
| resume_s | 7 | 3.779 | 3.804 | 3.804 |
| rollback_s | 2 | 0.194 | 0.204 | 0.205 |
| throughput_Bps | 3 | 4767.600 | 4770.660 | 4771.000 |

- retries (total): 0
- trials where a fault fired: 10
- trials that needed the ST-Link (recovery): 0

## Wear (erase counts after the last trial)

| counter | value |
|---|---|
| slot_erases_A | 103 |
| slot_erases_B | 84 |
| meta_erases0 | 0 |
| meta_erases1 | 1 |
