#!/bin/sh
# Start the virtual screen (a real window passes LinkedIn's checks more often than headless),
# a small window manager and the VNC server, then the bot.
set -e
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1280x900x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
sleep 1
fluxbox >/tmp/fluxbox.log 2>&1 &
# VNC is only published on the host's loopback (see docker-compose.yml): reach it with
# "ssh -L 5901:localhost:5901 <server>". It always asks for a password: VNC_PASSWORD, or
# a random one saved in /data/vnc-password (readable only by you) on the first start.
umask 077
if [ -n "$VNC_PASSWORD" ]; then
  printf '%s\n' "$VNC_PASSWORD" > /tmp/vnc-password
  PASSFILE=/tmp/vnc-password
else
  [ -s /data/vnc-password ] || head -c 12 /dev/urandom | base64 | tr -d '/+=' > /data/vnc-password
  PASSFILE=/data/vnc-password
fi
unset VNC_PASSWORD
x11vnc -display :99 -forever -shared -rfbport 5900 -passwdfile "$PASSFILE" -quiet \
  >/tmp/x11vnc.log 2>&1 &
umask 022
exec "$@"
