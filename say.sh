#!/bin/sh
# Put text on the board's panel from another machine. It stays up until it goes
# stale, then the display returns to rotating its status pages.
#
#   ./say.sh "build finished"
#   ./say.sh                      clears the message immediately
#
# Configure once, per shell or in your profile:
#   export CLAW_HOST=root@10.0.0.5
#   export CLAW_KEY=~/.ssh/claw_key
#   export CLAW_SSH="wsl ssh"     if ssh lives in WSL rather than on PATH
CLAW_HOST="${CLAW_HOST:-root@claw.local}"
CLAW_KEY="${CLAW_KEY:-$HOME/.ssh/claw_key}"
CLAW_SSH="${CLAW_SSH:-ssh}"
MSG="$*"

run() {
    # MSYS_NO_PATHCONV stops Git Bash rewriting the key path into a Windows one.
    MSYS_NO_PATHCONV=1 $CLAW_SSH -o ConnectTimeout=15 -i "$CLAW_KEY" "$CLAW_HOST" "$1"
}

if [ -z "$MSG" ]; then
    run 'rm -f /root/clawdisp/msg.txt' && echo "message cleared, status pages resume"
    exit $?
fi

# base64 so quotes, accents and newlines survive every shell between here and
# the board's ash: they do not survive plain quoting.
ENC=$(printf '%s' "$MSG" | base64 | tr -d '\n')
run "mkdir -p /root/clawdisp && echo $ENC | base64 -d > /root/clawdisp/msg.txt" \
    && echo "on screen: $MSG"
