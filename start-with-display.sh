#!/bin/sh
# Detect the live X display dynamically (the display number drifts between :0
# and :1 across logins) and exec the desktop client against it.
#
#   --wait-only : block up to 60s for any X socket to appear, then exit 0/1.
#                 Used as ExecStartPre so the unit doesn't launch before X.
#   (no args)   : pick the live display, export DISPLAY, exec the client.

if [ "$1" = "--wait-only" ]; then
    i=0
    while [ "$i" -lt 60 ]; do
        for s in /tmp/.X11-unix/X*; do
            [ -S "$s" ] && exit 0
        done
        sleep 1
        i=$((i + 1))
    done
    exit 1
fi

for s in /tmp/.X11-unix/X*; do
    [ -S "$s" ] && DISPLAY=":${s##*/X}" && export DISPLAY && break
done
cd /media/varingait/Lobotomite/Repository/AI/clients/desktop || exit 1
exec ./venv/bin/python main.py
