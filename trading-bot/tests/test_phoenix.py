import base64
import json
import struct

import pytest
from phoenix_fixture import SOL, USDC, fee_event, fill_event, log_data, market_account, order, place_event, trader

from hftbot.live import FeedError
from hftbot.market import Side
from hftbot.solana.phoenix import (
    PHOENIX_LOG_AUTHORITY,
    PHOENIX_PROGRAM_ID,
    fills_from_transaction,
    parse_header,
    parse_log_events,
    parse_market,
    place_post_only_orders_data,
)
from hftbot.solana.rpc import b58decode, b58encode

MARKET = "4DoNfFBfF7UokCC2FQzriy7yHK6DY6NVdYpuekQ5pRgg"
ME = bytes([7]) * 32
OTHER = bytes([9]) * 32


def sample_market(**kwargs):
    bids = [
        order(150_100, 3, 2, 2_000, bid=True),  # 150.100 x 2.0 (newer)
        order(150_100, 1, 1, 500, bid=True),  # same price, older
        order(150_050, 2, 1, 1_000, bid=True),
        order(150_200, 4, 1, 9_000, bid=True, last_valid_slot=50),  # expired at slot 60
    ]
    asks = [order(150_300, 5, 2, 1_500), order(150_400, 6, 1, 4_000)]
    traders = [trader(OTHER, quote_free=10), trader(ME, quote_locked=300_000_000, base_free=2_000)]
    stale = [order(149_000, 8, 1, 7_777, bid=True)]  # a freed node with stale bytes
    return market_account(bids, asks, traders, free_bids=stale, **kwargs)


def test_header_and_conversions():
    h = parse_header(sample_market())
    assert (h.base_decimals, h.quote_decimals, h.base_mint, h.quote_mint) == (9, 6, SOL, USDC)
    assert h.tick_price == pytest.approx(0.001) and h.lot_units == pytest.approx(0.001)
    assert h.ticks_to_price(150_123) == pytest.approx(150.123)
    assert h.price_to_ticks(150.1239, Side.BUY) == 150_123 and h.price_to_ticks(150.1231, Side.SELL) == 150_124
    assert h.price_to_ticks(150.123, Side.SELL) == 150_123  # exact prices are not bumped by float noise
    assert h.units_to_lots(1.5) == 1_500 and h.taker_fee_bps == 2


def test_rejects_non_market_accounts():
    with pytest.raises(ValueError, match="discriminant"):
        parse_header(bytes(1000))


def test_book_sorts_aggregates_and_drops_expired_and_freed_orders():
    market = parse_market(sample_market())
    book = market.book(slot=60, now=1_000.0)
    assert [(lv.price, lv.size) for lv in book.bids] == [(pytest.approx(150.1), pytest.approx(2.5)), (pytest.approx(150.05), pytest.approx(1.0))]
    assert [lv.price for lv in book.asks] == [pytest.approx(150.3), pytest.approx(150.4)]
    assert all(o.base_lots != 7_777 for o in market.bids)  # the freed node is not an order
    # Time priority among equal prices: the older order (seq 1) comes first.
    same_price = [o for o in market.bids if o.price_in_ticks == 150_100]
    assert [o.base_lots for o in same_price] == [500, 2_000]
    # Before its last valid slot the 150.2 bid is live and is the best bid.
    assert market.book(slot=40, now=1_000.0).best_bid == pytest.approx(150.2)


def test_traders_and_our_orders():
    market = parse_market(sample_market())
    me = market.traders[ME]
    assert me.index == 2 and me.quote_lots_locked == 300_000_000 and me.base_lots_free == 2_000
    assert {(o.side, o.price_in_ticks) for o in market.orders_of(2)} == {(Side.BUY, 150_100), (Side.SELL, 150_300)}


def test_book_slice_parses_without_traders():
    data = sample_market()
    h = parse_header(data)
    market = parse_market(data[: h.books_end], h)
    assert market.traders is None and len(market.asks) == 2


def test_one_sided_book_is_an_error():
    data = market_account([order(150_000, 1, 1, 10, bid=True)], [], [])
    with pytest.raises(ValueError):
        parse_market(data).book(0, 0.0)


