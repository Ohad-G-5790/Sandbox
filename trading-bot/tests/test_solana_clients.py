"""Solana RPC and Jupiter clients against canned responses (no network)."""

import base64
import json
import os
import stat

import pytest

from hftbot.solana.jupiter import SOL_MINT, USDC_MINT, JupiterClient, JupiterError, sign_versioned_transaction
from hftbot.solana.rpc import RpcError, SolanaRpc

QUOTE = {
    "inputMint": SOL_MINT,
    "inAmount": "2000000000",
    "outputMint": USDC_MINT,
    "outAmount": "301500000",
    "otherAmountThreshold": "300000000",
    "swapMode": "ExactIn",
    "slippageBps": 50,
    "priceImpactPct": "0.0004",
    "routePlan": [{"swapInfo": {"ammKey": "x", "label": "Phoenix", "inputMint": SOL_MINT, "outputMint": USDC_MINT, "inAmount": "1", "outAmount": "1"}, "percent": 100}],
}


def rpc_returning(result=None, error=None):
    calls = []

    def post(url, body, timeout_s):
        calls.append(json.loads(body))
        return json.dumps({"jsonrpc": "2.0", "id": 1, **({"error": error} if error else {"result": result})}).encode()

    return SolanaRpc("https://rpc.example", post=post), calls


def test_get_account_data_decodes_base64_and_reports_slot():
    rpc, calls = rpc_returning({"context": {"slot": 321}, "value": {"data": [base64.b64encode(b"\x01\x02").decode(), "base64"]}})
    assert rpc.get_account_data("Market111") == (321, b"\x01\x02")
    assert calls[0]["method"] == "getAccountInfo" and calls[0]["params"][1]["encoding"] == "base64"


def test_rpc_errors_and_missing_accounts_raise():
    rpc, _ = rpc_returning(error={"code": -32005, "message": "rate limited"})
    with pytest.raises(RpcError, match="rate limited"):
        rpc.get_slot()
    rpc, _ = rpc_returning({"context": {"slot": 1}, "value": None})
    with pytest.raises(RpcError, match="not found"):
        rpc.get_account_data("Nope111")


def test_send_transaction_skips_preflight_and_never_retries():
    rpc, calls = rpc_returning("5ig")
    assert rpc.send_transaction(b"tx") == "5ig"
    options = calls[0]["params"][1]
    assert options["skipPreflight"] is True and options["maxRetries"] == 0


def jupiter(responses, api_key=None):
    seen = []

    def http(url, body, headers, timeout_s):
        seen.append((url, json.loads(body) if body else None, headers))
        return json.dumps(responses.pop(0)).encode()

    return JupiterClient(api_key=api_key, http=http), seen


def test_quote_request_and_parsing():
    client, seen = jupiter([QUOTE])
    q = client.quote(SOL_MINT, USDC_MINT, 2_000_000_000, slippage_bps=50)
    url = seen[0][0]
    assert url.startswith("https://lite-api.jup.ag/swap/v1/quote?") and "amount=2000000000" in url and "slippageBps=50" in url
    assert (q.in_amount, q.out_amount, q.min_out_amount) == (2_000_000_000, 301_500_000, 300_000_000)
    assert q.route == "Phoenix"


def test_api_key_switches_host_and_header():
    client, seen = jupiter([QUOTE], api_key="k")
    client.quote(SOL_MINT, USDC_MINT, 1, 10)
    assert seen[0][0].startswith("https://api.jup.ag/swap/v1/") and seen[0][2]["x-api-key"] == "k"


def test_swap_request_sends_quote_back_with_capped_priority_fee():
    client, seen = jupiter([QUOTE, {"swapTransaction": base64.b64encode(b"unsigned").decode(), "lastValidBlockHeight": 9}])
    tx = client.swap_transaction(client.quote(SOL_MINT, USDC_MINT, 1, 10), "Wallet111", max_priority_lamports=5000)
    body = seen[1][1]
    assert tx == b"unsigned"
    assert body["quoteResponse"] == QUOTE and body["userPublicKey"] == "Wallet111"
    assert body["prioritizationFeeLamports"]["priorityLevelWithMaxLamports"]["maxLamports"] == 5000
    assert body["wrapAndUnwrapSol"] is False  # never swap the native SOL kept for fees


def test_error_payload_raises():
    client, _ = jupiter([{"error": "Could not find any route"}])
    with pytest.raises(JupiterError, match="route"):
        client.quote(SOL_MINT, USDC_MINT, 1, 10)


solders = pytest.importorskip("solders")


def _unsigned_tx(payer):
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction

    ix = transfer(TransferParams(from_pubkey=payer, to_pubkey=payer, lamports=1))
    message = MessageV0.try_compile(payer, [ix], [], Hash.default())
    return bytes(VersionedTransaction.populate(message, [solders.signature.Signature.default()]))


