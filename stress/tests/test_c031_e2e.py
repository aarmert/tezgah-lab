"""End-to-end `--target c031 --sim` runs through the CLI: the C031 sim behind the simulated ESP32 bridge
(--link esp, the default on c031) with the VCP witness, --link vcp, and --reset-source esp."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from stress.cli import main
from stress.csvlog import note_kv, read_rows

TMP = None
CFG = None


def setUpModule():
    global TMP, CFG
    TMP = Path(tempfile.mkdtemp(prefix="stress_c031_"))
    CFG = TMP / "stress.toml"
    CFG.write_text(f'[stress]\nsalt = "t"\nruns_dir = "{(TMP / "runs").as_posix()}"\n', encoding="utf-8")


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def cli(*args, cfg=None) -> dict:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["--json", "--stress-config", str(cfg or CFG), *args])
    return json.loads(out.getvalue())


def rows_of(r: dict) -> list[dict]:
    return read_rows(Path(r["report_dir"]) / "trials.csv")


class C031Sim(unittest.TestCase):
    def run_ok(self, *args):
        r = cli("run", *args, "--sim", "--target", "c031")
        self.assertTrue(r["ok"], r)
        rows = rows_of(r)
        self.assertTrue(rows)
        self.assertFalse(any("witness_mismatch" in x["note"] for x in rows), [x["note"] for x in rows])
        return r, rows

    def test_basic_and_run_json(self):
        r, rows = self.run_ok("basic", "--n", "3")
        self.assertEqual({x["outcome"] for x in rows}, {"NEW"})
        self.assertTrue(all("target=c031" in x["bl_line"] for x in rows))
        d = Path(r["report_dir"])
        run = json.loads((d / "run.json").read_text(encoding="utf-8"))
        self.assertEqual((run["target"], run["link"], run["reset_source"]), ("c031", "esp", "hook"))
        self.assertIn("--target c031", run["reproduce"])
        self.assertEqual(run["versions"]["esp_bridge"], "tezgah-bridge 0.1")
        log = (d / "serial.log").read_text(encoding="utf-8")
        self.assertIn(" V BL v0.1 design=ab hooks target=c031", log)      # the witness, direction V
        self.assertIn(" E ESP READY tezgah-bridge 0.1", log)

    def test_meta_rotates_every_16_to_21_updates(self):
        _, rows = self.run_ok("basic", "--n", "24")
        e1 = [int(x["meta_erases1"] or 0) for x in rows]
        first = next(i for i, v in enumerate(e1) if v >= 1)
        self.assertTrue(15 <= first <= 21, e1)

    def test_transfer_cut_noise_bad_image_rollback(self):
        _, rows = self.run_ok("transfer_cut", "--n", "6")
        self.assertEqual({x["outcome"] for x in rows}, {"OLD"})
        _, rows = self.run_ok("line_noise", "--n", "6", "--rate", "0.25")
        self.assertEqual({x["outcome"] for x in rows}, {"NEW"})
        self.assertGreater(sum(int(x["retries"]) for x in rows), 0)
        _, rows = self.run_ok("bad_image", "--n", "5")
        got = {x["image"].rsplit("_", 1)[0]: x["err_code"] for x in rows}
        self.assertEqual(got, {"bad_crc": "IMG_CRC", "wrong_board": "BOARD", "truncated": "SHORT",
                               "oversize": "SIZE", "old": "VERSION"})
        _, rows = self.run_ok("rollback", "--n", "3")
        self.assertEqual({x["outcome"] for x in rows}, {"OLD"})
        self.assertEqual({x["attempts"] for x in rows}, {"3"})

    def test_reset_tests_hook_all_accepted(self):
        for test, n in (("reset_random", "10"), ("reset_sweep", "24"), ("double_fault", "10")):
            _, rows = self.run_ok(test, "--n", n)
            self.assertTrue(all(x["accepted"] == "1" for x in rows), (test, [x["note"] for x in rows]))
            self.assertGreater(sum(int(x["fault_fired"] or 0) for x in rows), 0, test)
            self.assertFalse(any("unexpected_reset" in x["note"] for x in rows), test)
        cal = json.loads((TMP / "runs" / "calibration_sim_c031.json").read_text(encoding="utf-8"))["ab"]
        self.assertEqual(cal["target"], "c031")
        self.assertEqual(sorted(cal["phases"]["ERASE"]), ["12", "8"])          # first page of each slot
        self.assertGreater(min(cal["phases"]["ERASE"].values()), 4 * 18_000)   # four 2 KB pages
        self.assertIn("esp", cal)
        self.assertFalse((TMP / "runs" / "calibration_sim.json").exists())     # the F407 file is separate

    def test_meta_near_full_rotation(self):
        _, rows = self.run_ok("reset_sweep", "--n", "40", "--meta-near-full")
        self.assertTrue(all(x["accepted"] == "1" for x in rows))
        rot = [x for x in rows if x["phase"] == "ROTATE"]
        self.assertTrue(rot and all(int(x["fault_fired"]) >= 1 for x in rot), rot)

    def test_naive_control_sees_failures(self):
        r = cli("run", "naive_control", "--n", "8", "--sim", "--target", "c031")
        self.assertTrue(r["ok"], r)
        self.assertTrue(any(x["outcome"] in ("DEAD", "WRONG") for x in rows_of(r)))

    def test_reset_source_esp(self):
        for test, n in (("reset_random", "10"), ("reset_sweep", "20")):
            _, rows = self.run_ok(test, "--n", n, "--reset-source", "esp")
            self.assertEqual({x["fault_type"] for x in rows}, {"nrst_esp"})
            self.assertTrue(all(x["accepted"] == "1" for x in rows), [x["note"] for x in rows])
            self.assertTrue(all("reset_source=esp" in x["note"] for x in rows))
            fired = [x for x in rows if int(x["fault_fired"] or 0) >= 1]
            self.assertGreater(len(fired), len(rows) // 2, [x["note"] for x in rows])
            self.assertTrue(all("late_us" in note_kv(x["note"]) for x in fired))
            self.assertFalse(any("unexpected_reset" in x["note"] for x in rows))
            self.assertTrue({x["phase"] for x in rows} <= {"ERASE", "DATA", "HDR"})
        r = cli("run", "naive_control", "--n", "8", "--sim", "--target", "c031", "--reset-source", "esp")
        self.assertTrue(r["ok"], r)
        rows = rows_of(r)
        self.assertTrue(any(x["outcome"] in ("DEAD", "WRONG") for x in rows))
        self.assertEqual({x["fault_type"] for x in rows}, {"nrst_esp"})

    def test_link_vcp(self):
        r, rows = self.run_ok("reset_random", "--n", "4", "--link", "vcp")
        self.assertTrue(all(x["accepted"] == "1" for x in rows))
        run = json.loads((Path(r["report_dir"]) / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["link"], "vcp")
        self.assertIsNone(run["versions"]["esp_bridge"])
        self.assertNotIn(" V BL", (Path(r["report_dir"]) / "serial.log").read_text(encoding="utf-8"))

    def test_f407_over_esp_bridge(self):
        r = cli("run", "basic", "--n", "2", "--sim", "--link", "esp")
        self.assertTrue(r["ok"], r)
        self.assertTrue(all("target=f407" in x["bl_line"] for x in rows_of(r)))

    def test_bad_arguments(self):
        for args, err in ((["reset_random", "--link", "vcp", "--reset-source", "esp"], "bad_argument"),
                          (["double_fault", "--n", "2", "--reset-source", "esp"], "bad_argument"),
                          (["basic", "--n", "1", "--reset-source", "esp"], "bad_argument")):
            r = cli("run", *args, "--sim", "--target", "c031")
            self.assertEqual(r["error_type"], err, (args, r))

    def test_plan_and_doctor(self):
        p = cli("plan", "reset_sweep", "--n", "4", "--sim", "--target", "c031", "--reset-source", "esp")
        self.assertTrue(p["ok"], p)
        self.assertEqual((p["target"], p["reset_source"]), ("c031", "esp"))
        self.assertTrue(all(x["fault_type"] == "nrst_esp" for x in p["plans"]))
        doc = cli("doctor", "--sim", "--target", "c031")
        self.assertTrue(doc["ok"], doc)
        checks = {c["check"]: c for c in doc["checks"]}
        self.assertTrue(checks["esp PING"]["ok"])
        self.assertTrue(checks["images target"]["ok"])

    def test_doctor_hardware_config_no_hardware(self):
        """No bench-c031.toml: the C031 never falls back to bench.toml (the F407 bench)."""
        cfg = TMP / "stress_hw.toml"
        cfg.write_text(f'[stress]\nruns_dir = "{(TMP / "runs_hw").as_posix()}"\n'
                       f'[c031]\nbench_config = "{(TMP / "missing-bench-c031.toml").as_posix()}"\n'
                       f'images_dir = "{(TMP / "no_images").as_posix()}"\n'
                       '[esp]\nport = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_TEST-if00"\n', encoding="utf-8")
        doc = cli("doctor", "--target", "c031", "--no-hardware", cfg=cfg)
        self.assertFalse(doc["ok"])
        checks = {c["check"]: c for c in doc["checks"]}
        self.assertIn("missing-bench-c031.toml", checks["bench.toml"]["detail"])
        self.assertIn("skipped", checks["esp PING"]["detail"])
        self.assertTrue(checks["esp port configured"]["ok"])
        bench_toml = TMP / "bench-c031.toml"
        bench_toml.write_text('[bench]\nname = "t"\n[probe]\nbackend = "openocd"\n'
                              'openocd_target = "target/stm32c0x.cfg"\n[serial]\nport = "/dev/null"\n',
                              encoding="utf-8")
        doc = cli("doctor", "--target", "c031", "--no-hardware", "--config", str(bench_toml), cfg=cfg)
        checks = {c["check"]: c for c in doc["checks"]}
        self.assertTrue(checks["bench.toml"]["ok"], checks["bench.toml"])
        self.assertTrue(checks["openocd target"]["ok"])


if __name__ == "__main__":
    unittest.main()


class HardwarePathWiring(unittest.TestCase):
    """The non---sim branch of open_bench (bench config, SerialTransport on the ESP port + the VCP
    witness, OpenOCD programmer), with bench's classes replaced by sim-backed fakes. Nothing real
    is opened."""

    def test_run_without_sim_flag(self):
        from unittest import mock

        from stress import cli as cli_mod
        from stress.layout_c031 import LAYOUT as C031
        from stress.sim import SimBoard, SimClock, SimProgrammer, SimSerial
        from stress.sim_esp import SimEspPort
        from stress.simimages import write_image_set

        imgs = write_image_set(TMP / "hw_images", C031)
        board = SimBoard(seed=9, golden=(imgs / "golden_ab.bin").read_bytes(), layout=C031)
        tap = board.add_tap()
        opened = []

        class Cfg:
            path = TMP / "bench-c031.toml"

            def get(self, sec, key, default=None):
                return {("serial", "port"): "/dev/vcp-test", ("serial", "baud"): 115200,
                        ("bench", "name"): "fake-c031", ("probe", "backend"): "openocd"}.get((sec, key), default)

        class Prog(SimProgrammer):
            name = "openocd"

        def serial_transport(port, baud):
            opened.append((port, baud))
            if port == "/dev/esp-test":
                self.assertEqual(baud, 921600)
                return SimEspPort(board)
            return SimSerial(board, tap)

        def load_config(path, sim):
            self.assertTrue(str(path).endswith("bench-c031.toml"), path)
            return Cfg()

        (TMP / "bench-c031.toml").write_text("# fake\n", encoding="utf-8")
        cfg = TMP / "stress_hwpath.toml"
        cfg.write_text(f'[stress]\nsalt = "t"\nruns_dir = "{(TMP / "runs_hwpath").as_posix()}"\n'
                       f'[c031]\nimages_dir = "{imgs.as_posix()}"\n'
                       f'bench_config = "{(TMP / "bench-c031.toml").as_posix()}"\n'
                       '[esp]\nport = "/dev/esp-test"\n', encoding="utf-8")
        b = cli_mod.bench
        with mock.patch.object(b, "load_config", load_config), \
                mock.patch.object(b, "BenchLock", lambda cfg: mock.MagicMock()), \
                mock.patch.object(b, "make_programmer", lambda cfg, board_: Prog(board)), \
                mock.patch.object(b, "SerialTransport", serial_transport), \
                mock.patch.object(b, "probe_serial", lambda cfg: "fake"), \
                mock.patch.object(cli_mod, "RealClock", lambda: SimClock(board)):
            r = cli("run", "basic", "--n", "2", "--target", "c031", cfg=cfg)
        self.assertTrue(r["ok"], r)
        self.assertEqual(sorted(opened), [("/dev/esp-test", 921600), ("/dev/vcp-test", 115200)])
        rows = rows_of(r)
        self.assertEqual({x["outcome"] for x in rows}, {"NEW"})
        self.assertFalse(any("witness_mismatch" in x["note"] for x in rows))
        log = (Path(r["report_dir"]) / "serial.log").read_text(encoding="utf-8")
        self.assertIn(" V BOOT app", log)
        run = json.loads((Path(r["report_dir"]) / "run.json").read_text(encoding="utf-8"))
        self.assertEqual((run["bench_name"], run["target"], run["link"]), ("fake-c031", "c031", "esp"))
