"""RDK X5 laser output. BOARD numbering is physical header numbering."""
from __future__ import annotations

import importlib


class GpioLaser:
    pin = 31

    def __init__(self, gpio=None) -> None:
        self._gpio = gpio
        self.connected = False
        self._on = False

    def open(self) -> None:
        if self.connected:
            return
        gpio = self._gpio or importlib.import_module("Hobot.GPIO")
        self._gpio = gpio
        gpio.setmode(gpio.BOARD)
        gpio.setup(self.pin, gpio.OUT, initial=gpio.LOW)
        self.connected = True
        self._on = False

    def read(self) -> bool:
        """Last successfully requested level; this laser has no feedback input."""
        if not self.connected:
            raise OSError("GPIO BOARD 31 is not initialized")
        return self._on

    def write(self, enabled: bool) -> bool:
        if not self.connected:
            raise OSError("GPIO BOARD 31 is not initialized")
        self._gpio.output(self.pin, self._gpio.HIGH if enabled else self._gpio.LOW)
        self._on = bool(enabled)
        return self._on

    def close(self) -> None:
        if self.connected:
            try:
                self._gpio.output(self.pin, self._gpio.LOW)
                self._on = False
            finally:
                self._gpio.cleanup(self.pin)
                self.connected = False
