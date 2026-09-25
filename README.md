# Raspberry Pi Camera & Fan Dashboard

A focused MotionModule-style dashboard with a live CSI camera, On / Off / Auto fan controls, and a password-based GitHub updater. Designed for a Raspberry Pi 4 running current Raspberry Pi OS (Bookworm or newer). No Flask, pip environment, or external web assets required.

## Wiring

| Fan lead | Physical header pin | Purpose |
| --- | --- | --- |
| Power | 4 | 5V |
| Ground | 6 | Ground |
| Control | 8 | BCM GPIO14 |

This assumes a Pi-compatible fan with a **3.3V-compatible control input**, such as the Raspberry Pi 4 Case Fan. A third wire on some fans is a tachometer output, not a control input: check the model before using it. GPIO14 carries the control signal, never motor power. The camera connects to the CSI camera connector.

## Install on the Pi

1. Connect the Pi to your Wi-Fi using Raspberry Pi Imager or Raspberry Pi OS network settings. Enable SSH if installing remotely.
2. Run `sudo raspi-config`. Disable the serial login console and serial hardware interface to free GPIO14. Disable any existing case-fan control using GPIO14. Remove an existing `dtoverlay=gpio-fan,...` entry for this pin from `/boot/firmware/config.txt` if present, then reboot. This application must be the only owner of the fan pin.
3. Confirm the camera is detected with `rpicam-hello --list-cameras`. Shut down and disconnect power before reseating the ribbon if necessary.
4. Run:

```bash
sudo apt update
sudo apt install -y git
git clone https://github.com/AloeVeraZ/Pi_cam_test.git
cd Pi_cam_test
sudo bash install.sh "$USER" https://github.com/AloeVeraZ/Pi_cam_test.git main
```

The installer prints the address to open, e.g. **http://192.168.1.42** (the IP your router gave the Pi on Wi-Fi). Type it into a browser on any phone or computer on the same Wi-Fi; no port number is needed. To find it later, run `hostname -I` on the Pi. `http://raspberrypi.local` (your hostname) and the old `:8080` address also work. Guest-network client isolation may block access. The installer enables automatic startup at boot; it does not configure Wi-Fi or router port forwarding.

## Cooling

- Auto starts the fan at **60°C** and stops it at **50°C**. Separate thresholds prevent rapid switching.
- On and Off select manual control. At **75°C**, cooling overrides Off until temperature falls to **60°C**.
- A missing/invalid temperature reading requests fan On. Output errors appear in the dashboard.
- The temperature monitor runs independently of camera streaming and browser connections. A camera initialization error leaves fan controls available; restart the service after fixing the camera.
- Restarting the application returns to Auto. Displayed fan state is the commanded output, not measured RPM.
- Software cannot guarantee cooling during power loss, an OS hang, or while GPIO is released during a restart. Systemd restarts a failed application.

For different thresholds or inverted fan logic, edit the service with `sudo systemctl edit pi-dashboard`:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/python3 /opt/pi-dashboard/current/app.py --on-temp 60 --off-temp 50 --emergency-temp 75
```

Add `--active-low` only if your fan's control input requires it. Then run `sudo systemctl daemon-reload && sudo systemctl restart pi-dashboard`.

## Updates

The dashboard asks GitHub for the newest commit on the configured branch every 15 minutes (and whenever you press **Check now**) and shows **Up to date**, **Update available**, or **No connection** with both commit IDs. Checks use `git ls-remote`, so nothing is downloaded until you update.

Click **Update now**, enter the Pi account password when needed, and submit. This is the password used by `sudo`, not your GitHub password. An account with passwordless sudo can leave it blank. Passwords are passed to sudo through stdin and are not saved or logged. Five unsuccessful attempts cause a ten-minute cooldown within the running dashboard process.

The installer saves the repository and branch in root-owned `/etc/pi-dashboard.json`. The updater fetches the latest configured branch, installs dependencies, runs tests, installs a new release, and restarts the dashboard. It runs in a separate systemd unit. The page reconnects automatically. If the new application fails its HTTP health check, the previous application release is restored. Dependency and system configuration changes are not rolled back. Previous releases remain under `/opt/pi-dashboard/releases`.

Only configure a repository you trust: its installer runs with administrator privileges. Public GitHub repositories work without credentials; private repositories need Git authentication configured for the root-run updater separately. There is no GitHub-token field in the UI.

The dashboard is intended for a trusted LAN and has no viewer login. Its HTTP connection is unencrypted. For password entry on an untrusted network, use an SSH tunnel:

```bash
ssh -L 8080:localhost:8080 YOUR_PI_USER@raspberrypi.local
```

Then browse to `http://localhost:8080`. Do not expose port 8080 to the internet.

## Troubleshooting and tests

If you can SSH in but the page does not load, check `systemctl status pi-dashboard` and try `http://PI_IP:8080`.

```bash
systemctl status pi-dashboard
journalctl -u pi-dashboard -n 100 --no-pager
cat /var/log/pi-dashboard-update.log
sudo systemctl restart pi-dashboard
python3 -m unittest discover -v
```

Tests use fake GPIO and temperature inputs and exercise the HTTP API. Real camera capture, fan polarity, and systemd/sudo installation must also be checked on the Pi.

## References and credits

- [Raspberry Pi camera software and Picamera2](https://www.raspberrypi.com/documentation/computers/camera_software.html)
- [Raspberry Pi configuration](https://www.raspberrypi.com/documentation/computers/configuration.html)
- [GPIO Zero output devices and BCM numbering](https://gpiozero.readthedocs.io/en/stable/api_output.html)
- Visual styling follows your MotionModule dashboard. Bundled Inter and Barlow Condensed fonts retain their SIL Open Font License files in `static/fonts`.
