#!/usr/bin/env bash
# Detached overnight runner for the LP (liquidity-reward) bot. Auto-restarts on
# CRASH (non-zero exit). Each (re)start's _startup_cleanup cancels any orphaned
# resting orders first, so relaunching is safe. A CLEAN exit (SIGTERM -> graceful
# cancel_all -> exit 0) stops the wrapper.
#
#   start:  setsid nohup bash scripts/lp_overnight.sh >> logs/lp_overnight.log 2>&1 < /dev/null &
#   stop :  pkill -TERM -f 'src.execution.lp_run'      # graceful: cancels quotes, exits 0, wrapper stops
#
cd /home/ubuntu/last-shot || exit 1
while true; do
  echo "$(date -u +%FT%TZ) lp_overnight: starting lp_run"
  EXECUTOR_ARMED=1 .venv/bin/python -m src.execution.lp_run
  code=$?
  echo "$(date -u +%FT%TZ) lp_overnight: lp_run exited code=$code"
  if [ "$code" -eq 0 ]; then
    echo "$(date -u +%FT%TZ) lp_overnight: clean exit -> stopping wrapper"
    break
  fi
  echo "$(date -u +%FT%TZ) lp_overnight: non-zero exit -> restarting in 15s"
  sleep 15
done