def test_signing_checks_the_fee_payer():
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction

    me, stranger = Keypair(), Keypair()
    signed = VersionedTransaction.from_bytes(sign_versioned_transaction(_unsigned_tx(me.pubkey()), me))
    assert signed.verify_with_results() == [True]
    with pytest.raises(JupiterError, match="fee payer"):
        sign_versioned_transaction(_unsigned_tx(stranger.pubkey()), me)


def test_keypair_file_must_be_private(tmp_path, monkeypatch):
    from solders.keypair import Keypair

    from hftbot.solana.keys import load_keypair

    kp = Keypair()
    path = tmp_path / "bot.json"
    path.write_text(json.dumps(list(bytes(kp))))
    os.chmod(path, 0o644)
    with pytest.raises(RuntimeError, match="chmod 600"):
        load_keypair(path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("HFTBOT_KEYPAIR", str(path))
    assert load_keypair().pubkey() == kp.pubkey()


class FakeJupiter:
    def __init__(self, usdc_for_2_sol=300_200_000):
        self.usdc_for_2_sol = usdc_for_2_sol
        self.quotes = []

    def quote(self, input_mint, output_mint, amount, slippage_bps):
        from hftbot.solana.jupiter import SwapQuote

        self.quotes.append((input_mint, output_mint, amount))
        if input_mint == SOL_MINT:  # selling 2 SOL
            return SwapQuote({}, amount, self.usdc_for_2_sol, self.usdc_for_2_sol, 0.0, "Phoenix")
        lamports = round(amount / 1e6 / 150.25 * 1e9)  # buying SOL at 150.25
        return SwapQuote({}, amount, lamports, lamports, 0.0, "Phoenix")

    def swap_transaction(self, quote, user_pubkey, max_priority_lamports):
        from solders.pubkey import Pubkey

        return _unsigned_tx(Pubkey.from_string(user_pubkey))


class FakeRpc:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.sent = []

    def send_transaction(self, signed):
        self.sent.append(signed)
        return "sig1"

    def get_signature_statuses(self, signatures):
        return [self.statuses.pop(0) if self.statuses else None]


def _exit(jupiter, rpc, timeout=5.0):
    from solders.keypair import Keypair

    from hftbot.solana.exit import JupiterExit, TokenPair

    pair = TokenPair(SOL_MINT, USDC_MINT, 9, 6)
    return JupiterExit(jupiter, rpc, Keypair(), pair, confirm_timeout_s=timeout, sleep=lambda s: None)


def _block():
    from hftbot.market import Block, Book, Level

    return Block(7, 100.0, Book(100.0, (Level(150.0, 10.0),), (Level(150.2, 10.0),)))


def test_exit_sells_base_and_reports_the_fill_only_after_confirmation():
    from hftbot.market import Side

    jupiter, rpc = FakeJupiter(), FakeRpc([None, {"err": None, "confirmationStatus": "confirmed"}])
    receipt = _exit(jupiter, rpc).market_order(Side.SELL, 2.0, _block())
    assert receipt.error is None and len(rpc.sent) == 1
    assert jupiter.quotes[0] == (SOL_MINT, USDC_MINT, 2_000_000_000)
    (fill,) = receipt.fills
    assert fill.side is Side.SELL and fill.size == 2.0 and fill.price == pytest.approx(150.10) and not fill.maker
    assert receipt.gas > 0


def test_exit_buy_spends_quote_with_slippage_allowance():
    from hftbot.market import Side

    jupiter, rpc = FakeJupiter(), FakeRpc([{"err": None, "confirmationStatus": "finalized"}])
    receipt = _exit(jupiter, rpc).market_order(Side.BUY, 2.0, _block())
    assert jupiter.quotes[0][:2] == (USDC_MINT, SOL_MINT)
    assert jupiter.quotes[0][2] == round(2.0 * 150.2 * 1.01 * 1e6)
    assert receipt.fills[0].size == pytest.approx(2.0 * 150.2 * 1.01 / 150.25)
    assert receipt.fills[0].price == pytest.approx(150.25)


def test_exit_refuses_high_impact_and_reports_failed_or_unconfirmed_swaps():
    from hftbot.market import Side

    receipt = _exit(FakeJupiter(usdc_for_2_sol=290_000_000), FakeRpc([])).market_order(Side.SELL, 2.0, _block())  # 145 vs 150.1 mid
    assert "worse than mid" in receipt.error and receipt.fills == ()
    failed = _exit(FakeJupiter(), FakeRpc([{"err": {"InstructionError": [0, "x"]}, "confirmationStatus": "confirmed"}])).market_order(Side.SELL, 2.0, _block())
    assert "failed" in failed.error and failed.fills == ()
    lost = _exit(FakeJupiter(), FakeRpc([]), timeout=0.01).market_order(Side.SELL, 2.0, _block())
    assert "not confirmed" in lost.error and lost.fills == ()
