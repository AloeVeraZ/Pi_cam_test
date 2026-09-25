#!/usr/bin/env python3
"""Installed root-owned; launched by sudo/systemd independently of the UI."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request


def main():
    config = json.loads(Path('/etc/pi-dashboard.json').read_text())
    current = Path('/opt/pi-dashboard/current')
    previous = current.resolve()
    with open('/var/log/pi-dashboard-update.log', 'w', buffering=1) as log:
        os.chmod(log.name, 0o644)
        def run(args):
            subprocess.run(args, check=True, stdout=log, stderr=log, timeout=900,
                           env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
        try:
            log.write('Downloading ' + config['repository'] + ' · ' + config['branch'] + '\n')
            with tempfile.TemporaryDirectory(prefix='pi-dashboard-update-') as directory:
                source = Path(directory) / 'source'
                run(['git', 'clone', '--depth', '1', '--branch', config['branch'], '--', config['repository'], str(source)])
                run(['/bin/bash', str(source / 'install.sh'), config['user'], config['repository'], config['branch']])
            for _ in range(30):
                try:
                    with urllib.request.urlopen('http://127.0.0.1:8080/api/status', timeout=2) as response:
                        if response.status == 200:
                            log.write('Update complete. Dashboard is online.\n')
                            return
                except OSError:
                    pass
                time.sleep(1)
            raise RuntimeError('New dashboard did not become healthy')
        except Exception as exc:
            log.write('Update failed: ' + str(exc) + '\n')
            if current.resolve() != previous:
                temporary = current.with_name('rollback')
                temporary.unlink(missing_ok=True)
                temporary.symlink_to(previous, target_is_directory=True)
                temporary.replace(current)
                run(['systemctl', 'restart', 'pi-dashboard.service'])
                log.write('Restored the previous application release.\n')
            raise


if __name__ == '__main__':
    main()
