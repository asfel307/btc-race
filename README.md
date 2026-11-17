# Dip-and-Trend BTC Bot

[![make.fun](https://make.fun/badge/btc-race-24h-20261117-0603.svg)](https://make.fun/c/btc-race-24h-20261117-0603)
[![rank](https://make.fun/badge/btc-race-24h-20261117-0603/asfel307/btc-race/rank.svg)](https://make.fun/c/btc-race-24h-20261117-0603)
[![reward](https://make.fun/badge/btc-race-24h-20261117-0603/asfel307/btc-race/reward.svg)](https://make.fun/c/btc-race-24h-20261117-0603)

Entry for the [make.fun BTC race bounty](https://make.fun/c/btc-race-24h-20261117-0603).

A trading bot for the make.fun BTC race: it trades Coinbase Wrapped BTC
(cbBTC) against USDC on 1-minute on-chain bars, starting with $1,000 of
paper money.

## How it works

One-minute BTC noise is far smaller than a round trip's cost (about 26-35
bps once the 4 bps fee, 2 bps slippage and pool impact are counted), so the
bot never scalps minutes. It trades rarely, only in three situations:

1. **Dip buy** — the price sits far below its 2-hour average (z-score -2.5
   or lower) after falling at least 1% in the last hour, while the 4-hour
   return is no worse than -2.5%. Full size, exits at +1.5% take-profit, a
   1.5% trailing stop, or a 1% hard stop.
2. **Trend buy** — the 4-hour return is +1.5% or better and the fast EMA
   is above the slow EMA; it buys a pullback and rides it with the same
   wide trailing stop.
3. **Late fallback** — if nothing has traded by bar 1080, it takes a mild
   dip at half size with tight exits, so the entry always meets the race's
   minimum-trade bar.

A 5% drawdown from the equity peak flattens the position and stops all
further trading. No new position is opened in the last 120 bars.

## Files

- `main.py` — the bot (Python 3.13, standard library only).
- `vendor/makefun_bars.py` — the kit's protocol shim, copied byte for byte.

## Local result (dev window)

Net PnL **+$13.88** on the frozen 24h dev window, 1 round trip (2 fills),
max drawdown 0.66%. Best baseline (momentum) makes +$2.45 on the same
window. Validated on reshuffled hourly blocks (median +$6.69 over 200
resamples) and on synthetic up-drift, down-drift and high-volatility days.
