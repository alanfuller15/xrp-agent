"""Offline tests for xrp_agent.py (owner: Alan Fuller, alanfuller15). No network, no account: Robinhood is replaced by fakes.
Run: python3 -m pytest -q test_xrp_agent.py   (or: python3 test_xrp_agent.py)"""
import base64
import json
import math
import os
import random
import stat
import tempfile
from pathlib import Path

import xrp_agent as X


def synthetic_history(n=20000, step_s=60, seed=1, start=0.60):
    """Random-walk XRP-like minute prices with bursts of volatility."""
    rng = random.Random(seed)
    p, t, out, vol = start, 1_700_000_000, [], 0.002
    for _ in range(n):
        if rng.random() < 0.01:
            vol = rng.choice([0.001, 0.002, 0.004, 0.008])
        p *= math.exp(rng.gauss(0, vol))
        out.append((t, p))
        t += step_s
    return out


# ------------------------------------------------------------------ keys and signing

def test_keygen_and_signature_roundtrip():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "k.txt"
        pub = X.keygen(path)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        try:
            X.keygen(path)
            assert False, "keygen must refuse to overwrite"
        except SystemExit:
            pass
        key = X.load_private_key(path.read_text())
        sig = X.sign_request(key, "rh-api-KEY", 1700000000, "/api/v1/crypto/trading/accounts/", "GET", "")
        Ed25519PublicKey.from_public_bytes(base64.b64decode(pub)).verify(
            base64.b64decode(sig), b"rh-api-KEY1700000000/api/v1/crypto/trading/accounts/GET")


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code, self._p = status, payload
        self.text = json.dumps(payload)

    def json(self):
        return self._p


class RecordingSession:
    def __init__(self, payload):
        self.calls, self.payload = [], payload

    def request(self, method, url, headers, data, timeout):
        self.calls.append((method, url, dict(headers), data))
        return FakeResponse(self.payload)


def test_client_signs_exact_body_sent():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    sess = RecordingSession({"id": "o1", "state": "open"})
    cl = X.RobinhoodClient("KEY", key, base="https://example.invalid", session=sess)
    cl.market_order("buy", "12.5")
    method, url, h, data = sess.calls[0]
    assert method == "POST" and url == "https://example.invalid/api/v1/crypto/trading/orders/"
    body = json.loads(data)
    assert body["side"] == "buy" and body["symbol"] == "XRP-USD" and body["market_order_config"] == {"asset_quantity": "12.5"}
    msg = f"KEY{h['x-timestamp']}/api/v1/crypto/trading/orders/POST{data}".encode()
    key.public_key().verify(base64.b64decode(h["x-signature"]), msg)


def test_quote_uses_spread_inclusive_prices():
    sess = RecordingSession({"results": [{"symbol": "XRP-USD", "price": "0.6000", "bid_inclusive_of_sell_spread": "0.5970",
                                          "sell_spread": "0.005", "ask_inclusive_of_buy_spread": "0.6030", "buy_spread": "0.005"}]})
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    q = X.RobinhoodClient("KEY", Ed25519PrivateKey.generate(), session=sess).quote()
    assert (q.mid, q.bid, q.ask) == (0.6, 0.597, 0.603)
    assert sess.calls[0][1].endswith("/api/v1/crypto/marketdata/best_bid_ask/?symbol=XRP-USD")


# ------------------------------------------------------------------ strategy

def test_every_take_profit_close_is_profitable_after_spread():
    hist = synthetic_history()
    for spread in (0.0025, 0.005, 0.01):
        for fee in (0.0, 0.001):
            cfg = X.Config(extra_fee=fee)
            ag = X.simulate(hist, cfg, spread, seed=3)["agent"]
            assert ag.closed, (spread, fee)
            for c in ag.closed:
                assert c["reason"] == "take_profit"
                assert c["pnl_usd"] > 0, c
                assert c["return_pct"] / 100 >= cfg.arms[c["arm"]]["take_profit"] - 1e-9, c


def test_stop_loss_is_off_by_default_and_works_when_set():
    hist = [(i * 60, 1.0) for i in range(300)] + [(18000 + i * 60, 1.0 - 0.002 * i) for i in range(200)]
    no_stop = X.simulate(hist, X.Config(), 0.0025, seed=0)
    assert no_stop["trades"] == 0 and no_stop["open_at_end"]
    with_stop = X.simulate(hist, X.Config(stop_loss=0.05), 0.0025, seed=0)["agent"]
    assert any(c["reason"] == "stop_loss" and c["pnl_usd"] < 0 for c in with_stop.closed)


def test_chosen_arm_is_kept_until_the_buy():
    ag = X.Agent(X.Config(), random.Random(0))
    q = X.Quote(t=0, mid=1.0, bid=0.999, ask=1.001)
    ag.observe(q)
    ag.decide(q)
    first = ag.pending_arm
    for i in range(50):
        q = X.Quote(t=60 * (i + 1), mid=1.0, bid=0.999, ask=1.001)
        ag.observe(q)
        assert ag.decide(q) is None
        assert ag.pending_arm == first


def test_wide_spread_blocks_buying():
    ag = X.Agent(X.Config(max_round_trip_spread=0.01), random.Random(0))
    ag.observe(X.Quote(t=0, mid=1.0, bid=0.999, ask=1.001))
    assert ag.decide(X.Quote(t=60, mid=0.5, bid=0.49, ask=0.51)) is None   # 4% round trip


