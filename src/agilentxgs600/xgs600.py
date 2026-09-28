import math
import re
import threading
import time
from decimal import Decimal, InvalidOperation
from enum import IntEnum

import serial

from labdevices.pressuregauge import PressureGauge, PressureGaugeUnit


PRESSURE_PATTERN = re.compile(r"^[0-9]+\.[0-9]+E[+-][0-9]+$")
SENSOR_PATTERN = re.compile(r"^(?:T(?:[1-9]|1[0-2])|I[1-6])$")
LABEL_PATTERN = re.compile(r"^U[A-Z0-9 ]{1,5}$")
SCIENTIFIC_PATTERN = re.compile(r"^[0-9]\.[0-9]{3}E[+-][0-9]{2}$")
BOARD_CODES = {
    "10": "HFIG",
    "3A": "IMG",
    "40": "CNV",
    "4C": "ANALOG",
    "FE": "EMPTY"
}
UNIT_CODES = {
    "00": PressureGaugeUnit.TORR,
    "01": PressureGaugeUnit.MBAR,
    "02": PressureGaugeUnit.PASCAL
}
UNIT_COMMANDS = {
    PressureGaugeUnit.TORR: "10",
    PressureGaugeUnit.MBAR: "11",
    PressureGaugeUnit.PASCAL: "12"
}


class XGS600CommunicationError(Exception):
    pass


class XGS600ProtocolError(XGS600CommunicationError):
    pass


class XGS600SetpointMode(IntEnum):
    OFF = 0
    ON = 1
    AUTO = 3


