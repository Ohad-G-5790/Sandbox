"""Mid-frequency (hours to days) trading of a basket of liquid Solana tokens against USDC through Jupiter.

Fees per trade are fixed and small, so they stop mattering once each trade aims at a multi-percent move.
The hard part moves to the signal: a strategy is only traded if it beats simply holding the same coins,
after costs, on data it was never tuned on.
"""
