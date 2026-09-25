"""Password goes only to sudo's stdin; never to a file, log or command line."""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

CONFIG = Path('/etc/pi-dashboard.json')
LOG = Path('/var/log/pi-dashboard-update.log')
UNIT = 'pi-dashboard-update.service'


CHECK_INTERVAL = 15 * 60


def latest_commit(repository, branch):
    """Ask GitHub which commit the branch is on, without downloading anything."""
    try:
        result = subprocess.run(['git', 'ls-remote', '--heads', repository, 'refs/heads/' + branch],
                                capture_output=True, text=True, timeout=20,
                                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
    except (OSError, subprocess.SubprocessError):
        raise ValueError('Could not run git to check for updates') from None
    if result.returncode:
        raise ValueError('Could not reach GitHub. Check that the Pi has internet access.')
    commit = result.stdout.split()[0] if result.stdout.strip() else ''
    if len(commit) != 40:
        raise ValueError('Branch ' + branch + ' was not found on GitHub')
    return commit


class Updater:
    def __init__(self, clock=time.monotonic, interval=CHECK_INTERVAL):
        self.lock = threading.Lock()
        self.attempts = []
        self.clock, self.interval = clock, interval
        self.check_lock = threading.Lock()
        self.latest = self.check_error = None
        self.checked = self.checked_at = None
        self.checking = False
        self.thread = None

    def snapshot(self, refresh=False):
        try:
            config = json.loads(CONFIG.read_text())
            status = subprocess.run(['systemctl', 'show', UNIT, '-p', 'ActiveState', '--value'],
                                    capture_output=True, text=True, timeout=5).stdout.strip()
            log = LOG.read_text(errors='replace')[-6000:] if LOG.exists() else ''
            revision = (Path(__file__).resolve().parent / 'REVISION').read_text().strip()
            state = dict(configured=True, repository=config['repository'], branch=config['branch'],
                         revision=revision, running=status in ('active', 'activating'), log=log)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return dict(configured=False, running=False, log='Run install.sh to enable GitHub updates.')
        state.update(self.check(config['repository'], config['branch'], revision, refresh))
        return state

    def check(self, repository, branch, revision, refresh=False):
        """Return the last GitHub answer; start a background check when stale or asked."""
        with self.check_lock:
            stale = self.checked is None or self.clock() - self.checked >= self.interval
            if (refresh or stale) and not self.checking:
                self.checking = True
                self.thread = threading.Thread(target=self._check, args=(repository, branch), daemon=True)
                self.thread.start()
            latest, error = self.latest, self.check_error
            result = dict(checking=self.checking, checked_at=self.checked_at, latest=latest,
                          check_error=error, check_interval=self.interval)
        if error:
            result['update_status'] = 'unreachable'
        elif latest is None:
            result['update_status'] = 'checking'
        elif len(revision) != 40:
            result['update_status'] = 'unknown'
        else:
            result['update_status'] = 'up-to-date' if latest == revision else 'update-available'
        return result

    def _check(self, repository, branch):
        try:
            latest, error = latest_commit(repository, branch), None
        except Exception as exc:  # a failed check must never take the dashboard down
            latest, error = None, str(exc)
        with self.check_lock:
            self.latest, self.check_error = latest, error
            self.checked, self.checked_at = self.clock(), time.time()
            self.checking = False

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
