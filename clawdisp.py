#!/usr/bin/env python3
"""Always-on status display for the board's ST7789 panel.

Rotates through a few pages of machine state, and gives whoever holds SSH a
way to put something on the screen: write /root/clawdisp/msg.txt and it takes
over the panel until it goes stale.

    clawdisp.py                 run forever, rotating pages
    clawdisp.py --once          render one frame and exit (for testing)
    clawdisp.py --save out.png  also write the frame to a file

The panel and its GPIO lines are exclusive: only one process can hold them.
This daemon assumes it is that process.
"""
import argparse
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/app_picoclaw")

from PIL import Image, ImageDraw  # noqa: E402
from show import WIDTH, HEIGHT, load_font, open_panel, wrap  # noqa: E402

MSG_PATH = os.environ.get("CLAWDISP_MSG", "/root/clawdisp/msg.txt")
# A host on the local network to watch, and what to call it on screen. Left
# empty the network page simply omits the row.
PEER_HOST = os.environ.get("CLAWDISP_PEER", "")
PEER_LABEL = os.environ.get("CLAWDISP_PEER_LABEL", "peer")
INTERNET_HOST = os.environ.get("CLAWDISP_INTERNET", "1.1.1.1")
MSG_TTL = 300          # a pushed message owns the screen for five minutes
PAGE_SECONDS = 8
REFRESH_SECONDS = 4

BG = (12, 12, 16)
FG = (228, 228, 232)
DIM = (130, 130, 140)
OK = (90, 200, 140)
WARN = (230, 170, 70)
BAD = (225, 90, 90)


def sh(cmd, timeout=5):
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def uptime_load():
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        with open("/proc/loadavg") as f:
            load = f.read().split()[0]
    except (OSError, ValueError, IndexError):
        return "?", "?"
    days, rem = divmod(int(secs), 86400)
    hours, rem = divmod(rem, 3600)
    return (f"{days}d {hours}h" if days else f"{hours}h {rem // 60}m"), load


def memory():
    """Returns (used_mb, total_mb, percent)."""
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])
    except (OSError, ValueError):
        return 0, 0, 0
    total = info.get("MemTotal", 0) // 1024
    available = info.get("MemAvailable", info.get("MemFree", 0)) // 1024
    used = total - available
    return used, total, (100 * used // total if total else 0)


def disk_pct(path="/"):
    # statvfs is POSIX only. Guarding it keeps the render path testable on a
    # development machine, which is the only place these pages can be checked
    # without the panel in hand.
    if not hasattr(os, "statvfs"):
        return 0
    try:
        st = os.statvfs(path)
    except OSError:
        return 0
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return int(100 * (total - free) / total) if total else 0


def ip_address():
    for candidate in sh("ip -4 addr show 2>/dev/null").splitlines():
        line = candidate.strip()
        if line.startswith("inet ") and not line.startswith("inet 127."):
            return line.split()[1].split("/")[0]
    return "no ip"


def read_message():
    """A pushed message, if it exists and is still fresh."""
    try:
        age = time.time() - os.path.getmtime(MSG_PATH)
        if age > MSG_TTL:
            return None
        with open(MSG_PATH, encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
    except OSError:
        return None
    return text or None


def frame(title, rows, accent=OK):
    """One page: an accent title bar, then label/value rows."""
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, WIDTH, 30], fill=accent)
    draw.text((8, 6), title[:20], font=load_font(19), fill=BG)

    font = load_font(17)
    small = load_font(14)
    y = 40
    for label, value, colour in rows:
        if y > HEIGHT - 22:
            break
        draw.text((8, y), label, font=small, fill=DIM)
        draw.text((92, y - 1), str(value)[:16], font=font, fill=colour)
        y += 25
    draw.text((8, HEIGHT - 18), time.strftime("%H:%M:%S"), font=small, fill=DIM)
    return img


def page_system():
    up, load = uptime_load()
    used, total, pct = memory()
    disk = disk_pct()
    return frame("SYSTEM", [
        ("uptime", up, FG),
        ("load", load, WARN if _f(load) > 4 else FG),
        ("mem", f"{pct}% {used}M", BAD if pct > 90 else WARN if pct > 75 else FG),
        ("disk", f"{disk}%", BAD if disk > 90 else FG),
        ("host", socket.gethostname()[:14], DIM),
    ])


def pingable(host):
    return sh(f"ping -c1 -W2 {host} >/dev/null 2>&1 && echo yes || echo no") == "yes"


