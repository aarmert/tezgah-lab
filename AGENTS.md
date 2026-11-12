# Agent rules (tezgah-lab, umbrella)

This repo only holds two git submodules and a README. There is no code here.

| Folder | What | Rules |
|---|---|---|
| `f407-hil-bench/` | F407 bench: `bench.py`, `plans/`, test firmware, `runs/` | [`f407-hil-bench/AGENTS.md`](f407-hil-bench/AGENTS.md) |
| `c031-ab-bootloader-stress/` | C031 bootloader stress test: `python -m stress`, bootloaders, ESP32 bridge | [`c031-ab-bootloader-stress/AGENTS.md`](c031-ab-bootloader-stress/AGENTS.md) |

- Work inside the submodule the task belongs to: `cd` there, run commands with its own `uv run`, and
  commit there. Then commit the updated submodule pointer in this repo.
- Touch a board only as that submodule's `AGENTS.md` allows (F407: `bench.py`; C031: `python -m stress`).
  Everything prohibited there (changing tests or bench files, running `openocd` or
  `STM32_Programmer_CLI` directly, uploading to the ESP32, option bytes / RDP / mass erase, using the
  SWD pins) is prohibited from here too.
- `bench.py` is maintained in `f407-hil-bench`; the copy in `c031-ab-bootloader-stress` must stay identical.
