#backtest/charges.py
"""
Actual Flattrade charges (zero brokerage).
All rates sourced from https://flattrade.in/brokerage-calculator/
"""

from __future__ import annotations

SEBI_RATE = 10 / 1e7  # ₹10 per crore of turnover


def calc_charges(segment: str, buy_value: float, sell_value: float) -> float:
    """
    Returns total round-trip statutory charges in ₹.

    segment:    'fno_options' | 'fno_futures' | 'equity_intraday' | 'equity_delivery'
    buy_value:  qty × buy_price
    sell_value: qty × sell_price
    """
    total = 0.0
    turnover = buy_value + sell_value

    if segment == "fno_options":
        stt            = sell_value * 0.0015          # 0.15% on sell premium
        exchange       = turnover   * 0.0003553        # 0.03553% on total turnover
        stamp          = buy_value  * 0.00003          # 0.003% on buy
        sebi           = turnover   * SEBI_RATE
        gst            = (exchange + sebi) * 0.18
        total          = stt + exchange + stamp + sebi + gst

    elif segment == "fno_futures":
        stt            = sell_value * 0.0005           # 0.05% on sell
        exchange       = turnover   * 0.0000183        # 0.00183% on turnover
        stamp          = buy_value  * 0.00002          # 0.002% on buy
        sebi           = turnover   * SEBI_RATE
        gst            = (exchange + sebi) * 0.18
        total          = stt + exchange + stamp + sebi + gst

    elif segment == "equity_intraday":
        stt            = sell_value * 0.00025          # 0.025% on sell
        exchange       = turnover   * 0.0000307        # 0.00307% on turnover (NSE)
        stamp          = buy_value  * 0.00003          # 0.003% on buy
        sebi           = turnover   * SEBI_RATE
        gst            = (exchange + sebi) * 0.18
        total          = stt + exchange + stamp + sebi + gst

    elif segment == "equity_delivery":
        stt            = turnover   * 0.001            # 0.1% on both sides
        exchange       = turnover   * 0.0000307
        stamp          = buy_value  * 0.00015          # 0.015% on buy
        sebi           = turnover   * SEBI_RATE
        gst            = (exchange + sebi) * 0.18
        total          = stt + exchange + stamp + sebi + gst

    return round(total, 4)