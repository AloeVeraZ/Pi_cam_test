#!/usr/bin/env python3
"""LAN camera dashboard. Run one process; hardware imports are lazy for tests."""
import argparse
import io
import json
import logging
import secrets
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from controller import FanController
from updates import Updater

ROOT = Path(__file__).resolve().parent


class Frames(io.BufferedIOBase):
    def __init__(self):
        super().__init__()
        self.condition = threading.Condition()
        self.frame = None
        self.sequence = 0
        self.updated = 0

    def write(self, data):
        with self.condition:
            self.frame = bytes(data)
            self.sequence += 1
            self.updated = time.monotonic()
            self.condition.notify_all()
        return len(data)


class Dashboard(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, controller, frames, camera_error):
        self.controller = controller
        self.frames = frames
        self.camera_error = camera_error
        self.token = secrets.token_urlsafe(32)
        self.updater = Updater()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def reply(self, status, body, content_type='application/json'):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith('/static/fonts/'):
            name = self.path.removeprefix('/static/fonts/')
            file = ROOT / 'static' / 'fonts' / name
            if '/' in name or '\\' in name or not file.is_file():
                self.reply(404, {'error': 'Not found'})
            else:
                self.reply(200, file.read_bytes(), 'font/woff2' if name.endswith('.woff2') else 'text/plain')
        elif self.path == '/api/updates':
            self.reply(200, self.server.updater.snapshot())
        elif self.path == '/':
            page = (ROOT / 'index.html').read_text(encoding='utf-8')
            self.reply(200, page.replace('__TOKEN__', self.server.token).encode(), 'text/html; charset=utf-8')
        elif self.path == '/api/status':
            state = self.server.controller.snapshot()
            with self.server.frames.condition:
                fresh = time.monotonic() - self.server.frames.updated < 5
            state.update(camera_ok=fresh, camera_error=self.server.camera_error,
                         hostname=socket.gethostname(), instance=self.server.token[:12])
            self.reply(200, state)
        elif self.path == '/stream.mjpg':
            if self.server.camera_error:
                self.reply(503, {'error': self.server.camera_error})
                return
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=FRAME')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            sequence = -1
            try:
                while True:
                    frames = self.server.frames
                    with frames.condition:
                        ready = frames.condition.wait_for(
                            lambda: frames.frame is not None and frames.sequence != sequence, timeout=10)
                        if not ready:
                            return
                        frame, sequence = frames.frame, frames.sequence
                    self.wfile.write(b'--FRAME\r\nContent-Type: image/jpeg\r\nContent-Length: ' +
                                     str(len(frame)).encode() + b'\r\n\r\n' + frame + b'\r\n')
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
        else:
            self.reply(404, {'error': 'Not found'})

    def do_POST(self):
        if self.path not in ('/api/fan', '/api/updates'):
            self.reply(404, {'error': 'Not found'})
            return
        # A random per-process token prevents cross-origin form submissions.
        if not secrets.compare_digest(self.headers.get('X-Dashboard-Token', ''), self.server.token):
            self.reply(403, {'error': 'Reload the dashboard and try again'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise ValueError('Invalid request size')
            data = json.loads(self.rfile.read(length))
            if self.path == '/api/updates':
                if not isinstance(data, dict):
                    raise ValueError('Expected an object')
                self.reply(202, self.server.updater.start(data.get('password', '')))
                return
            if not isinstance(data, dict) or not isinstance(data.get('mode'), str):
                raise ValueError('A mode string is required')
            state = self.server.controller.update(data['mode'])
        except (ValueError, UnicodeDecodeError) as exc:
            self.reply(400, {'error': str(exc)})
            return
        self.reply(200, state)

    def log_message(self, fmt, *args):
        logging.debug('%s ' + fmt, self.client_address[0], *args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--fan-pin', type=int, default=14, help='BCM numbering')
    parser.add_argument('--active-low', action='store_true')
    parser.add_argument('--on-temp', type=float, default=60)
    parser.add_argument('--off-temp', type=float, default=50)
    parser.add_argument('--emergency-temp', type=float, default=75)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    from gpiozero import DigitalOutputDevice
    fan = DigitalOutputDevice(args.fan_pin, active_high=not args.active_low, initial_value=True)
    stop = threading.Event()
    camera = server = worker = None
    try:
        def read_temperature():
            return float(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000

        controller = FanController(fan, read_temperature, args.on_temp, args.off_temp, args.emergency_temp)
        controller.update()

        def cooling_loop():
            while not stop.wait(1):
                controller.update()

        worker = threading.Thread(target=cooling_loop, daemon=True)
        worker.start()
        frames, camera_error = Frames(), None
        try:
            from picamera2 import Picamera2
            from picamera2.encoders import MJPEGEncoder
            from picamera2.outputs import FileOutput
            camera = Picamera2()
            camera.configure(camera.create_video_configuration(main={'size': (1280, 720)},
                                                               controls={'FrameRate': 20}))
            camera.start_recording(MJPEGEncoder(), FileOutput(frames))
        except Exception as exc:
            camera_error = str(exc)
            logging.exception('Camera unavailable; fan control will continue')
        server = Dashboard((args.host, args.port), controller, frames, camera_error)

        def terminate(*_):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, terminate)
        logging.info('Dashboard: http://%s.local:%s', socket.gethostname(), args.port)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if worker:
            worker.join(timeout=3)
        if server:
            server.server_close()
        try:
            if camera:
                camera.close()
        finally:
            # Closing GPIO releases the pin; cooling after process exit is not guaranteed.
            fan.on()
            fan.close()


if __name__ == '__main__':
    main()
