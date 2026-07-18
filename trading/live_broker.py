from __future__ import annotations
import logging, math
from datetime import datetime, timezone
from typing import Dict, Optional
from paper import PaperFill, _get_tick

log = logging.getLogger(__name__)

class LiveBroker:
    def __init__(self, client, tsym_map: Dict[str, str],
                 mode_map: Optional[Dict[str, str]] = None,
                 ti_map: Optional[Dict[str, float]] = None) -> None:
        self.client      = client
        self.tsym_map    = tsym_map
        self.mode_map    = mode_map or {}
        self.ti_map      = ti_map or {}   # real per-stock tick size from the exchange
        self.pending:    Dict[str, dict] = {}
        self._committed  = 0.0   # margin locked by orders placed this session

    def reset_day(self) -> None:
        self._committed = 0.0

    def _broker_cash(self) -> float:
        try:
            limits = self.client.get_limits()
            return float(limits.get("cash", 0) or 0)
        except Exception as e:
            log.warning("get_limits() failed: %s", e)
            return 0.0

    def _available_cash(self, mode: str) -> float:
        """Cash available for a new order, subtracting already-committed margin."""
        broker = self._broker_cash()
        # Deploy at most 85% of broker cash, then subtract what we already committed
        effective = broker * 0.85 - self._committed
        return max(0.0, effective)

    def _held_qty(self, symbol: str) -> int:
        try:
            holdings = self.client.holdings()
            if not isinstance(holdings, list):
                return 0
            tsym = self.tsym_map.get(symbol, f"{symbol}-EQ")
            for h in holdings:
                ht = h.get("tsym", "")
                if ht == tsym or ht.startswith(symbol):
                    return int(h.get("holdqty", 0) or 0)
        except Exception as e:
            log.warning("holdings() failed for %s: %s", symbol, e)
        return 0

    def net_position(self, symbol: str):
        """Signed net intraday qty from the exchange PositionBook. Returns None if the
        call fails (so callers can tell 'genuinely flat' from 'don't know')."""
        try:
            result = self.client.positions()
            if not isinstance(result, list):
                return None
            tsym = self.tsym_map.get(symbol, f"{symbol}-EQ")
            for p in result:
                pt = p.get("tsym", "")
                if pt == tsym or pt.startswith(symbol):
                    return int(p.get("netqty", 0) or 0)
            return 0
        except Exception as e:
            log.warning("positions() failed for %s: %s", symbol, e)
            return None

    def simulate_fill(self, symbol: str, side: str, qty: int,
                      mid_price: float, reason: str = "", quote=None,
                      cross_ticks: int = 0, is_exit: bool = False) -> Optional[PaperFill]:
        mode     = self.mode_map.get(symbol, "MIS")
        product  = "C" if mode == "CNC" else "I"
        tsym     = self.tsym_map.get(symbol, f"{symbol}-EQ")
        trantype = "B" if side.upper() == "BUY" else "S"

        t = self.ti_map.get(symbol) or _get_tick(mid_price)   # tick size (real ti if we have it)
        bid = quote[0] if quote else 0.0
        ask = quote[1] if quote else 0.0
        # Price off the latest best bid/ask ONLY — never LTP. First attempt sits AT the quote
        # (cross_ticks=0, no slippage given up); retries pass cross_ticks>0 to cross the book.
        # A BUY needs the ASK, a SELL needs the BID; if that side has no quote yet, DON'T place
        # (return None) so the caller waits/retries — we never guess a price from last-traded.
        if side.upper() == "BUY":
            if not ask or ask <= 0:
                log.warning("BUY %s not placed — no live ASK yet (never price off LTP); will retry", symbol)
                return None
            limit_price = round((math.ceil(round(ask / t, 8)) + cross_ticks) * t, 4)
        else:
            if not bid or bid <= 0:
                log.warning("SELL %s not placed — no live BID yet (never price off LTP); will retry", symbol)
                return None
            limit_price = round((math.floor(round(bid / t, 8)) - cross_ticks) * t, 4)

        # ── Capital check before an ENTRY BUY only ─────────────────────────
        # NEVER capital-gate or qty-reduce an EXIT/cover: closing an existing
        # position must always be allowed at full qty (it releases margin, it
        # doesn't consume buying power). Gating a short-cover on cash is what
        # caused the 9k-attempt retry loop — the stop could never fire.
        if side.upper() == "BUY" and not is_exit:
            avail = self._available_cash(mode)
            # MIS: broker gives ~4x leverage so actual margin = value/4
            # CNC: full value required
            margin_needed = (qty * limit_price / 4.0) if mode == "MIS" else (qty * limit_price)

            if margin_needed > avail:
                if avail <= 0:
                    log.error("%s BUY %s skipped — no capital left (committed=Rs%.2f)",
                              mode, symbol, self._committed)
                    return None
                # Reduce qty to fit available capital
                if mode == "MIS":
                    new_qty = int(avail * 4.0 / limit_price)
                else:
                    new_qty = int(avail / limit_price)
                log.warning("%s BUY %s: avail=Rs%.2f < needed=Rs%.2f — qty %d→%d",
                            mode, symbol, avail, margin_needed, qty, new_qty)
                qty = new_qty
                if qty <= 0:
                    log.error("%s BUY %s skipped — insufficient capital after reduction", mode, symbol)
                    return None

        # ── CNC SELL: verify demat holdings ───────────────────────────────
        if side.upper() == "SELL" and mode == "CNC":
            held = self._held_qty(symbol)
            if held <= 0:
                log.error("CNC SELL %s skipped — no holdings found in demat", symbol)
                return None
            if held < qty:
                log.warning("CNC SELL %s: holdqty=%d < qty=%d — reducing", symbol, held, qty)
                qty = held

        # ── Place order ────────────────────────────────────────────────────
        log.info("ORDER  %-12s  %-5s  %-4s  qty=%-6d  price=Rs%-7.2f  reason=%s",
                 symbol, side.upper(), mode, qty, limit_price, reason)

        try:
            resp = self.client.place_order(
                buy_or_sell   = trantype,
                product_type  = product,
                exchange      = "NSE",
                tradingsymbol = tsym,
                quantity      = qty,
                price_type    = "LMT",
                price         = limit_price,
                retention     = "IOC",
                remarks       = reason[:20] if reason else "",
            )
        except Exception as e:
            log.error("place_order EXCEPTION  %s  %s  qty=%d: %s — treating as NOT placed",
                      symbol, side.upper(), qty, e)
            return None

        if not isinstance(resp, dict) or resp.get("stat") != "Ok":
            log.error("ORDER REJECTED  %s  %s  qty=%d  reason=%s",
                      symbol, side.upper(), qty, resp.get("emsg", resp) if isinstance(resp, dict) else resp)
            return None

        norenordno = resp.get("norenordno", "")
        log.info("ACCEPTED  %s  norenordno=%s", symbol, norenordno)

        # ── Track committed margin by ENTRY-vs-EXIT intent, not BUY-vs-SELL ──
        # An entry (long BUY or short SELL) locks margin; an exit (long SELL or
        # short-cover BUY) releases it. Keying off side alone wrongly released
        # margin on a short entry and committed it on a cover.
        margin = (qty * limit_price / 4.0) if mode == "MIS" else (qty * limit_price)
        if not is_exit:
            self._committed += margin
            log.info("CAPITAL  committed=Rs%.2f  (added Rs%.2f for %s %s)",
                     self._committed, margin, mode, symbol)
        else:
            self._committed = max(0.0, self._committed - margin)
            log.info("CAPITAL  committed=Rs%.2f  (released Rs%.2f for %s %s)",
                     self._committed, margin, mode, symbol)

        if norenordno:
            self.pending[norenordno] = {
                "symbol": symbol, "side": side,
                "qty": qty, "est": limit_price, "mode": mode,
            }

        # Record the fill at the (marketable) limit price we placed — a far better estimate
        # than the mid; the confirmation later corrects it to the true average fill.
        return PaperFill(datetime.now(timezone.utc), symbol, side.upper(),
                         qty, limit_price, reason, norenordno)
