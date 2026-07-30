"""Opens an ST7789 panel, with or without a vendor stack.

Two backends, picked automatically:

* **vendor** reuses the driver already installed alongside a vendor app. It
  knows the board's pin map, does partial refreshes, and is the right choice
  where it exists.
* **raw** drives the panel with nothing but spidev and the legacy GPIO sysfs,
  for a board with no vendor libraries at all.

Both expose the same two calls, `display(image)` and `set_backlight(on)`, so
callers do not care which one they got.
"""
import glob
import os
import sys
import time

GPIO_ROOT = "/sys/class/gpio"
VENDOR_DIR = "/opt/app_picoclaw"

# Same pins as the vendor app uses, overridable for a differently wired board.
DEFAULT_PINS = {
    "bus": int(os.environ.get("PANEL_SPI_BUS", "1")),
    "cs": int(os.environ.get("PANEL_SPI_CS", "0")),
    "dc": os.environ.get("PANEL_DC", "A28"),
    "rst": os.environ.get("PANEL_RST", "A27"),
    "bl": os.environ.get("PANEL_BL", "A19"),
    "speed": int(os.environ.get("PANEL_SPEED_HZ", "20000000")),
}


def _read(path, default="?"):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def gpio_banks():
    """Maps bank letters to (label, base, count), ordered by register address."""
    chips = []
    for path in glob.glob(os.path.join(GPIO_ROOT, "gpiochip*")):
        base = _read(os.path.join(path, "base"), "")
        if base.isdigit():
            chips.append((_read(os.path.join(path, "label")), int(base),
                          int(_read(os.path.join(path, "ngpio"), "0") or 0)))
    chips.sort(key=lambda c: c[0])
    return {chr(ord("A") + i): c for i, c in enumerate(chips)}


def resolve_pin(pin, mapping=None):
    """'A28' -> global sysfs line number."""
    mapping = mapping or gpio_banks()
    letter, number = pin[0].upper(), int(pin[1:])
    if letter not in mapping:
        raise RuntimeError(f"unknown GPIO bank {letter}; available: {sorted(mapping)}")
    _, base, count = mapping[letter]
    if count and number >= count:
        raise RuntimeError(f"{pin} out of range ({count} lines)")
    return base + number


class _Line:
    def __init__(self, number):
        self.number = number
        path = f"{GPIO_ROOT}/gpio{number}"
        if not os.path.exists(path):
            with open(f"{GPIO_ROOT}/export", "w") as f:
                f.write(str(number))
            time.sleep(0.05)
        with open(f"{path}/direction", "w") as f:
            f.write("out")
        self._fh = open(f"{path}/value", "w")

    def set(self, high):
        self._fh.write("1" if high else "0")
        self._fh.flush()

    def close(self):
        try:
            self._fh.close()
            with open(f"{GPIO_ROOT}/unexport", "w") as f:
                f.write(str(self.number))
        except OSError:
            pass


class RawST7789:
    """Minimal driver: spidev plus three GPIO lines, no vendor dependency."""

    def __init__(self, width=240, height=240, **pins):
        import spidev

        cfg = {**DEFAULT_PINS, **pins}
        self.width, self.height = width, height
        mapping = gpio_banks()
        self._dc = _Line(resolve_pin(cfg["dc"], mapping))
        self._rst = _Line(resolve_pin(cfg["rst"], mapping))
        self._bl = _Line(resolve_pin(cfg["bl"], mapping))
        self._spi = spidev.SpiDev()
        self._spi.open(cfg["bus"], cfg["cs"])
        self._spi.max_speed_hz = cfg["speed"]
        self._spi.mode = 0
        self.describe = (f"raw spidev{cfg['bus']}.{cfg['cs']} @{cfg['speed']}Hz "
                         f"DC={cfg['dc']} RST={cfg['rst']} BL={cfg['bl']}")
        self._init_panel()

    def _cmd(self, byte, payload=b""):
        self._dc.set(False)
        self._spi.writebytes([byte])
        if payload:
            self._dc.set(True)
            self._spi.writebytes2(list(payload))

    def _init_panel(self):
        self._rst.set(True); time.sleep(0.05)
        self._rst.set(False); time.sleep(0.05)
        self._rst.set(True); time.sleep(0.15)
        self._cmd(0x01); time.sleep(0.15)   # software reset
        self._cmd(0x11); time.sleep(0.15)   # sleep out
        self._cmd(0x3A, b"\x55")            # 16 bit colour
        self._cmd(0x36, b"\x00")            # memory access order
        self._cmd(0x21)                     # inversion on
        self._cmd(0x13); time.sleep(0.01)   # normal mode
        self._cmd(0x29); time.sleep(0.05)   # display on

    def set_backlight(self, value):
        self._bl.set(bool(value))

    def display(self, img):
        w, h = self.width, self.height
        if img.size != (w, h):
            img = img.resize((w, h))
        self._cmd(0x2A, bytes([0, 0, (w - 1) >> 8, (w - 1) & 0xFF]))
        self._cmd(0x2B, bytes([0, 0, (h - 1) >> 8, (h - 1) & 0xFF]))
        self._cmd(0x2C)
        self._dc.set(True)
        payload = self._to_rgb565(img)
        # Chunked so a full frame never sits twice in memory on a board where
        # one costs 115 KB out of 128 MB, and to stay under the SPI limit.
        for start in range(0, len(payload), 4096):
            self._spi.writebytes2(payload[start:start + 4096])

    @staticmethod
    def _to_rgb565(img):
        """Packs an RGB image to big-endian RGB565.

        Vectorised on purpose: doing this pixel by pixel in Python cost 1.2s a
        frame on a RISC-V board, enough that running a status display would
        have been a real load on the machine. numpy brings it to milliseconds.
        """
        try:
            import numpy as np
        except ImportError:
            pixels = img.convert("RGB").tobytes()
            out = bytearray(len(pixels) // 3 * 2)
            for i in range(len(pixels) // 3):
                r, g, b = pixels[i * 3], pixels[i * 3 + 1], pixels[i * 3 + 2]
                value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                out[i * 2], out[i * 2 + 1] = value >> 8, value & 0xFF
            return bytes(out)

        arr = np.asarray(img.convert("RGB"), dtype=np.uint16)
        packed = (((arr[..., 0] & 0xF8) << 8)
                  | ((arr[..., 1] & 0xFC) << 3)
                  | (arr[..., 2] >> 3)).astype(">u2")
        return packed.tobytes()

    def close(self):
        try:
            self._spi.close()
        except Exception:
            pass
        for line in (self._dc, self._rst, self._bl):
            line.close()


def open_panel(width=240, height=240):
    """Returns a panel object, preferring the vendor driver when present."""
    if os.path.exists(os.path.join(VENDOR_DIR, "st7789.py")):
        sys.path.insert(0, VENDOR_DIR)
        try:
            from config import (SPI_PORT, SPI_DC, SPI_RST, SPI_BACKLIGHT,
                                SPI_SPEED_HZ, SPI_ROTATION)
            from st7789 import ST7789

            panel = ST7789(port=SPI_PORT, dc=SPI_DC, rst=SPI_RST,
                           backlight=SPI_BACKLIGHT, spi_speed_hz=SPI_SPEED_HZ,
                           rotation=SPI_ROTATION)
            panel.describe = "vendor driver"
            return panel
        except Exception as err:
            # Falling through matters: the vendor driver fails when another
            # process holds the GPIO lines, and the raw one fails the same way,
            # so the message is preserved rather than swallowed.
            print(f"vendor driver unavailable ({err}), trying raw", file=sys.stderr)
    return RawST7789(width=width, height=height)
