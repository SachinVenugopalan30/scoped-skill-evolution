#!/bin/bash
# Wait for the gate calibration to write results/calibration/summary.json, then start Phase B.
# Detached so the calibration -> evolution handoff survives terminal/tool aborts.
set -u
export HOME="/Users/sachin"
cd "/Users/sachin/Desktop/Uni Courses/CSE 572 - Data Minining/project/scoped-skill-evolution"
export PATH="/Library/Frameworks/Python.framework/Versions/3.13/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
/usr/bin/caffeinate -dimsu -w $$ &  # keep the Mac awake until this watcher and its child exit
echo "=== phase_b watcher start $(date) ===" >> runs/logs/phase_b_auto.log
while [ ! -f results/calibration/summary.json ]; do
  sleep 60
done
echo "=== calibration summary detected $(date) ===" >> runs/logs/phase_b_auto.log
sleep 45  # let the calibration script restore results/static_selection.json and exit
uv run python run_phase_b.py --tau 1 >> runs/logs/phase_b_auto.log 2>&1  # PROTOCOL.md A1
echo "=== phase_b watcher end $(date) ===" >> runs/logs/phase_b_auto.log
