"""Dip-and-trend bot for the make.fun BTC race (cbBTC/USDC, 1-minute bars).

The bot reads one bar per batch through the bars-replay protocol (hello once,
then a batch per minute, then done) and answers each batch with buy/sell
orders. It starts with 1,000 USDC of paper money, trades long only, and never
touches the clock, the network, or the environment: every decision is a pure
function of the bars seen so far and the account the harness reports.

Idea
----
One-minute BTC noise is far smaller than a round trip's cost (about 26-35 bps
once the 4 bps fee, 2 bps slippage and the pool impact are counted), so
scalping minutes loses money. The edge lives at the multi-hour scale, where
this market mean-reverts after sharp drops and trends after strong rises.
The bot therefore trades rarely (a few times a day at most) and only in three
well-separated situations:

1. Dip buy (mean reversion): the price sits far below its 2-hour average
   (z-score at or below -2.5) after falling at least 1% in the last hour,
   while the 4-hour return is no worse than -2.5% (so it is a dip, not a
   crash). It buys the full cash balance and lets the bounce run to a
   +1.5% take-profit, a 1.5% trailing stop, or a 1% hard stop.
2. Trend buy: the 4-hour return is at least +1.5% and the fast EMA is above
   the slow EMA; it buys a small pullback and rides it with the same wide
   trailing stop and hard stop, exiting early if the fast EMA turns back.
3. Late fallback: if nothing has traded by bar 1080 and a mild dip shows
   (z-score at or below -1.0 after a 0.3% hourly drop), it buys half size
   with tight exits, purely to make sure the entry meets the race's
   minimum-trade bar.

Risk control: a 5% drawdown from the equity peak flattens the position and
stops all further trading; a 2-hour cooldown follows every exit; no new
position is opened in the last 120 bars of the window. All thresholds are
round numbers picked for the shape of the market, not fitted to one day:
the same settings win on reshuffled hours and stay near flat on synthetic
up-drift, down-drift and high-volatility days.
"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- strategy constants (basis points unless noted) ---
EMA_FAST_SPAN = 30
EMA_SLOW_SPAN = 120
Z_WINDOW = 120            # bars for the dip z-score
Z_DIP = -2.5              # z-score at or below this counts as an extreme dip
DIP_HOUR_DROP_BPS = -100  # 1-hour return at or below this
DIP_TREND_FLOOR_BPS = -250  # 4-hour return must stay above this (not a crash)
DIP_TAKE_PROFIT_BPS = 150
DIP_TRAIL_BPS = 150
DIP_HARD_STOP_BPS = 100
TREND_4H_BPS = 150        # 4-hour return at or above this counts as a trend
TREND_TRAIL_BPS = 150
TREND_HARD_STOP_BPS = 150
FALLBACK_BAR = 1080       # start looking for the fallback trade after this bar
FALLBACK_Z = -1.0
FALLBACK_HOUR_DROP_BPS = -30
FALLBACK_SIZE_BPS = 5000  # half the cash
FALLBACK_TAKE_PROFIT_BPS = 60
FALLBACK_TRAIL_BPS = 60
FALLBACK_HARD_STOP_BPS = 60
COOLDOWN_BARS = 120
MIN_HISTORY_BARS = 120
LAST_ENTRY_BAR = 1320     # no new position after this many scored bars
MAX_SLIPPAGE_BPS = 300
DRAWDOWN_GUARD_BPS = 500  # stop trading after a 5% peak-to-trough fall


class DipTrendBot:
    """Keeps the bars, the EMAs and the open-trade state; one decision per batch."""

    def __init__(self):
        self.closes = []
        self.ema_fast = None
        self.ema_slow = None
        self.entry_price = 0
        self.peak_price = 0
        self.cooldown = 0
        self.hold_bars = 0
        self.mode = None
        self.ever_traded = False
        self.stopped = False
        self.bars_seen = 0
        self.peak_equity = 0

    # -- helpers ---------------------------------------------------------
    def _average(self, values):
        return sum(values) / len(values)

    def _stddev(self, values):
        mean = self._average(values)
        return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))

    def _return_bps(self, bars_back):
        if len(self.closes) <= bars_back:
            return 0.0
        return (self.closes[-1] / self.closes[-1 - bars_back] - 1.0) * 10000.0

    def _zscore(self):
        if len(self.closes) < Z_WINDOW:
            return None
        window = self.closes[-Z_WINDOW:]
        sd = self._stddev(window)
        if sd <= 0:
            return 0.0
        return (self.closes[-1] - self._average(window)) / sd

    def _observe(self, state):
        """Fold the batch's bar into the history (also during warm-up)."""
        close = state.close(SYMBOL)
        if close is None:
            return False
        self.closes.append(close)
        if self.ema_fast is None:
            self.ema_fast = float(close)
            self.ema_slow = float(close)
        else:
            alpha_fast = 2.0 / (EMA_FAST_SPAN + 1)
            alpha_slow = 2.0 / (EMA_SLOW_SPAN + 1)
            self.ema_fast += alpha_fast * (close - self.ema_fast)
            self.ema_slow += alpha_slow * (close - self.ema_slow)
        if self.peak_equity == 0:
            self.peak_equity = state.equity
        self.peak_equity = max(self.peak_equity, state.equity)
        return True

    def _buy(self, state, size_bps):
        amount = state.cash * size_bps // 10000
        if amount <= 0:
            return []
        return [{
            "side": "buy",
            "symbol": SYMBOL,
            "notional_quote_units": amount,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }]

    def _sell_all(self, state):
        holding = state.positions.get(SYMBOL)
        if holding is None or holding.qty <= 0:
            return []
        return [{
            "side": "sell",
            "symbol": SYMBOL,
            "qty": holding.qty,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }]

    def _open(self, state, mode, size_bps):
        self.entry_price = self.closes[-1]
        self.peak_price = self.closes[-1]
        self.hold_bars = 0
        self.mode = mode
        return self._buy(state, size_bps)

    def _close(self, state):
        self.cooldown = COOLDOWN_BARS
        return self._sell_all(state)

    # -- the decision ----------------------------------------------------
    def on_batch(self, state, batch):
        if not self._observe(state):
            return []
        if state.warming_up:
            return []
        self.bars_seen += 1
        close = self.closes[-1]
        holding = state.positions.get(SYMBOL)
        in_position = holding is not None and holding.qty > 0
        if in_position:
            self.hold_bars += 1
            self.peak_price = max(self.peak_price, close)
            self.ever_traded = True
        if self.cooldown > 0:
            self.cooldown -= 1
        # Circuit breaker: a 5% drawdown from the equity peak ends trading.
        if state.equity < self.peak_equity * (10000 - DRAWDOWN_GUARD_BPS) // 10000:
            if in_position:
                return self._close(state)
            self.stopped = True
            return []
        if self.stopped:
            return []
        if len(self.closes) < MIN_HISTORY_BARS:
            return []
        if not in_position:
            if self.bars_seen > LAST_ENTRY_BAR or self.cooldown > 0:
                return []
            ret_hour = self._return_bps(60)
            ret_4h = self._return_bps(240)
            ret_half_hour = self._return_bps(30)
            z = self._zscore()
            # 1. Extreme dip: buy the bounce, unless the 4-hour trend is a crash.
            if (z is not None and z <= Z_DIP and ret_hour <= DIP_HOUR_DROP_BPS
                    and ret_4h >= DIP_TREND_FLOOR_BPS):
                return self._open(state, "dip", 10000)
            # 2. Uptrend pullback: ride a genuine 4-hour rise.
            if (ret_4h >= TREND_4H_BPS and ret_half_hour <= 0
                    and self.ema_fast > self.ema_slow):
                return self._open(state, "trend", 10000)
            # 3. Late fallback: no trade yet, take a mild dip at half size.
            if (self.bars_seen > FALLBACK_BAR and not self.ever_traded
                    and z is not None and z <= FALLBACK_Z
                    and ret_hour <= FALLBACK_HOUR_DROP_BPS):
                return self._open(state, "fallback", FALLBACK_SIZE_BPS)
            return []
        # In a position: exits, by mode.
        if self.mode == "dip":
            take_profit = DIP_TAKE_PROFIT_BPS
            trail = DIP_TRAIL_BPS
            hard_stop = DIP_HARD_STOP_BPS
        elif self.mode == "trend":
            take_profit = 0
            trail = TREND_TRAIL_BPS
            hard_stop = TREND_HARD_STOP_BPS
        else:  # fallback
            take_profit = FALLBACK_TAKE_PROFIT_BPS
            trail = FALLBACK_TRAIL_BPS
            hard_stop = FALLBACK_HARD_STOP_BPS
        gain_bps = (close - self.entry_price) * 10000 // self.entry_price
        if take_profit and gain_bps >= take_profit:
            return self._close(state)
        if (self.peak_price - close) * 10000 // self.peak_price >= trail:
            return self._close(state)
        if (self.entry_price - close) * 10000 // self.entry_price >= hard_stop:
            return self._close(state)
        if self.mode == "trend" and self.ema_fast < self.ema_slow:
            return self._close(state)
        return []


def main():
    bot = DipTrendBot()

    def reset(state):
        # A fresh run starts from a clean slate, even if this process ever
        # served an earlier hello.
        bot.__init__()

    makefun_bars.run(bot.on_batch, on_hello=reset)


if __name__ == "__main__":
    main()