def page_network():
    net_up = pingable(INTERNET_HOST)
    rows = [("ip", ip_address(), FG)]
    # An optional peer to watch, so the page is useful on a board that is not
    # part of this particular network.
    if PEER_HOST:
        peer_up = pingable(PEER_HOST)
        rows.append((PEER_LABEL[:9], "up" if peer_up else "DOWN", OK if peer_up else BAD))
    rows.append(("internet", "up" if net_up else "DOWN", OK if net_up else BAD))
    return frame("NETWORK", rows, accent=OK if net_up else BAD)


def page_services():
    procs = sh("ps")
    rows = []
    for label, needle in (("picoclaw", "app_picoclaw"), ("clawdisp", "clawdisp.py")):
        alive = any(needle in line and "grep" not in line for line in procs.splitlines())
        rows.append((label, "run" if alive else "off", OK if alive else DIM))
    rows.append(("procs", len(procs.splitlines()) - 1, FG))
    return frame("SERVICES", rows, accent=DIM)


def page_message(text):
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, WIDTH, 30], fill=WARN)
    draw.text((8, 6), "MESSAGE", font=load_font(19), fill=BG)
    font = load_font(18)
    y = 40
    for line in wrap(draw, text, font, WIDTH - 16):
        if y > HEIGHT - 20:
            break
        draw.text((8, y), line, font=font, fill=FG)
        y += 22
    return img


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


PAGES = [page_system, page_network, page_services]


def build_frame(index):
    message = read_message()
    if message:
        return page_message(message)
    return PAGES[index % len(PAGES)]()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--save")
    ap.add_argument("--page", type=int, default=0)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(MSG_PATH), exist_ok=True)

    if args.once:
        img = build_frame(args.page)
        if args.save:
            img.save(args.save)
        panel = open_panel()
        panel.set_backlight(1)
        panel.display(img)
        print("drawn")
        return 0

    panel = open_panel()
    panel.set_backlight(1)
    index = 0
    last_turn = 0.0
    while True:
        now = time.time()
        # A pushed message pins the screen; page rotation resumes when it expires.
        if read_message() is None and now - last_turn >= PAGE_SECONDS:
            index += 1
            last_turn = now
        try:
            panel.display(build_frame(index))
        except Exception as err:                      # noqa: BLE001
            # A transient failure must not kill a display that runs for weeks.
            print(f"draw failed: {err}", file=sys.stderr)
            time.sleep(5)
        time.sleep(REFRESH_SECONDS)


def _selftest():
    """Renders every page without touching the panel.

    The GPIO lines are exclusive, so on the board itself the daemon owns them
    and nothing else can draw. Being able to check the rendering off-device is
    the only way to iterate on it at all.
    """
    import tempfile

    for name in ("page_system", "page_network", "page_services"):
        img = globals()[name]()
        assert img.size == (WIDTH, HEIGHT), (name, img.size)
        assert img.getpixel((5, 5)) != img.getpixel((5, HEIGHT - 5)), \
            f"{name}: title bar and body should not be the same colour"

    long_text = "mot " * 120
    img = page_message(long_text)
    assert img.size == (WIDTH, HEIGHT)

    # A single word wider than the panel must be split, not dropped or looped.
    draw = ImageDraw.Draw(Image.new("RGB", (WIDTH, HEIGHT)))
    lines = wrap(draw, "x" * 200, load_font(18), WIDTH - 16)
    assert len(lines) > 1, "an over-long word must be hard-split"
    assert "".join(lines) == "x" * 200, "hard-splitting must not lose characters"

    with tempfile.TemporaryDirectory() as d:
        global MSG_PATH
        original = MSG_PATH
        MSG_PATH = os.path.join(d, "msg.txt")
        try:
            assert read_message() is None, "no file means no message"
            with open(MSG_PATH, "w", encoding="utf-8") as f:
                f.write("  hello  ")
            assert read_message() == "hello", "message should be stripped"
            with open(MSG_PATH, "w", encoding="utf-8") as f:
                f.write("   ")
            assert read_message() is None, "a blank message is not a message"
            # Stale messages must release the screen back to the status pages.
            with open(MSG_PATH, "w", encoding="utf-8") as f:
                f.write("old news")
            old = time.time() - MSG_TTL - 60
            os.utime(MSG_PATH, (old, old))
            assert read_message() is None, "an expired message must be ignored"
        finally:
            MSG_PATH = original

    assert _f("1.5") == 1.5 and _f("n/a") == 0.0 and _f(None) == 0.0
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        sys.exit(main())