def test_place_orders_wire_format():
    data = place_post_only_orders_data([(150_100, 2_000)], [(150_300, 1_500), (150_400, 10)])
    expected = (
        b"\x10"
        + struct.pack("<I", 1) + struct.pack("<QQBB", 150_100, 2_000, 0, 0)
        + struct.pack("<I", 2) + struct.pack("<QQBB", 150_300, 1_500, 0, 0) + struct.pack("<QQBB", 150_400, 10, 0, 0)
        + b"\x00"  # no client order id
        + b"\x02"  # skip on insufficient funds, amend on cross
    )
    assert data == expected


def test_fill_events_are_parsed_for_our_market_only():
    market = b58decode(MARKET)
    data = log_data(market, [place_event(11, 150_300, 100), fill_event(ME, 5, 150_300, 400, 1_100), fill_event(OTHER, 3, 150_100, 200, bid=True), fee_event(3)])
    fills = parse_log_events(data, market)
    assert [(f.maker, f.maker_side, f.price_in_ticks, f.base_lots_filled) for f in fills] == [(ME, Side.SELL, 150_300, 400), (OTHER, Side.BUY, 150_100, 200)]
    assert parse_log_events(data, bytes(32)) == []
    with pytest.raises(ValueError, match="unknown"):
        parse_log_events(log_data(market, [b"\x63" + bytes(10)]), market)


def _tx(inner_data: bytes, err=None):
    return {
        "meta": {
            "err": err,
            "loadedAddresses": {"writable": [], "readonly": [PHOENIX_PROGRAM_ID]},
            "innerInstructions": [{"index": 0, "instructions": [{"programIdIndex": 2, "accounts": [1], "data": b58encode(inner_data)}]}],
        },
        "transaction": {"message": {"accountKeys": ["Payer1111111111111111111111111111111111111", PHOENIX_LOG_AUTHORITY]}, "signatures": ["s"]},
    }


def test_fills_from_transaction_resolves_loaded_program_and_skips_failed_transactions():
    market = b58decode(MARKET)
    data = log_data(market, [fill_event(ME, 5, 150_300, 400)])
    assert len(fills_from_transaction(_tx(data), market)) == 1
    assert fills_from_transaction(_tx(data, err={"InstructionError": [0, "Custom"]}), market) == []


solders = pytest.importorskip("solders")


def test_pdas_match_known_addresses():
    from hftbot.solana.phoenix import pda

    assert str(pda([b"log"], PHOENIX_PROGRAM_ID)) == PHOENIX_LOG_AUTHORITY


def test_claim_seat_instruction():
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey

    from hftbot.solana.phoenix import SEAT_MANAGER_PROGRAM_ID, claim_seat_ix, seat_address

    me, market = Keypair().pubkey(), Pubkey.from_string(MARKET)
    ix = claim_seat_ix(market, me)
    assert str(ix.program_id) == SEAT_MANAGER_PROGRAM_ID and bytes(ix.data) == b"\x01"
    metas = ix.accounts
    assert len(metas) == 9 and metas[7].pubkey == seat_address(market, me)
    assert metas[6].pubkey == me and metas[6].is_signer and metas[6].is_writable


class FakeRpc:
    def __init__(self, account: bytes, signatures=(), transactions=None, slot=100):
        self.account, self.slot = account, slot
        self.signatures = list(signatures)
        self.transactions = transactions or {}
        self.sent = []
        self.slices = []
        self.statuses = {}

    def get_account_data(self, pubkey, commitment="confirmed", data_slice=None):
        self.slices.append(data_slice)
        data = self.account if data_slice is None else self.account[data_slice[0] : data_slice[0] + data_slice[1]]
        return self.slot, data

    def get_signatures_for_address(self, address, until=None, limit=1000):
        if until is None:
            return self.signatures[:limit]
        out = []
        for s in self.signatures:
            if s["signature"] == until:
                break
            out.append(s)
        return out[:limit]

    def batch(self, calls):
        return [self.transactions.get(params[0]) for _, params in calls]

    def get_latest_blockhash(self, commitment="confirmed"):
        return "11111111111111111111111111111111", 1

    def send_transaction(self, signed, skip_preflight=True):
        self.sent.append(signed)
        return f"sig{len(self.sent)}"

    def get_signature_statuses(self, signatures):
        return [self.statuses.get(s) for s in signatures]


def make_feed(rpc, trader=ME, clock=None):
    from hftbot.solana.feed import PhoenixFeed

    t = iter(range(1_000, 10_000)) if clock is None else clock
    return PhoenixFeed(rpc, MARKET, trader=b58encode(trader) if trader else None, clock=lambda: float(next(t)), sleep=lambda s: None)


