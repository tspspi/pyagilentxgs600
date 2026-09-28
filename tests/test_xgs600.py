import unittest

import serial

from agilentxgs600 import XGS600_RS232, XGS600CommunicationError, XGS600ProtocolError, XGS600SetpointMode
from labdevices.pressuregauge import PressureGaugeUnit


class FakeSerial(serial.SerialBase):
    def __init__(self, responses):
        self.is_open = True
        self.responses = list(responses)
        self.writes = []

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def read_until(self, expected, size):
        return self.responses.pop(0)


class XGS600Tests(unittest.TestCase):
    def test_pressure_and_units(self):
        port = FakeSerial([b">00\r", b">1.500E-03\r", b">00\r"])
        gauge = XGS600_RS232(port, sensor = "T1")

        measurement = gauge.get_measurement()

        self.assertEqual(port.writes, [b"#0013\r", b"#0002T1\r", b"#0013\r"])
        self.assertEqual(measurement["pressureUnit"], PressureGaugeUnit.TORR)
        self.assertAlmostEqual(measurement["pressure_mbar"], 0.001999835526)
        self.assertTrue(measurement["responseValid"])

    def test_unavailable_pressure_fails_closed(self):
        port = FakeSerial([b">01\r", b">NOFIL1\r", b">01\r"])
        gauge = XGS600_RS232(port, sensor = "I1")

        with self.assertRaises(XGS600ProtocolError):
            gauge.get_pressure()

    def test_timeout_and_controller_error(self):
        for response, error in ((b"", XGS600CommunicationError), (b"?FF\r", XGS600ProtocolError)):
            with self.subTest(response = response):
                gauge = XGS600_RS232(FakeSerial([response]), sensor = "T1")
                with self.assertRaises(error):
                    gauge.get_unit()

    def test_status_and_inventory(self):
        port = FakeSerial([b">10403AFEFEFE\r", b">0005\r", b">01\r"])
        gauge = XGS600_RS232(port, sensor = "I1")

        self.assertEqual([board["type"] for board in gauge.get_contents()],
                         ["HFIG", "CNV", "IMG", "EMPTY", "EMPTY", "EMPTY"])
        states = gauge.get_setpoint_states()
        self.assertTrue(states[3])
        self.assertFalse(states[2])
        self.assertTrue(gauge.get_emission_status())

    def test_sensor_validation(self):
        with self.assertRaises(ValueError):
            XGS600_RS232(FakeSerial([]), sensor = "T1\r#0006")
        XGS600_RS232(FakeSerial([]), sensor = "T12")

    def test_unit_change_during_reading(self):
        port = FakeSerial([b">00\r", b">1.500E-03\r", b">01\r"])
        gauge = XGS600_RS232(port, sensor = "T1")
        with self.assertRaisesRegex(XGS600ProtocolError, "unit changed"):
            gauge.get_measurement()

    def test_degas_and_convection_calibration_commands(self):
        port = FakeSerial([b">\r", b">\r", b">\r", b">01\r", b">\r", b">01\r"])
        gauge = XGS600_RS232(port, sensor = "I1")

        self.assertTrue(gauge.set_degas(True))
        gauge.degas(False)
        self.assertTrue(gauge.calibrate("T2"))
        self.assertTrue(gauge.set_atmosphere_value("1.013E+03", PressureGaugeUnit.MBAR))

        self.assertEqual(port.writes, [
            b"#0041I1\r", b"#0040I1\r", b"#00A1T2\r",
            b"#0013\r", b"#00A31.013E+03\r", b"#0013\r"
        ])

    def test_setpoint_modes_thresholds_and_delays(self):
        port = FakeSerial([
            b">0005\r", b">3\r", b">\r",
            b">01\r", b">1.000E-07\r", b">01\r",
            b">01\r", b">\r", b">01\r",
            b">\r", b">1.2\r", b">\r", b">12\r"
        ])
        gauge = XGS600_RS232(port, sensor = "UHFIG1")

        self.assertEqual(gauge.get_assigned_setpoints()[1], True)
        self.assertEqual(gauge.get_setpoint_mode(2), XGS600SetpointMode.AUTO)
        self.assertTrue(gauge.set_setpoint_mode(2, XGS600SetpointMode.AUTO))
        self.assertEqual(gauge.get_setpoint_on_pressure(2)["pressure"], 1e-7)
        self.assertTrue(gauge.set_setpoint_off_pressure(2, "UHFIG1", "2.000E-07", PressureGaugeUnit.MBAR))
        self.assertTrue(gauge.set_setpoint_on_delay(2, 1.2))
        self.assertEqual(gauge.get_setpoint_on_delay(2), 1.2)
        self.assertTrue(gauge.set_setpoint_off_delay(2, 1.2))
        self.assertEqual(gauge.get_setpoint_off_delay(2), 1.2)

        self.assertEqual(port.writes, [
            b"#0004UHFIG1\r", b"#005F2\r", b"#005E23\r",
            b"#0013\r", b"#0082\r", b"#0013\r",
            b"#0013\r", b"#0072UHFIG12.000E-07\r", b"#0013\r",
            b"#00C21.2\r", b"#00E2\r", b"#00D21.2\r", b"#00F2\r"
        ])

    def test_ion_calibration_parameters(self):
        port = FakeSerial([b">1.250\r", b">\r", b">2.500\r", b">\r", b">20.00\r", b">\r"])
        gauge = XGS600_RS232(port, sensor = "I1")

        self.assertEqual(gauge.get_gas_correction(), 1.25)
        self.assertTrue(gauge.set_gas_correction(1.25))
        self.assertEqual(gauge.get_emission_current(), 2.5)
        self.assertTrue(gauge.set_emission_current(2.5))
        self.assertEqual(gauge.get_sensitivity(), 20.0)
        self.assertTrue(gauge.set_sensitivity(20.0))
        self.assertEqual(port.writes, [
            b"#0050I1\r", b"#0051I11.250\r",
            b"#0052I1\r", b"#0053I12.500\r",
            b"#0054I1\r", b"#0055I120.00\r"
        ])

    def test_setpoint_on_write_and_off_read(self):
        port = FakeSerial([
            b">01\r", b">\r", b">01\r",
            b">01\r", b">2.000E-07\r", b">01\r"
        ])
        gauge = XGS600_RS232(port, sensor = "UHFIG1")

        self.assertTrue(gauge.set_setpoint_on_pressure(3, "UHFIG1", "1.000E-07", PressureGaugeUnit.MBAR))
        self.assertEqual(gauge.get_setpoint_off_pressure(3)["pressure"], 2e-7)
        self.assertEqual(port.writes, [
            b"#0013\r", b"#0063UHFIG11.000E-07\r", b"#0013\r",
            b"#0013\r", b"#0093\r", b"#0013\r"
        ])

    def test_write_validation_and_controller_rejection(self):
        port = FakeSerial([b">01\r", b"?FF\r"])
        gauge = XGS600_RS232(port, sensor = "I1")

        with self.assertRaises(ValueError):
            gauge.set_setpoint_mode(0, XGS600SetpointMode.ON)
        with self.assertRaises(ValueError):
            gauge.set_setpoint_on_pressure(1, "I1", "1.2345E-07", PressureGaugeUnit.MBAR)
        with self.assertRaises(ValueError):
            gauge.set_setpoint_on_delay(1, 0.05)
        with self.assertRaises(ValueError):
            gauge.set_emission_current(2.55)
        with self.assertRaises(ValueError):
            gauge.set_sensitivity(20.05)
        self.assertEqual(port.writes, [])

        with self.assertRaises(ValueError):
            gauge.set_setpoint_on_pressure(1, "I1", "1.000E-07", PressureGaugeUnit.TORR)
        self.assertEqual(port.writes, [b"#0013\r"])
        with self.assertRaises(XGS600ProtocolError):
            gauge.set_degas(True)


if __name__ == "__main__":
    unittest.main()
