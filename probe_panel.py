#!/usr/bin/env python3
"""Try to light an ST7789 panel with no vendor libraries.

An SPI display cannot be detected: it never answers, so the only way to know
whether one is wired up is to drive it and look. This uses nothing but spidev
and the legacy GPIO sysfs, so it runs on a board that has no vendor stack
installed.

    probe_panel.py --list                 show GPIO banks and their bases
    probe_panel.py --bus 1 --dc A28 --rst A27 --bl A19

Pin names follow the vendor convention: bank letter plus line number. If the
screen stays dark, either nothing is connected or the pins differ; the script
cannot tell those apart, which is why it prints exactly what it drove.
"""
import argparse
import glob
import os
import sys
import time

GPIO_ROOT = "/sys/class/gpio"


def read(path, default="?"):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def banks():
    """Maps a bank letter to its sysfs base, ordered by hardware address.

    The vendor names lines A0..A31, B0.., and so on; sysfs only knows global
    numbers, so the mapping is by sorting the chips by their register address.
    """
    chips = []
    for path in glob.glob(os.path.join(GPIO_ROOT, "gpiochip*")):
        label = read(os.path.join(path, "label"))
        base = read(os.path.join(path, "base"), "")
        ngpio = read(os.path.join(path, "ngpio"), "")
        if base.isdigit():
            chips.append((label, int(base), int(ngpio) if ngpio.isdigit() else 0))
    chips.sort(key=lambda c: c[0])
    return {chr(ord("A") + i): c for i, c in enumerate(chips)}


def resolve(pin, mapping):
    """'A28' -> global sysfs number."""
    letter, number = pin[0].upper(), int(pin[1:])
    if letter not in mapping:
        raise SystemExit(f"unknown bank {letter}; available: {sorted(mapping)}")
    label, base, ngpio = mapping[letter]
    if ngpio and number >= ngpio:
        raise SystemExit(f"{pin} is out of range for {label} ({ngpio} lines)")
    return base + number


class Line:
    def __init__(self, number):
        self.number = number
        self.path = f"{GPIO_ROOT}/gpio{number}"
        if not os.path.exists(self.path):
            with open(f"{GPIO_ROOT}/export", "w") as f:
                f.write(str(number))
            time.sleep(0.05)
        with open(f"{self.path}/direction", "w") as f:
            f.write("out")
        self._value = open(f"{self.path}/value", "w")

    def set(self, high):
        self._value.write("1" if high else "0")
        self._value.flush()

    def release(self):
        try:
            self._value.close()
            with open(f"{GPIO_ROOT}/unexport", "w") as f:
                f.write(str(self.number))
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--bus", type=int, default=1)
    ap.add_argument("--cs", type=int, default=0)
    ap.add_argument("--dc", default="A28")
    ap.add_argument("--rst", default="A27")
    ap.add_argument("--bl", default="A19")
    ap.add_argument("--speed", type=int, default=20_000_000)
    ap.add_argument("--width", type=int, default=240)
    ap.add_argument("--height", type=int, default=240)
    args = ap.parse_args()

    mapping = banks()
    if args.list or not mapping:
        for letter, (label, base, ngpio) in sorted(mapping.items()):
            print(f"  {letter}: {label} base={base} lines={ngpio}")
        if not mapping:
            print("no gpiochip exposes a base in sysfs", file=sys.stderr)
            return 2
        return 0

    import spidev

    dc_n, rst_n, bl_n = (resolve(p, mapping) for p in (args.dc, args.rst, args.bl))
    print(f"driving spidev{args.bus}.{args.cs} at {args.speed} Hz, "
          f"DC={args.dc}({dc_n}) RST={args.rst}({rst_n}) BL={args.bl}({bl_n})")

    dc, rst, bl = Line(dc_n), Line(rst_n), Line(bl_n)
    spi = spidev.SpiDev()
    spi.open(args.bus, args.cs)
    spi.max_speed_hz = args.speed
    spi.mode = 0

    def command(byte, payload=b""):
        dc.set(False)
        spi.writebytes([byte])
        if payload:
            dc.set(True)
            spi.writebytes2(list(payload))

    try:
        bl.set(True)
        rst.set(True); time.sleep(0.05)
        rst.set(False); time.sleep(0.05)
        rst.set(True); time.sleep(0.15)

        command(0x01); time.sleep(0.15)          # software reset
        command(0x11); time.sleep(0.15)          # sleep out
        command(0x3A, b"\x55")                   # 16 bit colour
        command(0x36, b"\x00")                   # memory access order
        command(0x21)                            # inversion on, as these panels want
        command(0x13); time.sleep(0.01)          # normal display
        command(0x29); time.sleep(0.05)          # display on

        w, h = args.width, args.height
        command(0x2A, bytes([0, 0, (w - 1) >> 8, (w - 1) & 0xFF]))
        command(0x2B, bytes([0, 0, (h - 1) >> 8, (h - 1) & 0xFF]))

        # Three horizontal bands: a wrong byte order or offset is obvious.
        red, green, blue = 0xF800, 0x07E0, 0x001F
        rows = []
        for y in range(h):
            colour = red if y < h // 3 else (green if y < 2 * h // 3 else blue)
            rows.append(bytes([colour >> 8, colour & 0xFF]) * w)
        command(0x2C)
        dc.set(True)
        for chunk in rows:
            spi.writebytes2(list(chunk))

        print("frame sent: red, green and blue bands. Look at the panel.")
        print("A dark panel means no display on these pins, not necessarily no display.")
        return 0
    finally:
        spi.close()
        for line in (dc, rst, bl):
            line.release()


if __name__ == "__main__":
    sys.exit(main())
