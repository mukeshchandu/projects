#!/bin/bash
# start_options_logger.sh — launch the standalone options-chain data logger.
# Independent of the trading runner; only RECORDS data, never trades.
# Assumes start_trading.sh already generated today's session token (same .env).
cd /home/ec2-user/projects/trading
source venv/bin/activate

mkdir -p logs data/options
DATE=$(date +%Y-%m-%d)

# Singleton guard so cron + manual runs don't stack two loggers.
exec 8>/home/ec2-user/projects/trading/.options_logger.lock
if ! flock -n 8; then
    echo "[$(date)] start_options_logger.sh: already running — exiting" \
        >> logs/options_${DATE}.log
    exit 0
fi

pkill -f "python options_logger.py" 2>/dev/null
sleep 1

# Restart on crash until EOD (the logger self-exits at 15:30 IST).
(while true; do
    HOUR=$(TZ="Asia/Kolkata" date +%H)
    MIN=$(TZ="Asia/Kolkata" date +%M)
    if [ "$HOUR" -gt 15 ] || { [ "$HOUR" -eq 15 ] && [ "$MIN" -ge 30 ]; }; then
        break
    fi
    echo "[$(date)] Starting options_logger..." >> logs/options_${DATE}.log
    python options_logger.py >> logs/options_${DATE}.log 2>&1
    echo "[$(date)] options_logger exited, restarting in 10s..." >> logs/options_${DATE}.log
    sleep 10
done) &

echo "[$(date)] Options-logger watchdog started" >> logs/options_${DATE}.log
