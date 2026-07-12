#!/bin/bash
# Set STOP flag FIRST so the watchdog won't respawn the runner, then signal the runner to exit.
STOP_FLAG=/home/ec2-user/projects/trading/STOP
touch "$STOP_FLAG"
pkill -SIGINT -f "python runner.py" 2>/dev/null
echo "[$(date)] STOP flag set + SIGINT sent — runner stopping, watchdog will not restart" \
    >> /home/ec2-user/projects/trading/logs/runner_$(date +%Y-%m-%d).log
