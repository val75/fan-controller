import dataclasses
import unittest

from fanloop.levels import RACADM_CODE, Level, level_from_name, parse_racadm_offset
from fanloop.readings import CardReading, IpmiReading


class TestLevels(unittest.TestCase):
    def test_order(self):
        self.assertEqual(sorted(Level), [Level.OFF, Level.LOW, Level.MEDIUM, Level.HIGH, Level.MAX])

    def test_racadm_codes(self):
        self.assertEqual(
            RACADM_CODE,
            {Level.OFF: "255", Level.LOW: "0", Level.MEDIUM: "2", Level.HIGH: "1", Level.MAX: "3"},
        )

    def test_parse(self):
        cases = {
            "FanSpeedOffset=Off": Level.OFF,
            "FanSpeedOffset=Low Fan Speed": Level.LOW,
            "FanSpeedOffset=Medium Fan Speed": Level.MEDIUM,
            "FanSpeedOffset=High Fan Speed": Level.HIGH,
            "FanSpeedOffset=Max Fan Speed": Level.MAX,
            "[Key=system.Embedded.1#ThermalSettings.1]\nFanSpeedOffset=Medium Fan Speed\n": Level.MEDIUM,
        }
        for text, level in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_racadm_offset(text), level)

    def test_parse_error_text(self):
        with self.assertRaises(ValueError):
            parse_racadm_offset("ERROR: Insufficient privilege level - You do not have")

    def test_level_from_name(self):
        self.assertEqual(level_from_name("Medium"), Level.MEDIUM)
        with self.assertRaises(ValueError):
            level_from_name("loud")


class TestReadings(unittest.TestCase):
    def test_card_reading_is_frozen(self):
        card = CardReading(0, 45.0, 28.0, 0.0, 585.0, False, False, False)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            card.temp_c = 50.0

    def test_ipmi_reading_fields(self):
        reading = IpmiReading(inlet_c=17.0, exhaust_c=23.0, fans_rpm={"Fan1": 7200}, power_w=238.0)
        self.assertEqual(reading.fans_rpm["Fan1"], 7200)
        self.assertIsNone(IpmiReading(None, None, {}, None).inlet_c)


if __name__ == "__main__":
    unittest.main()
