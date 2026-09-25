"""Fan policy, independent of the GPIO and web interfaces."""
import math
import threading


class FanController:
    def __init__(self, output, read_temperature, on=60, off=50, emergency=75):
        if not 0 < off < on < emergency < 100:
            raise ValueError('Expected 0 < off < on < emergency < 100')
        self.output = output
        self.read_temperature = read_temperature
        self.on, self.off, self.emergency = on, off, emergency
        self.lock = threading.RLock()
        self.mode = 'auto'
        self.auto_on = False
        self.override = False
        self.state = {}

    def update(self, mode=None):
        with self.lock:
            if mode is not None:
                if mode not in ('auto', 'on', 'off'):
                    raise ValueError('Mode must be auto, on, or off')
                self.mode = mode
            error = None
            try:
                temp = float(self.read_temperature())
                if not math.isfinite(temp) or not 0 <= temp <= 150:
                    raise ValueError('Invalid temperature reading')
            except Exception as exc:
                temp, error = None, str(exc)
            if temp is not None:
                if temp >= self.on:
                    self.auto_on = True
                elif temp <= self.off:
                    self.auto_on = False
                if temp >= self.emergency:
                    self.override = True
                elif temp <= self.on:
                    self.override = False
            enabled = temp is None or self.override or self.mode == 'on' or (self.mode == 'auto' and self.auto_on)
            reason = ('Temperature sensor unavailable' if temp is None else
                      'High-temperature override' if self.override else
                      'Automatic temperature control' if self.mode == 'auto' else 'Manual control')
            try:
                self.output.on() if enabled else self.output.off()
                output_error = None
            except Exception as exc:
                output_error = str(exc)
            self.state = dict(mode=self.mode, fan_on=enabled if output_error is None else None,
                              temperature=temp, reason=reason, sensor_error=error,
                              output_error=output_error, on_threshold=self.on,
                              off_threshold=self.off, emergency_threshold=self.emergency)
            return dict(self.state)

    def snapshot(self):
        with self.lock:
            return dict(self.state)
