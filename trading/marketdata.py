#marketdata.py
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Deque, Dict, Optional

from config import IST


@dataclass
class Tick:
    ts:     datetime
    symbol: str
    ltp:    float
    volume: float = 0.0
    raw:    Dict[str, object] = field(default_factory=dict)


@dataclass
class Candle:
    start:  datetime
    open:   float
    high:   float
    low:    float
    close:  float
    volume: float = 0.0


class CandleBuilder:
    def __init__(self, interval_seconds: int = 300) -> None:
        self.interval_seconds = interval_seconds
        self.current: Optional[Candle] = None

    def update(self, tick: Tick) -> Optional[Candle]:
        bucket_ts    = int(tick.ts.timestamp()) // self.interval_seconds * self.interval_seconds
        bucket_start = datetime.fromtimestamp(bucket_ts, tz=IST)

        if self.current is None:
            self.current = Candle(bucket_start, tick.ltp, tick.ltp, tick.ltp, tick.ltp)
            return None

        bucket_end = datetime.fromtimestamp(
            bucket_ts + self.interval_seconds, tz=IST
        )
        if tick.ts >= bucket_end:
            finished     = self.current
            self.current = Candle(bucket_start, tick.ltp, tick.ltp, tick.ltp, tick.ltp)
            return finished

        self.current.high   = max(self.current.high, tick.ltp)
        self.current.low    = min(self.current.low, tick.ltp)
        self.current.close  = tick.ltp
        self.current.volume += tick.volume
        return None


class TickBuffer:
    def __init__(self, maxlen: int = 5000) -> None:
        self.buffer: Deque[Tick] = deque(maxlen=maxlen)

    def add(self, tick: Tick) -> None:
        self.buffer.append(tick)

    def latest(self) -> Optional[Tick]:
        return self.buffer[-1] if self.buffer else None