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
# Written after every successful draw, so "is the screen alive" is answerable
# from another machine without eyes on the panel.
STATE_PATH = os.environ.get("CLAWDISP_STATE", "/tmp/clawdisp.state")
# Processes to show on the services page: "label:needle" separated by commas,
# where needle is what to look for in ps output. Defaults to this daemon alone,
# since nothing else can be assumed to exist on someone else's board.
WATCH = os.environ.get("CLAWDISP_WATCH", "clawdisp:clawdisp.py")
# When the peer serves DNS, check that it still resolves rather than merely
# answers a ping. Off by default: not every peer is a resolver.
DNS_CHECK = os.environ.get("CLAWDISP_DNS_CHECK", "0") == "1"
DNS_PROBE_NAME = os.environ.get("CLAWDISP_DNS_NAME", "example.com")
DNS_TTL = int(os.environ.get("CLAWDISP_DNS_TTL", "60"))
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


# Probes that cost a network round trip are cached: the panel redraws about
# once a second and hammering a DNS server to draw a status line would make the
# display a load source rather than an observer of one.
_probe_cache = {}


def cached(key, ttl, fn):
    now = time.time()
    hit = _probe_cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    _probe_cache[key] = (now, value)
    return value


def dns_resolves(server, name=DNS_PROBE_NAME):
    """Answers whether the server is serving DNS, not whether it listens.

    A port that accepts connections proves a process is bound to it and
    nothing more. What counts is whether a query gets an answer.

    Measured against busybox nslookup rather than assumed. Three things that
    matter and are not obvious:
      * the exit status is 0 even when the server is unreachable, so it
        carries no information and is not used;
      * a dead server prints "connection timed out", with no answer section;
      * NXDOMAIN is a *successful* exchange. A resolver that says "no such
        name" is working. Treating that as a failure would report an outage
        whenever the probed name is blocked, which on a DNS blocker is a
        thing that happens on purpose.
    """
    out = sh(f"nslookup {name} {server} 2>&1")
    if not out or "timed out" in out or "no servers could be reached" in out:
        return False
    # Either an answer section, or an authoritative "no such name". Both prove
    # the server answered.
    return "Name:" in out.split("Address:", 1)[-1] or "can't find" in out


def page_network():
    net_up = pingable(INTERNET_HOST)
    rows = [("ip", ip_address(), FG)]
    # An optional peer to watch, so the page is useful on a board that is not
    # part of this particular network.
    if PEER_HOST:
        peer_up = pingable(PEER_HOST)
        rows.append((PEER_LABEL[:9], "up" if peer_up else "DOWN", OK if peer_up else BAD))
        # Reachable is not the same as working. If that peer serves DNS, the
        # useful question is whether it still resolves.
        if DNS_CHECK and peer_up:
            ok = cached("dns", DNS_TTL, lambda: dns_resolves(PEER_HOST))
            rows.append(("dns", "ok" if ok else "FAIL", OK if ok else BAD))
    rows.append(("internet", "up" if net_up else "DOWN", OK if net_up else BAD))
    return frame("NETWORK", rows, accent=OK if net_up else BAD)


def watched():
    """Processes to report on, as label:needle pairs.

    Absent ones are drawn dim rather than red: a service that is off on
    purpose must not look like an incident, or the screen teaches you to
    ignore it.
    """
    pairs = []
    for item in WATCH.split(","):
        item = item.strip()
        if not item:
            continue
        label, _, needle = item.partition(":")
        pairs.append((label.strip()[:9], (needle or label).strip()))
    return pairs[:5]  # five rows is what fits


def page_services():
    procs = sh("ps")
    lines = [l for l in procs.splitlines() if "grep" not in l]
    rows = []
    for label, needle in watched():
        alive = any(needle in line for line in lines)
        rows.append((label, "run" if alive else "off", OK if alive else DIM))
    rows.append(("procs", max(len(lines) - 1, 0), FG))
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


def write_heartbeat(index, error):
    """Records that a frame actually reached the panel.

    A running process is not a working one: the whole point of this display is
    to reveal that kind of silence, so it must not be able to hide its own. The
    file carries the time of the last successful draw, so a reader can tell a
    live screen from a process that is merely alive.
    """
    page = "message" if read_message() else PAGES[index % len(PAGES)].__name__
    line = f"{int(time.time())} {page} {'ok' if error is None else 'error: ' + error[:80]}\n"
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass  # never let observability kill the display


def build_frame(index):
    message = read_message()
    if message:
        return page_message(message)
    return PAGES[index % len(PAGES)]()


