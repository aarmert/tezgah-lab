# Stress test `reset_sweep` (ab)

**PASS**: 57/57 trials met the pass criterion

- trials: 57, seed: 11, commit: `1e8853f273bbd43a57c3a8993640c8974be7e824`
- firmware manifest sha256: `eba96441fe6c158b7d07785feccc3868849a460dc27de3a990c32b5395562b66`
- reproduce: `python -m stress --json run reset_sweep --n 57 --seed 11 --design ab --target c031 --baseline any --max-points-per-phase 8 --meta-near-full`

## Outcomes

| outcome | count |
|---|---|
| BEKLİYOR | 27 |
| YENİ | 30 |

## Failure rate

| measure | k / n | rate | 95% upper bound | method |
|---|---|---|---|---|
| pass criterion failed | 0 / 57 | 0.00% | 5.12% | exact zero-failure bound 1-0.05^(1/N); 3/N = 5.26% |
| unsafe (ÖLÜ+YANLIŞ) | 0 / 57 | 0.00% | 5.12% | exact zero-failure bound 1-0.05^(1/N); 3/N = 5.26% |

## Phase x outcome

| phase | BEKLİYOR | YENİ |
|---|---|---|
| BOOT | 0 | 8 |
| CONFIRM | 0 | 8 |
| DATA | 8 | 0 |
| ERASE | 8 | 0 |
| HDR | 8 | 0 |
| JUMP | 0 | 1 |
| META | 3 | 5 |
| ROTATE | 0 | 8 |

## Timing (s)

| measure | n | median | p95 | max |
|---|---|---|---|---|
| update_s | 30 | 2.496 | 3.493 | 3.493 |
| resume_s | 27 | 3.802 | 3.805 | 3.806 |
| throughput_Bps | 30 | 4765.500 | 4769.355 | 4771.700 |

- retries (total): 0
- trials where a fault fired: 57
- trials that needed the ST-Link (recovery): 0

## Wear (erase counts after the last trial)

| counter | value |
|---|---|
| slot_erases_A | 182 |
| slot_erases_B | 243 |
| meta_erases0 | 0 |
| meta_erases1 | 1 |
