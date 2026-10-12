from helpers import FixedEngine, block, decision, sell

from hftbot.execution import PaperVenue, TxReceipt
from hftbot.live import FeedError, run_live
from hftbot.loop import Ladder, TradingLoop
from hftbot.market import Side
from hftbot.portfolio import Portfolio
from hftbot.pricing import PricingConfig
from hftbot.risk import RiskEngine, RiskLimits


class ListFeed:
    def __init__(self, items):
        self.items = list(items)

    def next_block(self):
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_loop(venue=None, limits=None):
    return TradingLoop(
        engine=FixedEngine(decision()),
        venue=venue or PaperVenue(),
        portfolio=Portfolio(10_000.0),
        risk=RiskEngine(limits or RiskLimits()),
        pricing=PricingConfig(min_half_spread_bps=1.0, order_size=5.0),
        on_alert=lambda message: None,
    )


def test_runs_every_block_and_cancels_on_exit():
    loop = make_loop()
    records = []
    n = run_live(ListFeed([block(0), block(1), block(2)]), loop, on_step=records.append, max_blocks=3)
    assert n == 3 and len(records) == 3
    assert records[-1].resting and loop.venue.open_orders() == []


def test_feed_errors_count_as_api_errors_and_are_skipped():
    loop = make_loop()
    n = run_live(ListFeed([block(0), FeedError("rpc down"), block(1)]), loop, max_blocks=2)
    assert n == 2 and len(loop.risk._errors) == 1


class ThinVenue(PaperVenue):
    """A venue whose first flatten attempt fails, like a Jupiter swap that does not land."""

    def __init__(self):
        super().__init__()
        self.attempts = 0

    def market_order(self, side, size, block):
        self.attempts += 1
        if self.attempts == 1:
            return TxReceipt(gas=0.0, error="swap failed")
        return super().market_order(side, size, block)


def test_failed_flatten_is_retried_until_flat_then_runner_stops():
    venue = ThinVenue()
    loop = make_loop(venue, RiskLimits(max_drawdown=0.2, max_daily_loss=1e9))
    loop.step(block(0))
    bid = next(o for o in venue.open_orders() if o.side is Side.BUY)
    feed = ListFeed([block(1, trades=(sell(round(bid.price - 0.01, 2), 5.0),)), block(2, 99.79, 99.81), block(3, 99.79, 99.81), block(4)])
    records = []
    run_live(feed, loop, on_step=records.append)
    assert [r.ladder for r in records[1:]] == [Ladder.KILLED, Ladder.KILLED]
    assert venue.attempts == 2 and loop.portfolio.position == 0.0
    assert feed.items == [block(4)]  # stopped once flat
