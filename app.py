#!/usr/bin/env python3
"""LAN camera dashboard. Run one process; hardware imports are lazy for tests."""
import argparse
import io
import json
import logging
import secrets
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from controller import FanController
from updates import Updater

ROOT = Path(__file__).resolve().parent
CAMERA_HINT = (' — Check the ribbon cable (blue side toward the USB/Ethernet ports on a Pi 4, '
               'contacts facing the HDMI side), that it is in the CAMERA port not DISPLAY, and that '
               'camera_auto_detect=1 is in /boot/firmware/config.txt. Power off before reseating. '
               'The dashboard restarts itself when a camera appears.')


def local_addresses():
    """The Pi's LAN IPv4 addresses, e.g. the one Wi-Fi gave it."""
    try:
        return [a for a in subprocess.run(['hostname', '-I'], capture_output=True, text=True,
                                          timeout=5).stdout.split() if '.' in a] or [socket.gethostname() + '.local']
    except (OSError, subprocess.SubprocessError):
        return [socket.gethostname() + '.local']


def camera_detected():
    for tool in ('rpicam-hello', 'libcamera-hello'):
        try:
            result = subprocess.run([tool, '--list-cameras'], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        return 'Available cameras' in result.stdout
    return False


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
        elif self.path in ('/api/updates', '/api/updates?refresh=1'):
            self.reply(200, self.server.updater.snapshot(refresh=self.path.endswith('refresh=1')))
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
    parser.add_argument('--web-port', type=int, default=80,
                        help='also serve here so http://PI_IP works without a port; 0 disables')
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
    camera = server = worker = web = None
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
            camera_error = str(exc) + CAMERA_HINT
            logging.exception('Camera unavailable; fan control will continue')
        server = Dashboard((args.host, args.port), controller, frames, camera_error)
        if args.web_port and args.web_port != args.port:
            try:
                web = Dashboard((args.host, args.web_port), controller, frames, camera_error)
            except OSError as exc:
                logging.warning('Port %s unavailable (%s); use port %s instead', args.web_port, exc, args.port)
            else:
                # One dashboard on two ports: same page token, same update checks.
                web.token, web.updater = server.token, server.updater
                threading.Thread(target=web.serve_forever, daemon=True).start()

        def camera_watch():
            # libcamera only enumerates cameras once per process, so check from a
            # fresh process and let systemd restart us once a camera appears.
            while not stop.wait(15):
                if camera_detected():
                    logging.info('Camera detected; restarting to start the stream')
                    server.shutdown()
                    return

        if camera_error:
            threading.Thread(target=camera_watch, daemon=True).start()

        def update_watch():
            # snapshot() starts a GitHub check whenever the last one is 15 minutes old.
            while True:
                server.updater.snapshot()
                if stop.wait(60):
                    return

        threading.Thread(target=update_watch, daemon=True).start()

        def terminate(*_):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, terminate)
        for address in local_addresses():
            logging.info('Dashboard: http://%s%s', address, '' if web else ':%s' % args.port)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if worker:
            worker.join(timeout=3)
        if web:
            web.shutdown()
            web.server_close()
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
