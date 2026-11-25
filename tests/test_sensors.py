import http.server
import threading
import unittest
from pathlib import Path

from fanloop.commands import IPMI_FANS, IPMI_POWER, IPMI_TEMPS, NVIDIA_QUERY, CmdResult
from fanloop.sensors import (
    Sensors,
    llama_health,
    parse_dcmi_power,
    parse_nvidia,
    parse_sdr_fans,
    parse_sdr_temps,
)
from tests.fakes import FakeRunner, fail, ok

FIX = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIX / name).read_text()


class TestParsers(unittest.TestCase):
    def test_nvidia_fixture(self):
        cards, errors = parse_nvidia(fixture("nvidia.txt"))
        self.assertEqual(errors, [])
        self.assertEqual([c.index for c in cards], [0, 1])
        c0 = cards[0]
        self.assertEqual((c0.temp_c, c0.power_w, c0.util_pct, c0.clock_mhz), (44, 26.62, 0, 585))
        self.assertEqual((c0.sw_power_cap, c0.sw_thermal, c0.hw_thermal), (False, False, False))

    def test_nvidia_active_flags(self):
        cards, _ = parse_nvidia("0, 84, 55.1, 99, 1035, Active, Active, Not Active\n")
        self.assertTrue(cards[0].sw_power_cap)
        self.assertTrue(cards[0].sw_thermal)
        self.assertFalse(cards[0].hw_thermal)

    def test_nvidia_broken_card_line(self):
        text = "0, 44, 26.62, 0, 585, Not Active, Not Active, Not Active\n1, [Unknown Error], [N/A], [N/A], [N/A], [N/A], [N/A], [N/A]\n"
        cards, errors = parse_nvidia(text)
        self.assertEqual([c.index for c in cards], [0])
        self.assertEqual(len(errors), 1)
        self.assertIn("Unknown Error", errors[0])

    def test_nvidia_na_power_drops_card(self):
        text = "0, 44, [N/A], 0, 585, Not Active, Not Active, Not Active\n1, 44, 28.43, 0, 585, Not Active, Not Active, Not Active\n"
        cards, errors = parse_nvidia(text)
        self.assertEqual([c.index for c in cards], [1])
        self.assertEqual(len(errors), 1)

    def test_nvidia_garbage(self):
        cards, errors = parse_nvidia("Failed to initialize NVML: Driver/library version mismatch\n")
        self.assertEqual(cards, [])
        self.assertEqual(len(errors), 1)

    def test_sdr_temps(self):
        self.assertEqual(parse_sdr_temps(fixture("sdr_temperature.txt")), (14.0, 20.0))
        self.assertEqual(parse_sdr_temps("Inlet Temp | 04h | ns | 7.1 | No Reading\n"), (None, None))

    def test_sdr_fans(self):
        fans = parse_sdr_fans(fixture("sdr_fan.txt"))
        self.assertEqual(len(fans), 6)
        self.assertEqual(fans["Fan1"], 7200)
        self.assertNotIn("Fan Redundancy", fans)

    def test_dcmi_power(self):
        self.assertEqual(parse_dcmi_power(fixture("dcmi_power.txt")), 236.0)
        self.assertIsNone(parse_dcmi_power("    Power reading state is: deactivated\n"))


