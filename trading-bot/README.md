# hftbot: block-cadence market making with Jev judgments

A 24/7 market making bot for on-chain order books that run at block speed (Monad ~300 ms, Solana ~400 ms).
It follows the architecture from RohOnChain's "How to Use Jev to Build a 24/7 HFT Trading System",
with the article's errors fixed and the lessons from Jarrod Watts' open source
[jev-trader](https://github.com/jarrodwatts/jev-trader) folded in.

```
market data -> feature engine -> deterministic snapshot -> Jev (6 parallel judgments)
            -> policy engine (your thresholds) -> Avellaneda-Stoikov quotes -> HARD RISK VETO -> execution
```

Code calculates, Jev interprets, code decides. Jev never trades: it answers six narrow questions about
a compact state (regime, direction, toxic flow, liquidity stress, quote environment, inventory pressure).
Everything else, including every risk limit, is deterministic code.

## Status: read this before anything else

- **Built and tested (60 tests):** state engine, Jev client (real `typesafe-sdk` API), rule-based fallback,
  policy, A-S pricing, risk engine with latched kill switch, fallback ladder, paper venue with queue
  modeling and gas, backtester, calibration tools, break-even calculator, CLI.
- **Not built yet:** a live venue adapter (Kuru on Monad, a Solana DEX, Hyperliquid...) and a recorder for
  real market data. Pick a venue and these are the next step. Nothing here can place a real order today.
- **No profit claim.** The only data so far is a synthetic market. Results on it prove the machinery
  works, not that any strategy makes money. On it, quoting without judgment *loses* money and the
  rule-based judgment layer roughly breaks even; that is about what to expect from a real market too.
  Edge has to come from your venue's data, measured with the tools here.

## Quick start

```sh
cd trading-bot
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # add ,jev for the Jev SDK: pip install -e ".[dev,jev]"
pytest -q

hftbot compare --config config.example.toml                  # engines side by side on a synthetic market
hftbot backtest --engine rules --journal journal.jsonl        # one engine, every block logged
hftbot calibrate journal.jsonl                                # are its probabilities honest?
hftbot breakeven --price 0.0226 --order-size 200 --fill-rate 0.05 --gas-per-tx 0.0008
```

With a TypeSafe key (`export TYPESAFE_API_KEY=...`, waitlist at typesafe.ai):

```sh
hftbot jev-check                                  # lists models, times one live battery, prints the model to pin
hftbot compare --with-jev --data blocks.jsonl     # calls Jev on every block: costs money, takes real time
```

## What the synthetic run shows (20,000 blocks, `config.example.toml`)

|                     | always-quote (no judgment) | rules |
|---------------------|---------------------------:|------:|
| PnL after gas       | -26.40                     | +4.65 |
| Fills               | 5,241                      | 1,471 |
| Spread capture, bps | 0.83                       | 1.29  |
| Adverse selection   | -0.70                      | -0.73 |
| Gas                 | 16.02                      | 11.56 |

Three lessons that carry over to real venues:

1. **The money is in not quoting.** Quoting every block into informed flow loses; skipping toxic moments
   is what turns the book. That is the judgment Jev is supposed to make better than rules.
2. **Gas is a fixed cost per transaction; spread is a percentage.** jev-trader posts a 200 MON order
   every block. At ~$0.0008 gas per transaction and an assumed 5% of the order filling per block,
   `hftbot breakeven` needs a 36 bps half spread against a ~7 bps book. 100x the size and a requote
   every 5 blocks brings it to ~1 bps. Size up, requote less.
3. **Requoting on every change also costs queue position.** Keeping resting quotes when the target moved
   less than a tick (`requote_ticks = 1`) cut gas from 29.95 to 16.02, raised fills, and turned the rules
   engine from -13.97 to +4.65 in the same backtest.

## Corrections to the article

- `Score(..., legend={...})` does not exist. The SDK takes `criteria=[...]`, an ordered list, one entry
  per level from 0. Noul answers carry no confidence. `hftbot/jev.py` uses the real API (checked against
  `typesafe-sdk` 0.7.4 source and tested against its wire format).
- The A-S formula it labels "half spread" is the *total* spread. Using it as written doubles every
  quote's distance. `hftbot/pricing.py` has the correct half spread.
- Cost: the snapshot here is ~130 tokens and the six questions ~385, so one battery is ~$0.00002. Every
  300 ms block for a month is ~$190, not $10 to $25. Still far below gas.
- You do not need AgenKit (the article's sponsor) or any agent framework: this repo is that system.

## Going live, in order

1. Pick a venue. Write its adapter against the `Venue` protocol in `hftbot/execution.py` and a recorder
   that writes real blocks in the JSONL format below.
2. Record at least a week of real blocks. Run `hftbot breakeven` with measured fill rate, gas and
   adverse selection; set `min_half_spread_bps` at or above the answer.
3. `hftbot compare` on the real data: always-quote vs rules vs Jev, with Jev's confidence gating on and off.
4. `hftbot calibrate` the Jev journal. If the reliability curve bends, apply Platt scaling
   (`hftbot.calibration.PlattScaler`) before using any threshold or `direction_kelly`.
5. Pin the exact Jev model (`jev_pinned_model`): thresholds are calibrated to one model, and an
   unpinned upgrade breaks them silently. A mismatch makes the bot fall back to rules.
6. Paper trade live for days. Then go live small, with limits sized to money you can lose.

## Safety rules

- Trade from a fresh wallet funded only with what you are willing to lose. The damage ceiling is set in advance.
- The bot never holds a seed phrase. Private keys never go in config files, journals, chat, or a prompt;
  load them from the environment or a hardware or remote signer in the venue adapter.
- Only a human raises a risk limit or resets the kill switch (`RiskEngine.reset()`). The bot never writes its own config.
- The kill switch is latched: on daily loss, drawdown or an API error burst it cancels, flattens, alerts and stays down.

## Layout

| Module | Role |
|---|---|
| `market.py` | Book, trade print, block types |
| `features.py` | State engine: deterministic features and the compact snapshot (< 400 tokens) |
| `judgments.py` | The six-question battery, typed answers, engine protocol |
| `jev.py` | Jev via the official SDK: no retries, deadline-bound, model pinning |
| `rules.py` | Rule-based engine (baseline and fallback), always-quote baseline |
| `policy.py` | Thresholds per action, confidence gating, inventory skew, fractional Kelly |
| `pricing.py` | Avellaneda-Stoikov quotes, tick rounding, post-only clamp, break-even floor |
| `risk.py` | Hard limits, latched kill switch |
| `portfolio.py` | Independent accounting from fills |
| `execution.py` | Venue protocol and paper venue (queue position, activation delay, gas on every tx) |
| `loop.py` | Per-block pipeline and fallback ladder |
| `backtest.py` | Metrics: PnL, Sharpe, Sortino, drawdown, hit rate, spread capture, adverse selection, coverage |
| `calibration.py` | Brier, log loss, ECE, reliability curves, Platt scaling, outcome labeling |
| `costs.py` | Gas honesty check |
| `sim.py` | Regime-switching synthetic market with informed flow and price impact |

## Block data format

One JSON object per line:

```json
{"number": 1, "ts": 1760000000.3, "bids": [[99.99, 40.0], [99.98, 52.0]], "asks": [[100.01, 38.0]],
 "trades": [[1760000000.21, 100.01, 3.5, "buy"]], "book_ts": 1760000000.3, "ref": 100.0}
```

`trades` are prints since the previous block, taker side last. `book_ts` (when the book was read) and
`ref` (a reference price from another venue) are optional.
