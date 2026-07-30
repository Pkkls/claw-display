#!/usr/bin/env python3
"""Draw on the board's ST7789 panel from the command line.

Reuses the driver and pin configuration that already ship with the picoclaw
app rather than reimplementing either: the panel is 240x240 on SPI, with DC,
RESET and backlight on GPIO, and getting any of those wrong shows nothing at
all with no error to explain it.

    show.py --text "hello"                 one or more lines, auto-wrapped
    show.py --title "STATUS" --text "..."  title bar plus body
    show.py --test                         colour bars, to prove the link works
    show.py --image photo.png              any image, scaled to fit
    show.py --off                          backlight off

The panel is a shared resource. If the picoclaw app is running it owns the
screen, and two writers on the same SPI bus interleave into garbage, so this
refuses to draw unless --force is given.
"""
import argparse
import os
import subprocess
import sys

APP_DIR = "/opt/app_picoclaw"
sys.path.insert(0, APP_DIR)

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

WIDTH = HEIGHT = 240
FONT_CANDIDATES = [
    "/maixapp/share/font/SourceHanSansCN-Regular.otf",
    "/usr/share/fonts/truetype/DejaVuSans.ttf",
]


def picoclaw_running():
    try:
        out = subprocess.run(["ps"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any("app_picoclaw" in line and "grep" not in line for line in out.splitlines())


def open_panel():
    """Builds the driver from picoclaw's own config so the pins stay in sync."""
    from config import SPI_PORT, SPI_DC, SPI_RST, SPI_BACKLIGHT, SPI_SPEED_HZ, SPI_ROTATION
    from st7789 import ST7789

    panel = ST7789(
        port=SPI_PORT, dc=SPI_DC, rst=SPI_RST, backlight=SPI_BACKLIGHT,
        spi_speed_hz=SPI_SPEED_HZ, rotation=SPI_ROTATION,
    )
    return panel


def load_font(size):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def wrap(draw, text, font, max_width):
    """Greedy wrap. Falls back to hard-splitting a word too long to fit."""
    lines = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for word in paragraph.split(" "):
            trial = f"{current} {word}".strip()
            if draw.textlength(trial, font=font) <= max_width:
                current = trial
                continue
            if current:
                lines.append(current)
            while draw.textlength(word, font=font) > max_width and len(word) > 1:
                cut = len(word)
                while cut > 1 and draw.textlength(word[:cut], font=font) > max_width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            current = word
        lines.append(current)
    return lines


def render_text(text, title=None, size=18, fg=(235, 235, 235), bg=(12, 12, 16),
                accent=(90, 200, 140)):
    img = Image.new("RGB", (WIDTH, HEIGHT), bg)
    draw = ImageDraw.Draw(img)
    font = load_font(size)
    y = 6

    if title:
        title_font = load_font(size + 2)
        draw.rectangle([0, 0, WIDTH, 28], fill=accent)
        draw.text((8, 5), title[:22], font=title_font, fill=bg)
        y = 36

    margin = 8
    for line in wrap(draw, text, font, WIDTH - 2 * margin):
        if y > HEIGHT - size:
            break
        draw.text((margin, y), line, font=font, fill=fg)
        y += size + 4
    return img


def render_test():
    """Colour bars plus a frame: wrong pin order or byte order is obvious."""
    img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    bars = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 255, 255)]
    bar_h = HEIGHT // len(bars)
    for i, colour in enumerate(bars):
        draw.rectangle([0, i * bar_h, WIDTH, (i + 1) * bar_h], fill=colour)
    # A frame proves the full extent is addressed, not a cropped window.
    draw.rectangle([0, 0, WIDTH - 1, HEIGHT - 1], outline=(0, 0, 0), width=3)
    font = load_font(20)
    draw.text((10, HEIGHT - 34), "SPI OK", font=font, fill=(0, 0, 0))
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text")
    ap.add_argument("--title")
    ap.add_argument("--image")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--off", action="store_true")
    ap.add_argument("--size", type=int, default=18)
    ap.add_argument("--force", action="store_true",
                    help="draw even if the picoclaw app owns the screen")
    ap.add_argument("--save", help="also write the rendered frame to a PNG")
    args = ap.parse_args()

    if not (args.text or args.image or args.test or args.off):
        ap.error("nothing to do: pass --text, --image, --test or --off")

    if picoclaw_running() and not args.force:
        print("picoclaw is running and owns the panel; pass --force to draw anyway",
              file=sys.stderr)
        return 2

    if args.image:
        img = Image.open(args.image).convert("RGB")
        img.thumbnail((WIDTH, HEIGHT))
        canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        canvas.paste(img, ((WIDTH - img.width) // 2, (HEIGHT - img.height) // 2))
        img = canvas
    elif args.test:
        img = render_test()
    elif args.text:
        img = render_text(args.text, title=args.title, size=args.size)
    else:
        img = None

    if args.save and img is not None:
        img.save(args.save)

    panel = open_panel()
    if args.off:
        panel.set_backlight(0)
        print("backlight off")
        return 0

    panel.set_backlight(1)
    panel.display(img)
    print(f"drawn {WIDTH}x{HEIGHT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