class TestSensors(unittest.TestCase):
    def runner(self, nvidia):
        return FakeRunner({
            NVIDIA_QUERY: [nvidia],
            IPMI_TEMPS: [ok(fixture("sdr_temperature.txt"))],
            IPMI_FANS: [ok(fixture("sdr_fan.txt"))],
            IPMI_POWER: [ok(fixture("dcmi_power.txt"))],
        })

    def test_gpus(self):
        cards, error = Sensors(self.runner(ok(fixture("nvidia.txt")))).gpus()
        self.assertEqual(len(cards), 2)
        self.assertIsNone(error)

    def test_gpus_empty_output(self):
        cards, error = Sensors(self.runner(ok(""))).gpus()
        self.assertEqual(cards, [])
        self.assertTrue(error)

    def test_gpus_command_fails(self):
        cards, error = Sensors(self.runner(fail("NVIDIA-SMI has failed"))).gpus()
        self.assertEqual(cards, [])
        self.assertIn("NVIDIA-SMI has failed", error)

    def test_one_card_lost_with_a_nonzero_exit_keeps_the_other_card(self):
        # What a GPU that fell off the bus most likely looks like: one good line,
        # one error line, and nvidia-smi exiting non-zero.
        out = ("1, 61, 41.20, 87, 1395, Not Active, Not Active, Not Active\n"
               "Unable to determine the device handle for GPU0000:04:00.0: Unknown Error\n")
        cards, error = Sensors(self.runner(CmdResult(False, 15, out, "", False, False))).gpus()
        self.assertEqual([c.index for c in cards], [1])
        self.assertEqual(cards[0].temp_c, 61.0)
        self.assertIn("Unknown Error", error)

    def test_timed_out_nvidia_smi_gives_no_cards(self):
        hung = CmdResult(False, None, "", "timed out after 10 s", True, False)
        cards, error = Sensors(self.runner(hung)).gpus()
        self.assertEqual(cards, [])
        self.assertIn("timed out", error)

    def test_ipmi_and_power(self):
        sensors = Sensors(self.runner(ok(fixture("nvidia.txt"))))
        reading = sensors.ipmi()
        self.assertEqual((reading.inlet_c, reading.exhaust_c, reading.power_w), (14.0, 20.0, 236.0))
        self.assertGreaterEqual(len(reading.fans_rpm), 1)
        self.assertEqual(sensors.power(), 236.0)

    def test_ipmi_stops_after_a_hung_command(self):
        hung = CmdResult(False, None, "", "timed out after 8 s", True, False)
        runner = FakeRunner({IPMI_TEMPS: [hung], IPMI_FANS: [ok(fixture("sdr_fan.txt"))],
                             IPMI_POWER: [ok(fixture("dcmi_power.txt"))]})
        reading = Sensors(runner).ipmi()
        self.assertEqual(runner.calls, [IPMI_TEMPS])  # the BMC is hung: don't queue more on it
        self.assertEqual((reading.inlet_c, reading.fans_rpm, reading.power_w), (None, {}, None))

    def test_one_ipmi_read_stays_inside_its_time_budget(self):
        clock = [0.0]

        class SlowRunner(FakeRunner):
            def run(self, argv, timeout_s):
                clock[0] += timeout_s * 0.99  # slow, never quite timing out
                return super().run(argv, timeout_s)

        runner = SlowRunner({IPMI_TEMPS: [ok(fixture("sdr_temperature.txt"))], IPMI_FANS: [ok(fixture("sdr_fan.txt"))],
                             IPMI_POWER: [ok(fixture("dcmi_power.txt"))]})
        Sensors(runner, ipmi_timeout_s=15, clock=lambda: clock[0]).ipmi()
        self.assertLessEqual(clock[0], 15.0)

    def test_ipmi_failure_gives_empty_reading(self):
        reading = Sensors(FakeRunner({})).ipmi()
        self.assertEqual((reading.inlet_c, reading.exhaust_c, reading.fans_rpm, reading.power_w), (None, None, {}, None))


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


class TestLlamaHealth(unittest.TestCase):
    def serve(self, status):
        handler = type("H", (_Handler,), {"status": status})
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/health"

    def test_healthy(self):
        self.assertTrue(llama_health(self.serve(200)))

    def test_unhealthy_status(self):
        self.assertFalse(llama_health(self.serve(503)))

    def test_unreachable(self):
        self.assertFalse(llama_health("http://127.0.0.1:9/health", timeout_s=1.0))


if __name__ == "__main__":
    unittest.main()
