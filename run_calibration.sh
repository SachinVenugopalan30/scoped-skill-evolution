#!/bin/bash
# Detached gate-calibration runner. Launched under launchctl so it survives terminal/tool aborts.
set -u
export HOME="/Users/sachin"
cd "/Users/sachin/Desktop/Uni Courses/CSE 572 - Data Minining/project/scoped-skill-evolution"
export PATH="/Library/Frameworks/Python.framework/Versions/3.13/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
/usr/bin/caffeinate -dimsu -w $$ &  # keep the Mac awake until this script exits
echo "=== calibration start $(date) ===" >> runs/logs/calib_runner.log
for i in 1 2 3 4 5; do
  echo "--- calib-$i start $(date) ---" >> runs/logs/calib_runner.log
  uv run python evolve.py static --partition selection --n 34 --seed 0 --salt calib-$i > runs/logs/calib_$i.log 2>&1
  echo "calib-$i exit $? $(date)" >> runs/logs/calib_runner.log
  cp -f results/static_selection.json results/calibration/calib_$i.json
done
cp -f results/calibration/baseline_selection.json results/static_selection.json
uv run python calibrate_gate.py > runs/logs/calib_summary.log 2>&1
echo "=== calibration end $(date) ===" >> runs/logs/calib_runner.log
