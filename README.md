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

## Things worth knowing before you adapt this

**The GPIO lines are exclusive.** Only one process can hold the panel's DC and RESET lines. A second one fails with `get gpio line failed`, which says nothing about the real cause. If another app already drives the screen, stop it first; the init script refuses to start rather than produce that error.

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