def test_feed_turns_market_transactions_into_trades_and_our_fills():
    market = b58decode(MARKET)
    rpc = FakeRpc(sample_market(), signatures=[{"signature": "old", "err": None}])
    feed = make_feed(rpc)
    assert feed.trader_state.index == 2
    rpc.signatures = [{"signature": "new2", "err": None}, {"signature": "bad", "err": {"x": 1}}, {"signature": "new1", "err": None}, {"signature": "old", "err": None}]
    rpc.transactions = {
        "new1": _tx(log_data(market, [fill_event(OTHER, 3, 150_100, 200, bid=True)], ts=1_760_000_001)),
        "new2": _tx(log_data(market, [fill_event(ME, 5, 150_300, 400)], ts=1_760_000_002)),
    }
    block = feed.next_block()
    assert [(t.aggressor, t.price, t.size) for t in block.trades] == [(Side.SELL, pytest.approx(150.1), pytest.approx(0.2)), (Side.BUY, pytest.approx(150.3), pytest.approx(0.4))]
    (mine,) = feed.drain_our_fills()
    assert mine.side is Side.SELL and mine.size == pytest.approx(0.4) and mine.maker and mine.fee == 0.0
    assert feed.drain_our_fills() == []
    assert rpc.slices[-1] == (0, feed.header.books_end)  # routine polls read only the order trees
    assert block.book.best_bid == pytest.approx(150.1)  # at slot 100 the 150.2 bid (valid to slot 50) has expired
    assert feed.next_block().trades == ()  # nothing new since "new2"


def test_feed_dates_the_book_when_the_slot_stops_advancing():
    rpc = FakeRpc(sample_market())
    feed = make_feed(rpc, trader=None)
    first = feed.next_block()
    second = feed.next_block()
    assert first.book.ts == first.ts and second.book.ts == first.ts < second.ts


def test_feed_rejects_closed_markets_and_reports_bad_reads():
    with pytest.raises(FeedError, match="status"):
        make_feed(FakeRpc(sample_market(status=4)))
    rpc = FakeRpc(sample_market())
    feed = make_feed(rpc)
    rpc.account = market_account([order(150_000, 1, 1, 10, bid=True)], [], [])
    with pytest.raises(FeedError):
        feed.next_block()


def make_venue(rpc):
    from solders.keypair import Keypair

    from hftbot.solana.venue import PhoenixVenue

    kp = Keypair()
    feed = make_feed(rpc, trader=bytes(kp.pubkey()))
    return PhoenixVenue(rpc, feed, kp, exit=None, clock=lambda: 0.0), kp


def test_requote_is_one_signed_transaction_cancel_then_place():
    from solders.transaction import Transaction

    from hftbot.pricing import Quote

    rpc = FakeRpc(sample_market())
    venue, kp = make_venue(rpc)
    block = venue.feed.next_block()
    receipt = venue.replace_quotes([Quote(Side.BUY, 150.0, 1.0), Quote(Side.SELL, 150.5, 1.0)], block)
    assert receipt.error is None and receipt.gas > 0
    tx = Transaction.from_bytes(rpc.sent[0])
    assert tx.verify_with_results() == [True] and tx.message.account_keys[0] == kp.pubkey()
    programs = [str(tx.message.account_keys[ix.program_id_index]) for ix in tx.message.instructions]
    assert programs == ["ComputeBudget111111111111111111111111111111", "ComputeBudget111111111111111111111111111111", PHOENIX_PROGRAM_ID, PHOENIX_PROGRAM_ID]
    cancel, place = tx.message.instructions[2], tx.message.instructions[3]
    assert bytes(cancel.data) == b"\x06" and len(cancel.accounts) == 9
    assert bytes(place.data) == place_post_only_orders_data([(150_000, 1_000)], [(150_500, 1_000)]) and len(place.accounts) == 10
    # Until the book shows them, the quotes just sent count as resting: no duplicate sends.
    assert {(o.side, o.price) for o in venue.open_orders()} == {(Side.BUY, 150.0), (Side.SELL, 150.5)}


