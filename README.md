# claw-display

Turns the small ST7789 panel on a RISC-V board into an always-on status screen, plus a one-command way to push text onto it over SSH.

Written for a LicheeRV Nano class board with a 240x240 SPI panel, no framebuffer and no DRM: the display is driven entirely from userspace.

## What it does

`clawdisp.py` runs as a service and rotates through three pages every eight seconds.

| Page | Shows |
| --- | --- |
| SYSTEM | uptime, load, memory, disk, hostname |
| NETWORK | IP address, reachability of a peer host and of the internet |
| SERVICES | which known processes are alive, total process count |

Values turn amber then red past a threshold, so an abnormal screen is recognisable across a room without reading it.

Anything written to `/root/clawdisp/msg.txt` takes over the panel for five minutes, then the rotation resumes. That file is the interface: whoever holds SSH holds the screen.

```sh
export CLAW_HOST=root@10.0.0.5 CLAW_KEY=~/.ssh/claw_key
./say.sh "build finished"
./say.sh                        # clear it early
```

`show.py` is the one-shot version, for a single frame without running a daemon.

```sh
show.py --test                        # colour bars, proves the SPI link
show.py --title "STATUS" --text "..."
show.py --image photo.png
show.py --off                         # backlight off
```

## Install

Copy `clawdisp.py`, `show.py` to `/root/clawdisp/` on the board and the init script to `/etc/init.d/`:

```sh
scp clawdisp.py show.py root@board:/root/clawdisp/
scp S97clawdisp root@board:/etc/init.d/
ssh root@board 'chmod +x /etc/init.d/S97clawdisp && /etc/init.d/S97clawdisp start'
```

## Installing on a board with no panel yet

Install it anyway. With `CLAWDISP_ENABLE=0` in `/etc/default/clawdisp` the service starts, says why it is not running, and exits. Nothing runs, nothing is consumed, and the selftest still passes on the board, so the stack is proven ready rather than assumed ready. Wire a 240x240 ST7789 to an SPI bus, set the flag to 1, start the service.

The vendor-free backend in `panel.py` exists for exactly this case: a board with no vendor libraries, driven by nothing but `spidev` and the legacy GPIO sysfs. Pins come from `PANEL_SPI_BUS`, `PANEL_DC`, `PANEL_RST` and `PANEL_BL`.

## Checking a peer actually works

A reachable host is not a working one. If the peer serves DNS, set `CLAWDISP_DNS_CHECK=1` and the network page resolves a name through it rather than pinging it: a port that accepts connections proves a process is bound and nothing more. The probe is cached for a minute, because the panel redraws about once a second and a status line should not become a load source.

## Is it actually drawing?

A running process is not a working one, and a status display that can go silent without saying so defeats its own purpose. After every successful draw the daemon writes `/tmp/clawdisp.state`:

```
1785499422 page_system ok
```

Unix time of the last frame, the page that was drawn, and `ok` or the error. Compare it twice to tell a live panel from a process that is merely alive:

```sh
a=$(awk '{print $1}' /tmp/clawdisp.state); sleep 20
[ "$(awk '{print $1}' /tmp/clawdisp.state)" -gt "$a" ] && echo alive || echo stuck
```

Writing that file can never crash the display: an unwritable path is swallowed, because observability that takes down the thing it observes is worse than none.

## Things worth knowing before you adapt this

**The GPIO lines are exclusive.** Only one process can hold the panel's DC and RESET lines. A second one fails with `get gpio line failed`, which says nothing about the real cause. The daemon does not treat that as fatal: it waits and retries, recording the wait in the heartbeat, because a rival app may hold the panel for a second or for an hour and quitting on the first refusal leaves a black screen nobody notices.

**The driver and pin map are not reimplemented here.** They are read from the vendor app already on the board, so a pin change stays in one place. A wrong DC or RESET pin displays nothing at all, with no error to explain it, so guessing them is expensive.

**Rendering is testable off-device**, which matters because on the board the daemon owns the panel and nothing else can draw:

```sh
python clawdisp.py --selftest
python clawdisp.py --once --save frame.png    # on the board, also draws
```

The selftest renders every page, checks that an over-long word is split rather than dropped, and that a stale message releases the screen instead of pinning it forever.

**Nothing is hardcoded to one host.** `say.sh` reads `CLAW_HOST`, `CLAW_KEY` and `CLAW_SSH`, the last one because ssh sometimes lives inside WSL rather than on PATH. Messages travel base64-encoded: quotes, accents and newlines do not survive four nested shells otherwise.

## License

MIT
