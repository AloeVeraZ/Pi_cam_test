import json
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import Mock, patch
from controller import FanController
from app import Dashboard, Frames, camera_detected
from updates import Updater


class FanTests(unittest.TestCase):
    def setUp(self):
        self.temperature = 40
        self.output = Mock()
        self.fan = FanController(self.output, lambda: self.temperature)

    def test_hysteresis(self):
        for temp, expected in [(40, False), (60, True), (55, True), (50, False), (55, False)]:
            self.temperature = temp
            self.assertEqual(self.fan.update()['fan_on'], expected)

    def test_manual_and_emergency_latch(self):
        self.assertTrue(self.fan.update('on')['fan_on'])
        self.assertFalse(self.fan.update('off')['fan_on'])
        for temp, expected in [(75, True), (70, True), (60, False)]:
            self.temperature = temp
            self.assertEqual(self.fan.update()['fan_on'], expected)
        self.assertTrue(self.fan.update('auto')['fan_on'])

    def test_sensor_failure_and_recovery(self):
        self.temperature = float('nan')
        self.assertTrue(self.fan.update('off')['fan_on'])
        self.temperature = 40
        self.assertFalse(self.fan.update()['fan_on'])

    def test_gpio_failure_is_not_reported_as_success(self):
        self.output.on.side_effect = OSError('GPIO lost')
        self.assertIsNone(self.fan.update('on')['fan_on'])


class WebTests(unittest.TestCase):
    def setUp(self):
        self.controller = FanController(Mock(), lambda: 45)
        self.controller.update()
        self.server = Dashboard(('127.0.0.1', 0), self.controller, Frames(), 'No camera')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(self, data, token=True):
        return urlopen(Request(self.url + '/api/fan', data=json.dumps(data).encode(),
                              headers={'X-Dashboard-Token': self.server.token if token else ''}), timeout=3)

    def test_dashboard_and_manual_control(self):
        with urlopen(self.url) as r:
            page = r.read().decode()
            self.assertNotIn('__TOKEN__', page)
            self.assertIn('Software updates', page)
        with self.post({'mode': 'on'}) as r:
            self.assertTrue(json.load(r)['fan_on'])

    def test_bad_request_and_csrf(self):
        for data, token, status in [({'mode': 'on'}, False, 403), ({'mode': 'bad'}, True, 400), ([], True, 400)]:
            with self.assertRaises(HTTPError) as ctx:
                self.post(data, token)
            self.assertEqual(ctx.exception.code, status)

    def test_camera_failure_keeps_status_available(self):
        with self.assertRaises(HTTPError) as ctx:
            urlopen(self.url + '/stream.mjpg')
        self.assertEqual(ctx.exception.code, 503)
        with urlopen(self.url + '/api/status') as r:
            self.assertEqual(json.load(r)['temperature'], 45)


class CameraDetectTests(unittest.TestCase):
    @patch('app.subprocess.run')
    def test_list_cameras_output(self, run):
        run.return_value = Mock(stdout='Available cameras\n-----------------\n0 : ov5647')
        self.assertTrue(camera_detected())
        run.return_value = Mock(stdout='No cameras available!')
        self.assertFalse(camera_detected())

    @patch('app.subprocess.run', side_effect=FileNotFoundError)
    def test_missing_tools(self, run):
        self.assertFalse(camera_detected())


class UpdateTests(unittest.TestCase):
    @patch('updates.CONFIG')
    @patch('updates.subprocess.run')
    def test_password_stdin_and_fixed_command(self, run, config):
        config.exists.return_value = True
        run.return_value = Mock(returncode=0)
        updater = Updater()
        updater.snapshot = lambda: {'running': False}
        updater.start('secret-example')
        args, kwargs = run.call_args
        self.assertNotIn('secret-example', ' '.join(args[0]))
        self.assertEqual(kwargs['input'], 'secret-example\n')

    @patch('updates.CONFIG')
    @patch('updates.subprocess.run')
    def test_wrong_password_rate_limit(self, run, config):
        config.exists.return_value = True
        run.return_value = Mock(returncode=1)
        updater = Updater()
        updater.snapshot = lambda: {'running': False}
        for _ in range(5):
            with self.assertRaises(ValueError):
                updater.start('bad')
        with self.assertRaisesRegex(ValueError, 'Too many'):
            updater.start('bad')
        self.assertEqual(run.call_count, 5)


if __name__ == '__main__':
    unittest.main()
