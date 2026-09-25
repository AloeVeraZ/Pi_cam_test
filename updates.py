"""Password goes only to sudo's stdin; never to a file, log or command line."""
import json
import subprocess
import threading
import time
from pathlib import Path

CONFIG = Path('/etc/pi-dashboard.json')
LOG = Path('/var/log/pi-dashboard-update.log')
UNIT = 'pi-dashboard-update.service'


class Updater:
    def __init__(self):
        self.lock = threading.Lock()
        self.attempts = []

    def snapshot(self):
        try:
            config = json.loads(CONFIG.read_text())
            status = subprocess.run(['systemctl', 'show', UNIT, '-p', 'ActiveState', '--value'],
                                    capture_output=True, text=True, timeout=5).stdout.strip()
            log = LOG.read_text(errors='replace')[-6000:] if LOG.exists() else ''
            revision = (Path(__file__).resolve().parent / 'REVISION').read_text().strip()
            return dict(configured=True, repository=config['repository'], branch=config['branch'],
                        revision=revision, running=status in ('active', 'activating'), log=log)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return dict(configured=False, running=False, log='Run install.sh to enable GitHub updates.')

    def start(self, password):
        if not isinstance(password, str) or len(password) > 1024 or any(c in password for c in '\r\n\0'):
            raise ValueError('Invalid password')
        with self.lock:
            if not CONFIG.exists():
                raise ValueError('Run install.sh first')
            now = time.monotonic()
            self.attempts = [t for t in self.attempts if now - t < 600]
            if len(self.attempts) >= 5:
                raise ValueError('Too many attempts. Wait ten minutes.')
            if self.snapshot()['running']:
                raise ValueError('An update is already running')
            self.attempts.append(now)
            command = ['sudo', '-S', '-k', '-p', '', 'systemd-run', '--collect',
                       '--unit=pi-dashboard-update', '--property=Type=exec',
                       '/usr/bin/python3', '/usr/local/lib/pi-dashboard/update_worker.py']
            try:
                result = subprocess.run(command, input=password + '\n', capture_output=True,
                                        text=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                raise ValueError('Could not start update; check the Pi service logs') from None
            if result.returncode:
                raise ValueError('Update could not start. Check your Pi login password and sudo access.')
            self.attempts.clear()
            return {'message': 'Update started. The dashboard will reconnect after restarting.'}
