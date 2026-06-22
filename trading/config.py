#config.py
from datetime import timezone, timedelta

REST_BASE = "https://piconnect.flattrade.in/PiConnectAPI"
WS_URL    = "wss://piconnect.flattrade.in/PiConnectWSAPI/"

IST = timezone(timedelta(hours=5, minutes=30))

MARKET_OPEN_HOUR    = 9
MARKET_OPEN_MINUTE  = 15
EOD_EXIT_HOUR       = 15
EOD_EXIT_MINUTE     = 0

# Confirmed from GetIndexList
NIFTY_TOKEN     = "26000"
BANKNIFTY_TOKEN = "26009"

DEFAULT_INTERVAL_SECONDS = 300