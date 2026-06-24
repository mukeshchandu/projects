#!/bin/bash
pkill -SIGINT -f "python runner.py" 2>/dev/null
echo "[$(date)] Runner stopped" >> /home/ec2-user/projects/trading/logs/runner_$(date +%Y-%m-%d).log
