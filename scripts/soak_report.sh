#!/usr/bin/env bash
# Hourly CP2 soak report. Run by the lastshot-soak-report systemd timer.
# Appends to logs/soak_report.log and emits to journald (journalctl -u
# lastshot-soak-report). Mirrors the paper-trading hourly loop, for the live arb
# soak: lock/abort/unwind rates, slippage, the PM->Betfair gap, and live-vs-
# shadow capture.
cd /home/ubuntu/last-shot || exit 1
ts="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
{
  echo "================ soak report ${ts} ================"
  .venv/bin/python -m analysis.live_capture_report 2>&1 || true
  echo ""
} | tee -a logs/soak_report.log