def test_failed_transactions_are_reported_as_api_errors():
    from hftbot.pricing import Quote

    rpc = FakeRpc(sample_market())
    venue, _ = make_venue(rpc)
    errors = []
    venue.on_error = errors.append
    block = venue.feed.next_block()
    venue.replace_quotes([Quote(Side.BUY, 150.0, 1.0)], block)
    rpc.statuses = {"sig1": {"err": {"InstructionError": [3, "Custom"]}}}
    rpc.slot += 5
    for _ in range(5):
        venue.match(venue.feed.next_block())
    assert len(errors) == 1 and venue.stats.orders_rejected == 1


def test_nothing_to_cancel_sends_nothing():
    rpc = FakeRpc(sample_market())
    venue, _ = make_venue(rpc)
    assert venue.cancel_all(venue.feed.next_block()).gas == 0.0 and rpc.sent == []


def test_wire_account_data_is_base64_in_rpc():
    from hftbot.solana.rpc import SolanaRpc
    import json

    data = sample_market()

    def post(url, body, timeout_s):
        return json.dumps({"result": {"context": {"slot": 5}, "value": {"data": [base64.b64encode(data).decode(), "base64"]}}}).encode()

    slot, raw = SolanaRpc(post=post).get_account_data(MARKET)
    assert slot == 5 and parse_market(raw).header.base_mint == SOL


def test_paper_run_end_to_end(monkeypatch, tmp_path, capsys):
    """`hftbot run` (paper) against a simulated Phoenix market: real parsing, simulated fills."""
    import hftbot.solana.cli as solana_cli
    from hftbot.cli import main

    market = b58decode(MARKET)
    rpc = FakeRpc(sample_market(), signatures=[{"signature": "s0", "err": None}])
    polls = {"n": 0}

    def next_signatures(address, until=None, limit=1000):
        polls["n"] += 1
        if until is None:
            return [{"signature": "s0", "err": None}]
        sig = f"s{polls['n']}"
        # Someone sells through the whole bid side every poll, so our paper bid gets hit.
        rpc.transactions[sig] = _tx(log_data(market, [fill_event(OTHER, 1, 140_000, 5_000, bid=True)]))
        return [{"signature": sig, "err": None}]

    rpc.get_signatures_for_address = next_signatures
    monkeypatch.setattr(solana_cli, "SolanaRpc", lambda *a, **k: rpc)
    real_run_live = solana_cli.run_live
    monkeypatch.setattr(solana_cli, "run_live", lambda feed, loop, **kw: real_run_live(feed, loop, max_blocks=6, **kw))
    config = tmp_path / "fast.toml"
    config.write_text(open(__file__.replace("tests/test_phoenix.py", "config.solana.toml")).read().replace("block_interval_s = 0.4", "block_interval_s = 0.001"))
    journal = tmp_path / "j.jsonl"

    assert main(["run", "--config", str(config), "--journal", str(journal)]) == 0
    out = capsys.readouterr().out
    assert "PAPER TRADING" in out and "done: 6 blocks" in out
    records = [json.loads(line) for line in journal.read_text().splitlines()]
    assert len(records) == 6 and any(r["fills"] for r in records)
    assert rpc.sent == []  # paper mode never sends a transaction


