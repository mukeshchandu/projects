#!/bin/bash
cd /home/ec2-user/projects/trading
source venv/bin/activate

DATE=$(date +%Y-%m-%d)
STOP_FLAG=/home/ec2-user/projects/trading/STOP

# Clear any stale stop flag from a previous session
rm -f "$STOP_FLAG"

echo "[$(date)] Generating token..." >> logs/token_${DATE}.log
python generate_token.py >> logs/token_${DATE}.log 2>&1

pkill -f "python runner.py" 2>/dev/null
sleep 2

# Restart runner if it crashes before 3:05 PM IST — unless STOP flag is set
(while true; do
    if [ -f "$STOP_FLAG" ]; then
        echo "[$(date)] STOP flag detected — watchdog exiting, no restart" >> logs/runner_${DATE}.log
        rm -f "$STOP_FLAG"
        break
    fi
    HOUR=$(TZ="Asia/Kolkata" date +%H)
    MIN=$(TZ="Asia/Kolkata" date +%M)
    if [ "$HOUR" -gt 15 ] || { [ "$HOUR" -eq 15 ] && [ "$MIN" -ge 5 ]; }; then
        break
    fi
    echo "[$(date)] Starting runner..." >> logs/runner_${DATE}.log
    LIVE_MODE=1 python runner.py
    echo "[$(date)] Runner exited, restarting in 10s..." >> logs/runner_${DATE}.log
    sleep 10
done) &

echo "[$(date)] Watchdog started" >> logs/runner_${DATE}.log
