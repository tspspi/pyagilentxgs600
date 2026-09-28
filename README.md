# pyAgilentXGS600

Unofficial Python library for reading the Agilent XGS-600 vacuum gauge controller over its serial interface. It implements the [pylabdevs](https://github.com/tspspi/pylabdevs) `PressureGauge` base class and supplies an additional MQTT publishing microservice.

## Installation

```sh
pip install pyagilentxgs600-tspspi
```

The package requires `pylabdevs-tspspi`, `pyserial`, and `paho-mqtt`. Installing dependencies may require access to a package index.

## Usage

```python
from agilentxgs600 import XGS600_RS232
from labdevices.pressuregauge import PressureGaugeUnit

with XGS600_RS232("/dev/cuaU0", sensor = "UHFIG1") as gauge:
    measurement = gauge.get_measurement()
    print(measurement)
    print(gauge.get_pressure(PressureGaugeUnit.MBAR))
    print(gauge.get_contents())
    print(gauge.get_setpoint_states())
```

The `sensor` constructor argument selects the default channel. You can pass a different channel to `get_measurement(sensor = "I1")` or `get_pressure(sensor = "I1")`. Sensor codes use `T1` through `T12` for convection/auxiliary gauges, `I1` through `I6` for ion gauges, or `U` followed by an uppercase user label of up to five characters. For a channel labeled `HFIG1`, use `sensor = "UHFIG1"`. Only channels physically installed on a particular controller will respond. Check the installed boards and configured sensor labels before relying on a channel.

`get_measurement()` returns a dictionary with timestamp `ts` (Unix seconds), `sensor`, `pressure`, `pressureUnit`, `pressure_mbar`, `raw`, and `responseValid`. The last field only reports that framing and numeric validation passed; it does not establish physical gauge health. Communication timeouts, controller `?FF` errors, and nonnumeric pressure displays raise `XGS600CommunicationError` or `XGS600ProtocolError`; they never reuse an earlier reading. The caller must still enforce reading age, the relevant sensor range, and independent hardware protection in its interlock policy.

`get_pressure()` returns a number in mbar by default, matching the `PressureGauge` API. The controller reports pressure in its current display unit; the library reads that unit before and after every pressure query and converts as requested. The unit can be changed with inherited `set_unit(PressureGaugeUnit.TORR)`; this modifies the controller display setting. The other supported methods are `get_unit()`, `get_contents()`, `get_versions()`, `get_setpoint_states()`, `get_emission_status()`, and `get_degas_status()`. 

Use `connect()` and `disconnect()` instead of `with` when an imperative lifecycle is needed. `close()` also releases an owned serial port. An already opened `serial.Serial` object can be supplied; the caller retains ownership of that object.

`xgs600mqtt` periodically reads the configured sensor and publishes a compact JSON object containing `pressure_mbar` and `timestamp` (Unix seconds). It uses MQTT QoS 1 and waits for the brokers acknowledgment. Messages are not retained; a consumer must check the timestamp and treat an old or missing value as unavailable. A failed gauge read publishes nothing and is logged. The first read after opening some `CH340` adapters can time out; the next scheduled read is attempted on the same connection.

Copy [examples/xgs600mqtt.example.json](examples/xgs600mqtt.example.json) to `~/.config/xgs600mqtt.conf`, edit it for the host, and set mode `0600`. `gauge` currently accepts only `xgs600`; `gauge_name` identifies this publisher in its MQTT client ID. `serial_port`, `gauge_address`, and `sensor` select the controller and channel. `base_topic` and `pressure_topic` are explicit; the latter must be below the former. `interval_seconds` defaults to 10 and must be between 1 and 60.

```sh
chmod 600 ~/.config/xgs600mqtt.conf
xgs600mqtt --once
xgs600mqtt
```

The [service/xgs600mqtt](service/xgs600mqtt) template is a [FreeBSD](https://www.freebsd.org) `rc.d` script. Install it under `/usr/local/etc/rc.d/`, then set `xgs600mqtt_enable=YES`, `xgs600mqtt_run_user`, and `xgs600mqtt_config` in `/etc/rc.conf`.  The service account must be able to read its private config and serial device. The script uses `daemon(8)` to supervise the Python process and log to syslog.

## Serial protocol

The controller must be configured for ASCII RS-232, 9600 or 19200 bit/s, 8 data bits, no parity, one stop bit, and no flow control. The default address is `00`. 
The [Agilent XGS-600 instruction manual, revision D, Appendix B](https://community.agilent.com/cfs-file/__key/docpreview-s/00-00-01-92-60/XGS_2D00_600-Gauge-Controller-Instruction-Manual-rev-D.pdf) documents these commands and response formats.