def wait_for_panel(retry_seconds=20):
    """Waits for the panel instead of giving up on it.

    The GPIO lines are exclusive, so another app holding them makes opening
    fail. That app may hold them for a second or for an hour, and a display
    that quits on the first refusal leaves a black screen nobody notices.
    Waiting costs nothing and recovers on its own.
    """
    announced = False
    while True:
        try:
            return open_panel()
        except Exception as err:                      # noqa: BLE001
            if not announced:
                print(f"panel busy ({err}), waiting for it to be released", file=sys.stderr)
                announced = True
            write_heartbeat(0, f"panel busy: {err}")
            time.sleep(retry_seconds)


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

    panel = wait_for_panel()
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
            write_heartbeat(index, None)
        except Exception as err:                      # noqa: BLE001
            # A transient failure must not kill a display that runs for weeks.
            print(f"draw failed: {err}", file=sys.stderr)
            write_heartbeat(index, str(err))
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

    # The cache must actually prevent repeat calls: without it the panel would
    # query a DNS server about once a second just to draw one line.
    calls = []
    _probe_cache.clear()
    for _ in range(5):
        cached("k", 60, lambda: calls.append(1) or "v")
    assert len(calls) == 1, f"cached() called through {len(calls)} times"
    assert cached("k", 60, lambda: "other") == "v", "a cache hit must return the stored value"
    _probe_cache["k"] = (0.0, "stale")
    assert cached("k", 60, lambda: "fresh") == "fresh", "an expired entry must be refetched"
    _probe_cache.clear()

    # These three strings are captured verbatim from busybox nslookup on the
    # board, not invented. The probe was written against invented output once
    # and got NXDOMAIN wrong, so the fixtures are now the real thing.
    WORKING = (
        "Server:\t\t192.168.1.46\nAddress:\t192.168.1.46:53\n\n"
        "Non-authoritative answer:\nName:\texample.com\nAddress: 104.20.23.154\n"
    )
    DEAD = ";; connection timed out; no servers could be reached\n"
    NXDOMAIN = (
        "Server:\t\t192.168.1.46\nAddress:\t192.168.1.46:53\n\n"
        "** server can't find nxdomain-test-zzz.invalid: NXDOMAIN\n"
    )

    real_sh = globals()["sh"]
    try:
        globals()["sh"] = lambda cmd: WORKING
        assert dns_resolves("x") is True, "a normal answer is a working resolver"
        globals()["sh"] = lambda cmd: DEAD
        assert dns_resolves("x") is False, "an unreachable server is a failure"
        globals()["sh"] = lambda cmd: NXDOMAIN
        assert dns_resolves("x") is True, \
            "NXDOMAIN is a successful exchange: the resolver answered"
        globals()["sh"] = lambda cmd: ""
        assert dns_resolves("x") is False, "no output at all is a failure"
    finally:
        globals()["sh"] = real_sh

    # A busy panel must be waited out, not treated as fatal, and the wait must
    # be visible in the heartbeat rather than silent.
    real_open = globals()["open_panel"]
    attempts = []
    try:
        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("get gpio line failed")
            return "panel"
        globals()["open_panel"] = flaky
        with tempfile.TemporaryDirectory() as d:
            keep = STATE_PATH
            globals()["STATE_PATH"] = os.path.join(d, "s")
            try:
                assert wait_for_panel(retry_seconds=0) == "panel"
                assert len(attempts) == 3, attempts
                assert "panel busy" in open(STATE_PATH, encoding="utf-8").read()
            finally:
                globals()["STATE_PATH"] = keep
    finally:
        globals()["open_panel"] = real_open

    global WATCH
    original_watch = WATCH
    try:
        WATCH = "bot:pkkls_bot, dns:dnsmasq ,, plain"
        pairs = watched()
        assert pairs == [("bot", "pkkls_bot"), ("dns", "dnsmasq"), ("plain", "plain")], pairs
        WATCH = ",".join(f"s{i}:x{i}" for i in range(9))
        assert len(watched()) == 5, "more rows than fit must be dropped, not overflow"
        WATCH = ""
        assert watched() == [], "an empty list is valid, not a crash"
        assert page_services().size == (WIDTH, HEIGHT)
    finally:
        WATCH = original_watch

    # The heartbeat must record the page drawn and survive an unwritable path,
    # because observability that can crash the display is worse than none.
    with tempfile.TemporaryDirectory() as d:
        original_state = STATE_PATH
        globals()["STATE_PATH"] = os.path.join(d, "state")
        try:
            write_heartbeat(0, None)
            recorded = open(STATE_PATH, encoding="utf-8").read().split()
            assert recorded[1] == "page_system", recorded
            assert recorded[2] == "ok", recorded
            assert int(recorded[0]) > 0, recorded

            write_heartbeat(1, "spi closed")
            assert "error" in open(STATE_PATH, encoding="utf-8").read()

            globals()["STATE_PATH"] = os.path.join(d, "nope", "state")
            write_heartbeat(0, None)  # must not raise
        finally:
            globals()["STATE_PATH"] = original_state

    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        sys.exit(main())
