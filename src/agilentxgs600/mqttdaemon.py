import argparse
import json
import logging
import os
import re
import signal
import stat
import threading
import time

import paho.mqtt.client as mqtt

from .xgs600 import XGS600_RS232, XGS600CommunicationError


DEFAULT_CONFIG = os.path.expanduser("~/.config/xgs600mqtt.conf")
LOG = logging.getLogger("xgs600mqtt")


def load_config(path):
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        raise ValueError("MQTT config contains a password and must have mode 0600")

    with open(path, "r", encoding = "utf-8") as configFile:
        config = json.load(configFile)
    if not isinstance(config, dict):
        raise ValueError("MQTT config must be a JSON object")

    requiredStrings = (
        "mqtt_host", "mqtt_user", "mqtt_password", "gauge",
        "gauge_name", "serial_port", "sensor", "base_topic",
        "pressure_topic"
    )
    for key in requiredStrings:
        if not isinstance(config.get(key), str) or not config[key]:
            raise ValueError(f"{key} must be a nonempty string")
    if config["gauge"] != "xgs600":
        raise ValueError("Only gauge type xgs600 is supported")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", config["gauge_name"]):
        raise ValueError("gauge_name must contain only letters, digits, underscores, and hyphens")
    port = config.get("mqtt_port", 1883)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("mqtt_port must be a TCP port number")
    address = config.get("gauge_address", 0)
    if not isinstance(address, int) or isinstance(address, bool) or not 0 <= address <= 0x20:
        raise ValueError("gauge_address must be 0..32")
    interval = config.get("interval_seconds", 10)
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not 1 <= interval <= 60:
        raise ValueError("interval_seconds must be between 1 and 60")
    if not config["pressure_topic"].startswith(config["base_topic"].rstrip("/") + "/"):
        raise ValueError("pressure_topic must be below base_topic")
    if "#" in config["pressure_topic"] or "+" in config["pressure_topic"]:
        raise ValueError("pressure_topic cannot contain MQTT wildcards")

    XGS600_RS232._validate_sensor(config["sensor"])
    return config


def measurement_payload(measurement):
    return json.dumps(
        {
            "pressure_mbar": measurement["pressure_mbar"],
            "timestamp": measurement["ts"]
        },
        separators = (",", ":"),
        allow_nan = False
    )


def make_client(config, connected):
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id = "xgs600mqtt-" + config["gauge_name"],
        protocol = mqtt.MQTTv311
    )
    client.username_pw_set(config["mqtt_user"], config["mqtt_password"])
    client.reconnect_delay_set(min_delay = 1, max_delay = 30)

    def on_connect(client, userdata, flags, reasonCode, properties):
        if reasonCode == 0:
            connected.set()
        else:
            connected.clear()
            LOG.error("MQTT connection rejected: %s", reasonCode)

    def on_disconnect(client, userdata, flags, reasonCode, properties):
        connected.clear()
        if reasonCode != 0:
            LOG.warning("MQTT connection lost: %s", reasonCode)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    return client


def publish_measurement(client, config, measurement):
    payload = measurement_payload(measurement)
    info = client.publish(config["pressure_topic"], payload, qos = 1, retain = False)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        raise OSError(f"MQTT publish failed with code {info.rc}")
    info.wait_for_publish(timeout = 10)
    if not info.is_published():
        raise TimeoutError("MQTT PUBACK was not received within 10 seconds")
    return payload


def run(config, stopEvent, once = False):
    interval = config.get("interval_seconds", 10)

    while not stopEvent.is_set():
        connected = threading.Event()
        client = make_client(config, connected)
        try:
            with XGS600_RS232(
                config["serial_port"],
                sensor = config["sensor"],
                address = config.get("gauge_address", 0),
                baudrate = 9600,
                timeout = 2.0
            ) as gauge:
                client.connect(config["mqtt_host"], config.get("mqtt_port", 1883), keepalive = 120)
                client.loop_start()
                if not connected.wait(10):
                    raise TimeoutError("MQTT connection was not accepted within 10 seconds")

                while not stopEvent.is_set():
                    started = time.monotonic()
                    try:
                        if not connected.is_set():
                            raise ConnectionError("MQTT broker is disconnected")
                        measurement = gauge.get_measurement()
                        payload = publish_measurement(client, config, measurement)
                        LOG.info("Published %s to %s", payload, config["pressure_topic"])
                    except (XGS600CommunicationError, OSError, TimeoutError, ConnectionError) as error:
                        LOG.warning("No pressure published: %s", error)
                        if once:
                            raise
                    if once:
                        return
                    stopEvent.wait(max(0, interval - (time.monotonic() - started)))
        except (OSError, TimeoutError, ConnectionError) as error:
            LOG.warning("Connection unavailable: %s", error)
            if once:
                raise
            stopEvent.wait(interval)
        finally:
            client.disconnect()
            client.loop_stop()


def main(argv = None):
    parser = argparse.ArgumentParser(description = "Publish XGS-600 pressure to MQTT")
    parser.add_argument("--config", default = DEFAULT_CONFIG, help = "JSON config path")
    parser.add_argument("--once", action = "store_true", help = "publish one reading and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(level = logging.INFO, format = "%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config)
    stopEvent = threading.Event()
    signal.signal(signal.SIGTERM, lambda signum, frame: stopEvent.set())
    signal.signal(signal.SIGINT, lambda signum, frame: stopEvent.set())
    run(config, stopEvent, once = args.once)


if __name__ == "__main__":
    main()