def test_live_requires_confirming_the_wallet(monkeypatch, tmp_path, capsys):
    import os
    import stat

    from solders.keypair import Keypair

    from hftbot.cli import main

    kp = Keypair()
    key = tmp_path / "k.json"
    key.write_text(json.dumps(list(bytes(kp))))
    os.chmod(key, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("HFTBOT_KEYPAIR", str(key))
    assert main(["run", "--live"]) == 2
    assert str(kp.pubkey()) in capsys.readouterr().err


def test_seat_approval_check():
    from hftbot.solana.cli import _seat_approved

    seat = bytearray(128)
    struct.pack_into("<Q", seat, 72, 1)

    class SeatRpc:
        def __init__(self, data):
            self.data = data

        def get_account_data(self, pubkey):
            if self.data is None:
                from hftbot.solana.rpc import RpcError

                raise RpcError("not found")
            return 1, bytes(self.data)

    assert _seat_approved(SeatRpc(seat), "x")
    assert not _seat_approved(SeatRpc(bytearray(128)), "x")
    assert not _seat_approved(SeatRpc(None), "x")


def test_flatten_withdraws_market_funds_first_then_swaps():
    from solders.transaction import Transaction

    from hftbot.execution import TxReceipt

    rpc = FakeRpc(sample_market())
    calls = []

    class Exit:
        def market_order(self, side, size, block):
            calls.append(("swap", side, size, len(rpc.sent)))
            return TxReceipt(gas=0.01)

    venue, _ = make_venue(rpc)
    venue.exit = Exit()
    venue._sleep = lambda s: None
    rpc.statuses = {"sig1": {"err": None, "confirmationStatus": "confirmed"}}
    receipt = venue.market_order(Side.SELL, 1.5, venue.feed.next_block())
    tx = Transaction.from_bytes(rpc.sent[0])
    datas = [bytes(ix.data) for ix in tx.message.instructions[2:]]
    assert datas == [b"\x06", b"\x0c\x00\x00"]  # cancel all, then withdraw every free balance
    assert calls == [("swap", Side.SELL, 1.5, 1)]  # the swap comes after the withdraw was sent and confirmed
    assert receipt.error is None and receipt.gas > 0.01


def test_sigterm_stops_a_paper_run_cleanly(monkeypatch, tmp_path, capsys):
    import os
    import signal as signal_module

    import hftbot.solana.cli as solana_cli
    from hftbot.cli import main

    rpc = FakeRpc(sample_market())
    monkeypatch.setattr(solana_cli, "SolanaRpc", lambda *a, **k: rpc)
    real_run_live = solana_cli.run_live

    def run_then_get_terminated(feed, loop, on_step, **kw):
        def step(record):
            on_step(record)
            if record.block == 3:
                os.kill(os.getpid(), signal_module.SIGTERM)  # what a cloud host sends on redeploy

        return real_run_live(feed, loop, on_step=step, **kw)

    monkeypatch.setattr(solana_cli, "run_live", run_then_get_terminated)
    config = tmp_path / "fast.toml"
    config.write_text(open(__file__.replace("tests/test_phoenix.py", "config.solana.toml")).read().replace("block_interval_s = 0.4", "block_interval_s = 0.001"))
    before = signal_module.getsignal(signal_module.SIGTERM)
    assert main(["run", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "stopping: resting orders were cancelled" in out and "done: 3 blocks" in out
    assert signal_module.getsignal(signal_module.SIGTERM) is before


def test_live_start_cancels_orders_left_by_a_previous_run(monkeypatch, tmp_path, capsys):
    import os
    import stat

    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    import hftbot.solana.cli as solana_cli
    from hftbot.cli import main
    from hftbot.solana.phoenix import TraderAccounts, parse_header

    kp = Keypair()
    me = bytes(kp.pubkey())
    data = market_account([order(150_000, 1, 1, 500, bid=True)], [order(150_500, 2, 1, 500)], [trader(me)])
    accounts = TraderAccounts.derive(Pubkey.from_string(MARKET), kp.pubkey(), parse_header(data))
    seat = bytearray(128)
    struct.pack_into("<Q", seat, 72, 1)

    class LiveRpc(FakeRpc):
        def get_account_data(self, pubkey, commitment="confirmed", data_slice=None):
            if pubkey == str(accounts.seat):
                return self.slot, bytes(seat)
            return super().get_account_data(pubkey, commitment, data_slice)

        def get_token_balance(self, account, commitment="confirmed"):
            return 10**9

        def get_balance(self, pubkey, commitment="confirmed"):
            return 10**9

    rpc = LiveRpc(data)
    monkeypatch.setattr(solana_cli, "SolanaRpc", lambda *a, **k: rpc)
    monkeypatch.setattr(solana_cli, "run_live", lambda feed, loop, **kw: 0)
    monkeypatch.setenv("HFTBOT_KEYPAIR_JSON", json.dumps(list(bytes(kp))))
    monkeypatch.setenv("HFTBOT_CONFIRM_WALLET", str(kp.pubkey()))

    assert main(["run", "--live"]) == 0
    assert "cancelling 2 orders left by a previous run" in capsys.readouterr().out
    (sent,) = rpc.sent
    assert [bytes(ix.data) for ix in Transaction.from_bytes(sent).message.instructions[2:]] == [b"\x06"]


def test_unreachable_rpc_is_a_clean_error(monkeypatch, capsys):
    import hftbot.solana.cli as solana_cli
    from hftbot.cli import main
    from hftbot.solana.rpc import RpcError

    class DownRpc:
        def get_account_data(self, *a, **k):
            raise RpcError("getAccountInfo: connection refused")

    monkeypatch.setattr(solana_cli, "SolanaRpc", lambda *a, **k: DownRpc())
    assert main(["run"]) == 1
    assert "cannot read market" in capsys.readouterr().err