class XGS600_RS232(PressureGauge):
    """Synchronous ASCII protocol driver for one XGS-600 serial connection.

    Configure ``sensor`` (for example ``T1`` or ``I1``) for the inherited
    ``get_pressure()`` call. A different sensor can be passed per reading.
    """

    def __init__(
        self,
        port,
        sensor = None,
        address = 0,
        baudrate = 9600,
        timeout = 2.0,
        debug = False
    ):
        super().__init__(
            measurementRange = (0, math.inf),
            hasDegas = True,
            deviceSupportedUnits = list(UNIT_COMMANDS),
            debug = debug
        )
        if not isinstance(port, (str, serial.SerialBase)):
            raise ValueError("Port must be a device path or a pyserial port")
        if not isinstance(address, int) or isinstance(address, bool) or not 0 <= address <= 0x20:
            raise ValueError("Address must be an integer from 0 to 0x20")
        if baudrate not in (9600, 19200):
            raise ValueError("Baud rate must be 9600 or 19200 bits/s")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 30:
            raise ValueError("Timeout must be greater than 0 and at most 30 seconds")
        if sensor is not None:
            sensor = self._validate_sensor(sensor)

        self._portName = port if isinstance(port, str) else None
        self._port = None if isinstance(port, str) else port
        self._ownsPort = isinstance(port, str)
        self._sensor = sensor
        self._address = address
        self._baudrate = baudrate
        self._timeout = timeout
        self._lock = threading.RLock()
        self._lastQuery = None

    @staticmethod
    def _validate_sensor(sensor):
        if not isinstance(sensor, str):
            raise ValueError("Sensor must be a string such as T1, I1, or UGATE")
        if SENSOR_PATTERN.fullmatch(sensor) or LABEL_PATTERN.fullmatch(sensor):
            return sensor
        raise ValueError("Sensor must be T1..T12, I1..I6, or U followed by a 1-5 character uppercase label")

    def _ion_sensor(self, sensor):
        sensor = self._sensor if sensor is None else self._validate_sensor(sensor)
        if sensor is None or not sensor.startswith(("I", "U")):
            raise ValueError("An ion gauge sensor must be configured or supplied")
        return sensor

    def _convection_sensor(self, sensor):
        sensor = self._sensor if sensor is None else self._validate_sensor(sensor)
        if sensor is None or not sensor.startswith(("T", "U")):
            raise ValueError("A convection or auxiliary sensor must be configured or supplied")
        return sensor

    @staticmethod
    def _setpoint_number(number):
        if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 8:
            raise ValueError("Setpoint number must be an integer from 1 to 8")
        return number

    @staticmethod
    def _decimal_value(value, name):
        if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
            raise ValueError(f"{name} must be a finite positive number")
        try:
            result = Decimal(str(value))
        except InvalidOperation as error:
            raise ValueError(f"{name} must be a finite positive number") from error
        if not result.is_finite() or result <= 0:
            raise ValueError(f"{name} must be a finite positive number")
        return result

    @classmethod
    def _scientific_value(cls, value):
        number = cls._decimal_value(value, "Pressure")
        mantissa, exponent = f"{number:.3E}".split("E")
        exponent = int(exponent)
        if not -99 <= exponent <= 99:
            raise ValueError("Pressure exponent is outside the controller format")
        encoded = f"{mantissa}E{exponent:+03d}"
        if Decimal(encoded) != number:
            raise ValueError("Pressure needs more than four significant digits")
        return encoded

    @classmethod
    def _fixed_value(cls, value, wholeDigits, fractionalDigits, name):
        number = cls._decimal_value(value, name)
        if number >= 10 ** wholeDigits:
            raise ValueError(f"{name} exceeds the controller format")
        encoded = f"{number:0{wholeDigits + fractionalDigits + 1}.{fractionalDigits}f}"
        if Decimal(encoded) != number:
            raise ValueError(f"{name} cannot be represented without rounding")
        return encoded

    def _write_command(self, command):
        response = self._query(command)
        if response:
            raise XGS600ProtocolError(f"Unexpected write response: {response!r}")
        return True

    def _connect(self):
        with self._lock:
            if self._port is None:
                try:
                    self._port = serial.Serial(
                        self._portName,
                        baudrate = self._baudrate,
                        bytesize = serial.EIGHTBITS,
                        parity = serial.PARITY_NONE,
                        stopbits = serial.STOPBITS_ONE,
                        timeout = self._timeout,
                        write_timeout = self._timeout,
                        xonxoff = False,
                        rtscts = False
                    )
                except serial.SerialException as error:
                    raise XGS600CommunicationError(str(error)) from error
            elif not self._port.is_open:
                self._port.open()
        return True

    def _disconnect(self):
        with self._lock:
            if self._port is not None and self._ownsPort:
                self._port.close()
                self._port = None
            self._lastQuery = None
        return True

    def close(self):
        self._disconnect()
        self._usedConnect = False

    def __enter__(self):
        if self._usedConnect:
            raise ValueError("Cannot use context management on a connected object")
        self._connect()
        self._usesContext = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        self._usesContext = False

    def _query(self, command):
        with self._lock:
            if self._port is None or not self._port.is_open:
                raise XGS600CommunicationError("Device is not connected")

            # The manual limits query traffic to at most ten commands per second.
            if self._lastQuery is not None:
                delay = 0.11 - (time.monotonic() - self._lastQuery)
                if delay > 0:
                    time.sleep(delay)

            request = f"#{self._address:02X}{command}\r".encode("ascii")
            try:
                self._port.reset_input_buffer()
                if self._debug:
                    print(f"[XGS600-DEBUG] Tx: {request!r}")
                written = self._port.write(request)
                self._lastQuery = time.monotonic()
                if written != len(request):
                    raise XGS600CommunicationError("Incomplete serial command write")
                response = self._port.read_until(b"\r", size = 256)
            except (serial.SerialException, serial.SerialTimeoutException, OSError) as error:
                raise XGS600CommunicationError(str(error)) from error

            if self._debug:
                print(f"[XGS600-DEBUG] Rx: {response!r}")
            if not response or not response.endswith(b"\r"):
                raise XGS600CommunicationError("Timed out or incomplete XGS-600 response")
            try:
                response = response[:-1].decode("ascii")
            except UnicodeDecodeError as error:
                raise XGS600ProtocolError("Non-ASCII XGS-600 response") from error
            if response == "?FF":
                raise XGS600ProtocolError("XGS-600 rejected command or sensor (?FF)")
            if not response.startswith(">"):
                raise XGS600ProtocolError(f"Unexpected XGS-600 response: {response!r}")
            return response[1:]

    def _get_unit(self):
        response = self._query("13")
        if response not in UNIT_CODES:
            raise XGS600ProtocolError(f"Unknown pressure unit code: {response!r}")
        return UNIT_CODES[response]

    def _set_unit(self, unit):
        return self._write_command(UNIT_COMMANDS[unit])

    def get_contents(self):
        response = self._query("01")
        if len(response) != 12 or not re.fullmatch(r"[0-9A-F]{12}", response):
            raise XGS600ProtocolError(f"Invalid board inventory: {response!r}")
        result = []
        for slot in range(6):
            code = response[slot * 2:slot * 2 + 2]
            result.append({"slot": slot + 1, "code": code, "type": BOARD_CODES.get(code, "UNKNOWN")})
        return result

    def get_versions(self):
        response = self._query("05")
        revisions = response.split(",")
        if not revisions or not all(re.fullmatch(r"[0-9A-F]{4}", item) for item in revisions):
            raise XGS600ProtocolError(f"Invalid revision response: {response!r}")
        return revisions

    def _get_versions(self):
        return self.get_versions()

    def _get_device_type(self):
        return {"device": "XGS-600"}

    def get_measurement(self, sensor = None):
        sensor = self._sensor if sensor is None else self._validate_sensor(sensor)
        if sensor is None:
            raise ValueError("A sensor must be configured or supplied")

        # Keep the unit query and pressure query together so another caller
        # cannot change units between them through this connection.
        with self._lock:
            unit = self._get_unit()
            response = self._query("02" + sensor)
            if self._get_unit() != unit:
                raise XGS600ProtocolError("Pressure unit changed during measurement")
            timestamp = time.time()

        if not PRESSURE_PATTERN.fullmatch(response):
            raise XGS600ProtocolError(f"Invalid or unavailable pressure for {sensor}: {response!r}")
        pressure = float(response)
        if not math.isfinite(pressure) or pressure <= 0:
            raise XGS600ProtocolError(f"Invalid pressure for {sensor}: {response!r}")

        if unit == PressureGaugeUnit.MBAR:
            mbar = pressure
        elif unit == PressureGaugeUnit.TORR:
            mbar = pressure * 1.333223684
        else:
            mbar = pressure / 100.0

        return {
            "ts": timestamp,
            "sensor": sensor,
            "pressure": pressure,
            "pressureUnit": unit,
            "pressure_mbar": mbar,
            "raw": response,
            "responseValid": True
        }

    def _get_pressure(self):
        measurement = self.get_measurement()
        return {
            "raw": measurement["raw"],
            "value": {measurement["pressureUnit"]: measurement["pressure"]},
            "mbar": measurement["pressure_mbar"]
        }

    def get_pressure(self, unit = PressureGaugeUnit.MBAR, sensor = None):
        if not isinstance(unit, PressureGaugeUnit):
            raise ValueError("Unit must be a PressureGaugeUnit")
        measurement = self.get_measurement(sensor = sensor)
        if unit == PressureGaugeUnit.MBAR:
            return measurement["pressure_mbar"]
        if unit == PressureGaugeUnit.TORR:
            return measurement["pressure_mbar"] / 1.333223684
        return measurement["pressure_mbar"] * 100.0

    def get_setpoint_states(self):
        response = self._query("03")
        return self._parse_setpoint_mask(response)

    @staticmethod
    def _parse_setpoint_mask(response):
        if not re.fullmatch(r"00[0-9A-F]{2}", response):
            raise XGS600ProtocolError(f"Invalid setpoint response: {response!r}")
        mask = int(response[2:], 16)
        return {index: bool(mask & (1 << (index - 1))) for index in range(1, 9)}

    def get_assigned_setpoints(self, sensor = None):
        sensor = self._sensor if sensor is None else self._validate_sensor(sensor)
        if sensor is None:
            raise ValueError("A sensor must be configured or supplied")
        return self._parse_setpoint_mask(self._query("04" + sensor))

    def get_setpoint_mode(self, number):
        number = self._setpoint_number(number)
        response = self._query(f"5F{number:X}")
        if response not in ("0", "1", "3"):
            raise XGS600ProtocolError(f"Invalid setpoint mode: {response!r}")
        return XGS600SetpointMode(int(response))

    def set_setpoint_mode(self, number, mode):
        number = self._setpoint_number(number)
        if not isinstance(mode, XGS600SetpointMode):
            raise ValueError("Mode must be an XGS600SetpointMode")
        return self._write_command(f"5E{number:X}{mode.value}")

    def _get_setpoint_pressure(self, number, command):
        number = self._setpoint_number(number)
        with self._lock:
            unit = self.get_unit()
            response = self._query(f"{command}{number:X}")
            if self.get_unit() != unit:
                raise XGS600ProtocolError("Pressure unit changed during setpoint read")
        if not SCIENTIFIC_PATTERN.fullmatch(response):
            raise XGS600ProtocolError(f"Invalid setpoint pressure: {response!r}")
        return {"pressure": float(response), "unit": unit, "raw": response}

    def _set_setpoint_pressure(self, number, sensor, pressure, unit, command):
        number = self._setpoint_number(number)
        sensor = self._validate_sensor(sensor)
        if not isinstance(unit, PressureGaugeUnit):
            raise ValueError("Unit must be a PressureGaugeUnit")
        encoded = self._scientific_value(pressure)
        with self._lock:
            if self.get_unit() != unit:
                raise ValueError("Setpoint pressure unit differs from the controller display unit")
            result = self._write_command(f"{command}{number:X}{sensor}{encoded}")
            if self.get_unit() != unit:
                raise XGS600ProtocolError("Pressure unit changed during setpoint write")
        return result

    def get_setpoint_on_pressure(self, number):
        return self._get_setpoint_pressure(number, "8")

    def set_setpoint_on_pressure(self, number, sensor, pressure, unit):
        """Assign a sensor and ON threshold in the controller's current unit."""
        return self._set_setpoint_pressure(number, sensor, pressure, unit, "6")

    def get_setpoint_off_pressure(self, number):
        return self._get_setpoint_pressure(number, "9")

    def set_setpoint_off_pressure(self, number, sensor, pressure, unit):
        """Set the OFF threshold after setting ON; OFF must be higher."""
        return self._set_setpoint_pressure(number, sensor, pressure, unit, "7")

    @staticmethod
    def _delay_value(seconds):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float, str, Decimal)):
            raise ValueError("Delay must be 0.0..9.9 seconds in 0.1 second steps")
        try:
            value = Decimal(str(seconds))
        except InvalidOperation as error:
            raise ValueError("Delay must be 0.0..9.9 seconds in 0.1 second steps") from error
        if not value.is_finite() or not 0 <= value <= Decimal("9.9") or value * 10 != (value * 10).to_integral_value():
            raise ValueError("Delay must be 0.0..9.9 seconds in 0.1 second steps")
        return f"{value:.1f}"

    def _get_setpoint_delay(self, number, command):
        number = self._setpoint_number(number)
        response = self._query(f"{command}{number:X}")
        if re.fullmatch(r"[0-9]\.[0-9]", response):
            return float(response)
        if re.fullmatch(r"[0-9]{2}", response):
            # The manual prints the OFF response as xx, but describes x.x seconds.
            return int(response) / 10.0
        raise XGS600ProtocolError(f"Invalid setpoint delay: {response!r}")

    def get_setpoint_on_delay(self, number):
        return self._get_setpoint_delay(number, "E")

    def set_setpoint_on_delay(self, number, seconds):
        number = self._setpoint_number(number)
        return self._write_command(f"C{number:X}{self._delay_value(seconds)}")

    def get_setpoint_off_delay(self, number):
        return self._get_setpoint_delay(number, "F")

    def set_setpoint_off_delay(self, number, seconds):
        number = self._setpoint_number(number)
        return self._write_command(f"D{number:X}{self._delay_value(seconds)}")

    def get_emission_status(self, sensor = None):
        sensor = self._ion_sensor(sensor)
        response = self._query("32" + sensor)
        if response not in ("00", "01"):
            raise XGS600ProtocolError(f"Invalid emission status: {response!r}")
        return response == "01"

    def get_degas_status(self, sensor = None):
        sensor = self._ion_sensor(sensor)
        response = self._query("42" + sensor)
        if response not in ("00", "01"):
            raise XGS600ProtocolError(f"Invalid degas status: {response!r}")
        return response == "01"

    def set_degas(self, on, sensor = None):
        if not isinstance(on, bool):
            raise ValueError("Degas state must be a boolean")
        sensor = self._ion_sensor(sensor)
        return self._write_command(("41" if on else "40") + sensor)

    def _degas(self, degasOn):
        return self.set_degas(degasOn)

    def calibrate(self, sensor = None):
        """Run Smart Cal on a CNV or AUX sensor at its present pressure."""
        return self._write_command("A1" + self._convection_sensor(sensor))

    def set_atmosphere_value(self, pressure, unit):
        """Set the Smart Cal atmosphere reference in the current display unit."""
        if not isinstance(unit, PressureGaugeUnit):
            raise ValueError("Unit must be a PressureGaugeUnit")
        encoded = self._scientific_value(pressure)
        if "E-" in encoded:
            raise ValueError("Atmosphere reference must have a positive exponent")
        with self._lock:
            if self.get_unit() != unit:
                raise ValueError("Atmosphere pressure unit differs from the controller display unit")
            result = self._write_command("A3" + encoded)
            if self.get_unit() != unit:
                raise XGS600ProtocolError("Pressure unit changed during atmosphere value write")
        return result

    def _get_ion_parameter(self, sensor, command, pattern, name):
        sensor = self._ion_sensor(sensor)
        response = self._query(command + sensor)
        if not re.fullmatch(pattern, response):
            raise XGS600ProtocolError(f"Invalid {name}: {response!r}")
        return float(response)

    def get_gas_correction(self, sensor = None):
        return self._get_ion_parameter(sensor, "50", r"[0-9]\.[0-9]{3}", "gas correction")

    def set_gas_correction(self, value, sensor = None):
        sensor = self._ion_sensor(sensor)
        encoded = self._fixed_value(value, 1, 3, "Gas correction")
        return self._write_command("51" + sensor + encoded)

    def get_emission_current(self, sensor = None):
        """Return emission current in mA."""
        return self._get_ion_parameter(sensor, "52", r"[0-9]\.[0-9]{3}", "emission current")

    def set_emission_current(self, current_mA, sensor = None):
        sensor = self._ion_sensor(sensor)
        current = self._decimal_value(current_mA, "Emission current")
        if current * 10 != (current * 10).to_integral_value():
            raise ValueError("Emission current must be in 0.1 mA steps")
        encoded = self._fixed_value(current, 1, 3, "Emission current")
        return self._write_command("53" + sensor + encoded)

    def get_sensitivity(self, sensor = None):
        """Return ion-gauge sensitivity in inverse Torr."""
        return self._get_ion_parameter(sensor, "54", r"[0-9]{2}\.[0-9]{2}", "sensitivity")

    def set_sensitivity(self, value_per_torr, sensor = None):
        """Overwrite ion-gauge sensitivity, including any factory setting."""
        sensor = self._ion_sensor(sensor)
        sensitivity = self._decimal_value(value_per_torr, "Sensitivity")
        if sensitivity * 10 != (sensitivity * 10).to_integral_value():
            raise ValueError("Sensitivity must be in 0.1 per Torr steps")
        encoded = self._fixed_value(sensitivity, 2, 2, "Sensitivity")
        return self._write_command("55" + sensor + encoded)
