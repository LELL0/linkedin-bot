#!/usr/bin/env bash
# Keeps the bot alive: blocks sleep and restarts it if it exits.
cd "$(dirname "$0")" || exit 1
exec systemd-inhibit --what=sleep:idle:handle-lid-switch --who=linkedin-bot --why="Answering LinkedIn messages" \
  bash -c 'while true; do uv run python -m bot run; echo "bot exited, restarting in 5s"; sleep 5; done'
