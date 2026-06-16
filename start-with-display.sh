#!/bin/sh
# Detect the live X display dynamically and exec the desktop client against it.
#
# The display number drifts between :0 and :1 across logins, and at cold boot a
# gdm greeter server can briefly occupy :0 before the autologin session lands on
# :1. So we don't just take the first /tmp/.X11-unix/X* socket -- we probe each
# candidate with the configured XAUTHORITY cookie and pick the first display
# that actually accepts an authenticated connection.
#
#   --wait-only : block up to 60s for a usable display, then exit 0/1. Used as
#                 ExecStartPre so the unit doesn't launch before X is ready.
#   (no args)   : pick the usable display, export DISPLAY, exec the client.

: "${XAUTHORITY:=/run/user/1000/gdm/Xauthority}"
export XAUTHORITY

# Echo the first display that accepts an authenticated connection, else nothing.
find_display() {
    for s in /tmp/.X11-unix/X*; do
        [ -S "$s" ] || continue
        d=":${s##*/X}"
        if xset -display "$d" q >/dev/null 2>&1; then
            echo "$d"
            return 0
        fi
    done
    return 1
}

if [ "$1" = "--wait-only" ]; then
    i=0
    while [ "$i" -lt 60 ]; do
        find_display >/dev/null && exit 0
        sleep 1
        i=$((i + 1))
    done
    exit 1
fi

DISPLAY="$(find_display)"
export DISPLAY
cd /media/varingait/Lobotomite/Repository/AI/clients/desktop || exit 1
exec ./venv/bin/python main.py
