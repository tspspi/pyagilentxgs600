import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from agilentxgs600 import XGS600CommunicationError
from agilentxgs600.mqttdaemon import load_config, measurement_payload, publish_measurement, run


CONFIG = {
    "mqtt_host": "broker.example.org",
    "mqtt_user": "publisher",
    "mqtt_password": "test-secret",
    "gauge": "xgs600",
    "gauge_name": "gauge-one",
    "serial_port": "/dev/tty-test",
    "sensor": "UHFIG1",
    "base_topic": "/example/site",
    "pressure_topic": "/example/site/status/pressure/gauge-one",
    "interval_seconds": 10
}


class FakePublishInfo:
    rc = 0

    def wait_for_publish(self, timeout):
        self.timeout = timeout

    def is_published(self):
        return True


class FakeClient:
    def __init__(self, connected = None):
        self.connected = connected
        self.published = []

    def connect(self, host, port, keepalive):
        self.connected.set()

    def loop_start(self):
        pass

    def publish(self, topic, payload, qos, retain):
        self.published.append((topic, payload, qos, retain))
        return FakePublishInfo()

    def disconnect(self):
        pass

    def loop_stop(self):
        pass


class FailingGauge:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get_measurement(self):
        raise XGS600CommunicationError("serial timeout")


class MQTTDaemonTests(unittest.TestCase):
    def test_config_requires_private_file_and_topic_under_base(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "xgs600mqtt.conf")
            with open(path, "w", encoding = "utf-8") as configFile:
                json.dump(CONFIG, configFile)
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(ValueError, "0600"):
                load_config(path)

            os.chmod(path, 0o600)
            self.assertEqual(load_config(path)["sensor"], "UHFIG1")
            other = dict(CONFIG, pressure_topic = "/other/status/pressure/gauge-one")
            with open(path, "w", encoding = "utf-8") as configFile:
                json.dump(other, configFile)
            with self.assertRaisesRegex(ValueError, "below base_topic"):
                load_config(path)

    def test_publish_payload_has_only_pressure_and_timestamp(self):
        measurement = {"pressure_mbar": 1.804e-7, "ts": 1790605361.8, "sensor": "UHFIG1"}
        client = FakeClient()

        payload = publish_measurement(client, CONFIG, measurement)

        self.assertEqual(json.loads(payload), {"pressure_mbar": 1.804e-7, "timestamp": 1790605361.8})
        self.assertEqual(client.published, [(CONFIG["pressure_topic"], payload, 1, False)])
        with self.assertRaises(ValueError):
            measurement_payload({"pressure_mbar": float("nan"), "ts": 1})

    def test_timeout_never_publishes_a_stale_value(self):
        clients = []

        def make_client(config, connected):
            client = FakeClient(connected)
            clients.append(client)
            return client

        with patch("agilentxgs600.mqttdaemon.make_client", side_effect = make_client), \
             patch("agilentxgs600.mqttdaemon.XGS600_RS232", FailingGauge):
            with self.assertRaises(XGS600CommunicationError):
                run(CONFIG, threading.Event(), once = True)
        self.assertEqual(clients[0].published, [])


if __name__ == "__main__":
    unittest.main()