def test_profits_are_reinvested_and_budget_starts_at_100():
    ag = X.Agent(X.Config(), random.Random(0))
    assert ag.capital() == 100.0
    ag.opened(0, 100.0, 100.0, 0)
    ag.closed_position(110.0, 3600, "take_profit")
    assert abs(ag.capital() - 110.0) < 1e-9
    ag2 = X.Agent(X.Config(reinvest_profits=False), random.Random(0))
    ag2.opened(0, 100.0, 100.0, 0)
    ag2.closed_position(110.0, 3600, "take_profit")
    assert ag2.capital() == 100.0


def test_state_roundtrip():
    ag = X.simulate(synthetic_history(3000), X.Config(), 0.0025, seed=1)["agent"]
    d = json.loads(json.dumps(ag.to_dict()))
    ag2 = X.Agent(X.Config())
    ag2.load_dict(d)
    assert ag2.to_dict() == ag.to_dict()


# ------------------------------------------------------------------ study

def test_read_history_formats():
    with tempfile.TemporaryDirectory() as d:
        a = Path(d) / "cdd.csv"
        a.write_text("unix,date,symbol,open,high,low,close,Volume XRP\n"
                     "1700000120000,2023-11-14 22:02:00,XRP/USD,0.6,0.6,0.6,0.61,10\n"
                     "1700000060000,2023-11-14 22:01:00,XRP/USD,0.6,0.6,0.6,0.60,10\n")
        assert X.read_history(a) == [(1700000060.0, 0.60), (1700000120.0, 0.61)]
        b = Path(d) / "yahoo.csv"
        b.write_text("Date,Open,High,Low,Close,Adj Close,Volume\n2024-01-01,0.6,0.7,0.5,0.62,0.62,1\n")
        assert X.read_history(b)[0][1] == 0.62


def test_study_runs_and_reports_held_out_results():
    hist = synthetic_history(6000)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "h.csv"
        p.write_text("timestamp,close\n" + "".join(f"{t},{px}\n" for t, px in hist))
        out = Path(d) / "study.json"
        X.main(["study", str(p), "--spread", "0.005", "--out", str(out)])
        res = json.loads(out.read_text())[0]
        assert len(res["arms"]) == len(X.DEFAULT_ARMS)
        assert res["test_points"] == 1800 and "held_out_learner" in res


# ------------------------------------------------------------------ live loop against a fake Robinhood

class FakeRobinhood:
    """Serves a price path; fills market orders at the spread-inclusive price."""

    def __init__(self, prices, spread=0.005, cash=500.0):
        self.prices, self.i, self.spread = prices, 0, spread
        self.cash, self.xrp, self.orders = cash, 0.0, {}

    def trading_pair(self):
        return {"asset_increment": "0.000001", "min_order_size": "0.1"}

    def account(self):
        return {"buying_power": str(self.cash), "status": "active"}

    def quote(self):
        p = self.prices[min(self.i, len(self.prices) - 1)]
        self.i += 1
        return X.Quote(t=self.i * 60.0, mid=p, bid=p * (1 - self.spread), ask=p * (1 + self.spread))

    def holdings(self):
        return self.xrp

    def market_order(self, side, qty):
        p = self.prices[min(self.i - 1, len(self.prices) - 1)]
        q = float(qty)
        px = p * (1 + self.spread) if side == "buy" else p * (1 - self.spread)
        if side == "buy":
            assert q * px <= self.cash + 1e-9, "bot tried to spend more than buying power"
            self.cash -= q * px
            self.xrp += q
        else:
            assert q <= self.xrp + 1e-12
            self.cash += q * px
            self.xrp -= q
        oid = f"o{len(self.orders)}"
        self.orders[oid] = {"id": oid, "state": "filled", "filled_asset_quantity": qty, "average_price": str(px)}
        return self.orders[oid]

    def order(self, oid):
        return self.orders[oid]


def test_live_loop_buys_sells_logs_and_stops():
    prices = [1.0] * 30 + [0.97] * 5 + [1.0 + 0.002 * i for i in range(40)]
    fake = FakeRobinhood(prices)
    cfg = X.Config(arms=[{"dip": 0.02, "take_profit": 0.01}], average_minutes=60)
    ag = X.Agent(cfg, random.Random(0))
    with tempfile.TemporaryDirectory() as d:
        st, tr, stop = Path(d) / "s.json", Path(d) / "t.csv", Path(d) / "STOP"
        X.run_live(fake, ag, st, tr, 0, stop, max_cycles=len(prices), sleep=lambda s: None)
        assert len(ag.closed) >= 1 and all(c["pnl_usd"] > 0 for c in ag.closed)
        assert fake.cash > 500.0                       # the fake account really made money after spread
        assert "pnl_usd" in tr.read_text() and json.loads(st.read_text())["realized_usd"] > 0
        spent_max = max(float(o["filled_asset_quantity"]) * float(o["average_price"]) for o in fake.orders.values() if o["id"] in ("o0",))
        assert spent_max <= 100.0                      # first buy stays inside the $100 budget
        stop.write_text("")
        assert X.run_live(fake, ag, st, tr, 0, stop, max_cycles=5, sleep=lambda s: None) == "stopped"


def test_run_requires_confirm_live():
    try:
        X.main(["run"])
        assert False
    except SystemExit as e:
        assert "confirm-live" in str(e)


def test_quantize_rounds_down():
    assert X.quantize(12.3456789, "0.000001") == "12.345678"
    assert X.quantize(12.9, "1") == "12"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns:
        f()
        print("ok", f.__name__)
    print(len(fns), "tests passed")
