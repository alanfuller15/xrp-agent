"""XRP trading agent for the Robinhood Crypto Trading API.

Owner: Alan Fuller (alanfuller15). Built with Claude Code.

Every price the agent acts on already includes Robinhood's spread:
  * it BUYS at the ask price including the buy spread (what you actually pay);
  * it SELLS only when the bid price including the sell spread (what you actually
    receive) beats the full cost of the position by the target margin.
So each closed trade is profitable after the spread (plus any extra fee you set).
The trade-off is honest: if XRP falls after a buy, the agent holds and waits for
the target instead of selling at a loss, unless you set a stop-loss.

Learning: the agent keeps several settings ("arms") for how deep a dip to buy
and how much profit to take. It scores each arm by realized profit per hour of
holding, mostly uses the best one, and keeps trying the others a little so it
adapts as XRP's behaviour changes. `study` scores the arms on a price-history
CSV first, so the live agent starts from what the history showed.

Commands:
  keygen    make the key pair you register with Robinhood (private key stays here)
  check     read-only: account, buying power, XRP quote and spread, holdings
  study     score every arm on a price-history CSV (no account needed)
  run       trade live with real money (needs --confirm-live)

Stop: create a file named STOP next to the state file, or press Ctrl-C.
"""
import argparse
import base64
import csv
import json
import math
import os
import random
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

API_BASE = "https://trading.robinhood.com"
SYMBOL = "XRP-USD"
ASSET = "XRP"

# dip = how far the ask must be below the recent average price to buy.
# take_profit = how far above the full entry cost the sell price must be.
DEFAULT_ARMS = [
    {"dip": d, "take_profit": tp}
    for d in (0.003, 0.006, 0.010, 0.015, 0.025)
    for tp in (0.004, 0.008, 0.015, 0.030)
]


# --------------------------------------------------------------------------- keys and signing

def load_private_key(b64_seed):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    seed = base64.b64decode(b64_seed.strip())
    if len(seed) != 32:
        raise ValueError("private key must be a base64-encoded 32-byte Ed25519 seed")
    return Ed25519PrivateKey.from_private_bytes(seed)


def keygen(out_path):
    """Write a new Ed25519 private key to out_path (owner-only permissions) and return the public key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    out = Path(out_path)
    if out.exists():
        raise SystemExit(f"{out} already exists; refusing to overwrite a private key")
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(base64.b64encode(seed).decode() + "\n")
    return base64.b64encode(pub).decode()


def sign_request(private_key, api_key, timestamp, path, method, body):
    """Robinhood signs api_key + timestamp + path (with query) + method + body."""
    message = f"{api_key}{timestamp}{path}{method}{body}"
    return base64.b64encode(private_key.sign(message.encode("utf-8"))).decode()


# --------------------------------------------------------------------------- Robinhood client

class RobinhoodClient:
    def __init__(self, api_key, private_key, base=API_BASE, session=None):
        import requests
        self.api_key = api_key
        self.key = private_key
        self.base = base
        self.http = session or requests.Session()

    def _request(self, method, path, body=None):
        body_str = json.dumps(body) if body is not None else ""
        ts = int(time.time())
        headers = {
            "x-api-key": self.api_key,
            "x-timestamp": str(ts),
            "x-signature": sign_request(self.key, self.api_key, ts, path, method, body_str),
            "Content-Type": "application/json; charset=utf-8",
        }
        for attempt in range(4):
            r = self.http.request(method, self.base + path, headers=headers, data=body_str or None, timeout=15)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                ts = int(time.time())
                headers["x-timestamp"] = str(ts)
                headers["x-signature"] = sign_request(self.key, self.api_key, ts, path, method, body_str)
                continue
            break
        if r.status_code >= 400:
            raise RuntimeError(f"Robinhood API {method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.text else {}

    def account(self):
        return self._request("GET", "/api/v1/crypto/trading/accounts/")

    def quote(self, symbol=SYMBOL):
        """Best bid/ask with Robinhood's spread included."""
        res = self._request("GET", f"/api/v1/crypto/marketdata/best_bid_ask/?symbol={symbol}")["results"][0]
        return Quote(
            t=time.time(),
            mid=float(res["price"]),
            bid=float(res["bid_inclusive_of_sell_spread"]),
            ask=float(res["ask_inclusive_of_buy_spread"]),
        )

    def trading_pair(self, symbol=SYMBOL):
        return self._request("GET", f"/api/v1/crypto/trading/trading_pairs/?symbol={symbol}")["results"][0]

    def holdings(self, asset=ASSET):
        res = self._request("GET", f"/api/v1/crypto/trading/holdings/?asset_code={asset}")["results"]
        return float(res[0]["quantity_available_for_trading"]) if res else 0.0

    def market_order(self, side, quantity, symbol=SYMBOL):
        body = {
            "client_order_id": str(uuid.uuid4()),
            "side": side,
            "type": "market",
            "symbol": symbol,
            "market_order_config": {"asset_quantity": quantity},
        }
        return self._request("POST", "/api/v1/crypto/trading/orders/", body)

    def order(self, order_id):
        return self._request("GET", f"/api/v1/crypto/trading/orders/{order_id}/")


# --------------------------------------------------------------------------- strategy (shared by study and live)

@dataclass
class Quote:
    t: float      # unix seconds
    mid: float    # Robinhood's reference price
    bid: float    # what you receive when selling (sell spread included)
    ask: float    # what you pay when buying (buy spread included)


@dataclass
class Position:
    arm: int
    quantity: float
    cost_usd: float        # everything paid, spread included
    opened_t: float

    @property
    def cost_per_unit(self):
        return self.cost_usd / self.quantity


@dataclass
class ArmStats:
    trades: int = 0
    reward_sum: float = 0.0   # sum of (return / hours held)

    @property
    def mean(self):
        return self.reward_sum / self.trades if self.trades else 0.0


@dataclass
class Config:
    budget_usd: float = 100.0
    reinvest_profits: bool = True
    average_minutes: float = 240.0     # time window of the recent average price
    max_round_trip_spread: float = 0.03  # skip buying when ask/bid - 1 is wider than this
    extra_fee: float = 0.0             # any fee on top of the spread, per side (0.001 = 0.1%)
    stop_loss: float = 0.0             # 0 = never sell at a loss; 0.10 = sell if the bid is 10% under cost
    explore_min: float = 0.05          # smallest share of buys that try a non-best arm
    arms: list = field(default_factory=lambda: [dict(a) for a in DEFAULT_ARMS])


class Agent:
    """Decides BUY / SELL / HOLD from spread-inclusive quotes. Holds no money itself."""

    def __init__(self, config, rng=None):
        self.cfg = config
        self.rng = rng or random.Random()
        self.avg = None
        self.avg_t = None
        self.position = None
        self.realized_usd = 0.0
        self.stats = [ArmStats() for _ in config.arms]
        self.closed = []
        self.pending_arm = None   # the arm chosen for the next buy, kept until that buy happens

    # --- the recent average: time-weighted exponential average of the mid price
    def observe(self, q):
        if self.avg is None:
            self.avg, self.avg_t = q.mid, q.t
            return
        dt = max(q.t - self.avg_t, 0.0)
        w = 1.0 - math.exp(-dt / (self.cfg.average_minutes * 60.0))
        self.avg += w * (q.mid - self.avg)
        self.avg_t = q.t

    def capital(self):
        c = self.cfg.budget_usd + (self.realized_usd if self.cfg.reinvest_profits else min(self.realized_usd, 0.0))
        return max(c, 0.0)

    def choose_arm(self):
        tried = [i for i, s in enumerate(self.stats) if s.trades]
        untried = [i for i, s in enumerate(self.stats) if not s.trades]
        n = sum(s.trades for s in self.stats)
        explore = max(self.cfg.explore_min, 1.0 / math.sqrt(n + 1))
        if untried and (not tried or self.rng.random() < explore):
            return self.rng.choice(untried)
        if self.rng.random() < explore:
            return self.rng.randrange(len(self.stats))
        return max(tried, key=lambda i: self.stats[i].mean)

    def target_bid(self, pos):
        tp = self.cfg.arms[pos.arm]["take_profit"]
        return pos.cost_per_unit * (1.0 + tp) / (1.0 - self.cfg.extra_fee)

    def decide(self, q):
        """Return ('buy', arm, usd) / ('sell', reason) / None. Call observe(q) first."""
        if q.bid <= 0 or q.ask <= 0 or self.avg is None:
            return None
        if self.position is None:
            if q.ask / q.bid - 1.0 > self.cfg.max_round_trip_spread:
                return None
            if self.pending_arm is None:
                self.pending_arm = self.choose_arm()
            arm = self.pending_arm
            if q.ask <= self.avg * (1.0 - self.cfg.arms[arm]["dip"]):
                usd = self.capital()
                return ("buy", arm, usd) if usd > 0 else None
            return None
        pos = self.position
        if q.bid >= self.target_bid(pos):
            return ("sell", "take_profit")
        if self.cfg.stop_loss > 0 and q.bid * (1.0 - self.cfg.extra_fee) <= pos.cost_per_unit * (1.0 - self.cfg.stop_loss):
            return ("sell", "stop_loss")
        return None

    def opened(self, arm, quantity, cost_usd, t):
        self.position = Position(arm=arm, quantity=quantity, cost_usd=cost_usd * (1.0 + self.cfg.extra_fee), opened_t=t)
        self.pending_arm = None

    def closed_position(self, proceeds_usd, t, reason):
        pos = self.position
        net = proceeds_usd * (1.0 - self.cfg.extra_fee)
        pnl = net - pos.cost_usd
        hours = max((t - pos.opened_t) / 3600.0, 0.25)
        ret = pnl / pos.cost_usd
        s = self.stats[pos.arm]
        s.trades += 1
        s.reward_sum += ret / hours
        self.realized_usd += pnl
        rec = dict(arm=pos.arm, **self.cfg.arms[pos.arm], opened_t=pos.opened_t, closed_t=t, hours=round(hours, 3),
                   quantity=pos.quantity, cost_usd=round(pos.cost_usd, 6), proceeds_usd=round(net, 6),
                   pnl_usd=round(pnl, 6), return_pct=round(100 * ret, 4), reason=reason)
        self.closed.append(rec)
        self.position = None
        return rec

    # --- persistence
    def to_dict(self):
        return dict(avg=self.avg, avg_t=self.avg_t, realized_usd=self.realized_usd,
                    position=asdict(self.position) if self.position else None,
                    stats=[asdict(s) for s in self.stats], arms=self.cfg.arms)

    def load_dict(self, d):
        if d.get("arms") != self.cfg.arms:
            raise SystemExit("saved state was made with different arms; move the state file aside to start fresh")
        self.avg, self.avg_t, self.realized_usd = d["avg"], d["avg_t"], d["realized_usd"]
        self.position = Position(**d["position"]) if d.get("position") else None
        self.stats = [ArmStats(**s) for s in d["stats"]]

    def seed_stats(self, studied):
        """Start the arms from a study's scores (counted as a few trades each, so live results soon take over)."""
        for i, row in enumerate(studied):
            if row["trades"]:
                self.stats[i] = ArmStats(trades=min(row["trades"], 5), reward_sum=min(row["trades"], 5) * row["reward_per_trade"])


# --------------------------------------------------------------------------- study (history backtest)

def read_history(path):
    """CSV with a time column (unix seconds or ISO) and a close/price column; header names are matched loosely."""
    from datetime import datetime
    rows = []
    with open(path, newline="") as f:
        rdr = csv.DictReader(f)
        cols = {c.lower().strip(): c for c in rdr.fieldnames}
        tcol = next((cols[c] for c in ("unix", "timestamp", "open_time", "time", "date", "datetime", "snapped_at") if c in cols), None)
        pcol = next((cols[c] for c in ("close", "price", "last", "close_price") if c in cols), None)
        if not tcol or not pcol:
            raise SystemExit(f"need a time column and a close/price column; found {rdr.fieldnames}")
        for r in rdr:
            tv, pv = r[tcol].strip(), r[pcol].strip()
            if not tv or not pv:
                continue
            try:
                t = float(tv)
                if t > 1e12:
                    t /= 1000.0
            except ValueError:
                t = datetime.fromisoformat(tv.replace("Z", "+00:00")).timestamp()
            rows.append((t, float(pv)))
    rows.sort()
    return rows


def simulate(history, config, spread_per_side, fixed_arm=None, seed=0, agent=None):
    """Replay a price history with Robinhood-style quotes: bid = price*(1-spread), ask = price*(1+spread)."""
    cfg = config
    if fixed_arm is not None:
        cfg = Config(**{**asdict(config), "arms": [config.arms[fixed_arm]], "explore_min": 0.0})
    ag = agent or Agent(cfg, random.Random(seed))
    peak = equity = cfg.budget_usd
    max_dd = 0.0
    held_s = 0.0
    last_t = None
    for t, p in history:
        q = Quote(t=t, mid=p, bid=p * (1 - spread_per_side), ask=p * (1 + spread_per_side))
        if ag.position is not None and last_t is not None:
            held_s += t - last_t
        last_t = t
        ag.observe(q)
        d = ag.decide(q)
        if d and d[0] == "buy":
            usd = d[2]
            ag.opened(d[1], usd / q.ask, usd, t)
        elif d and d[0] == "sell":
            ag.closed_position(ag.position.quantity * q.bid, t, d[1])
        equity = cfg.budget_usd + ag.realized_usd
        if ag.position is not None:
            equity += ag.position.quantity * q.bid * (1 - cfg.extra_fee) - ag.position.cost_usd
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak if peak > 0 else 0)
    span_h = (history[-1][0] - history[0][0]) / 3600.0 if len(history) > 1 else 0.0
    wins = sum(1 for c in ag.closed if c["pnl_usd"] > 0)
    return dict(
        trades=len(ag.closed), wins=wins, realized_usd=round(ag.realized_usd, 4),
        final_equity_usd=round(equity, 4), return_pct=round(100 * (equity / cfg.budget_usd - 1), 3),
        max_drawdown_pct=round(100 * max_dd, 3), hours=round(span_h, 1),
        time_in_position_pct=round(100 * held_s / (span_h * 3600), 1) if span_h else 0.0,
        open_at_end=ag.position is not None,
        reward_per_trade=(sum(c["return_pct"] / 100 / c["hours"] for c in ag.closed) / len(ag.closed)) if ag.closed else 0.0,
        agent=ag,
    )


def study(history, config, spread_per_side, train_frac=0.7):
    """Score every arm on the first part of the history, then test the best arm and the learner on the unseen rest."""
    cut = int(len(history) * train_frac)
    train, test = history[:cut], history[cut:]
    table = []
    for i, arm in enumerate(config.arms):
        r = simulate(train, config, spread_per_side, fixed_arm=i)
        r.pop("agent")
        table.append(dict(arm=i, **arm, **r))
    ranked = sorted(table, key=lambda r: r["return_pct"], reverse=True)
    best = ranked[0]["arm"]
    held_out_best = simulate(test, config, spread_per_side, fixed_arm=best)
    held_out_best.pop("agent")
    learner = Agent(config, random.Random(0))
    learner.seed_stats(table)
    held_out_learner = simulate(test, config, spread_per_side, agent=learner)
    held_out_learner.pop("agent")
    hold_return = 100 * ((test[-1][1] * (1 - spread_per_side)) / (test[0][1] * (1 + spread_per_side)) - 1) if len(test) > 1 else 0.0
    return dict(spread_per_side=spread_per_side, train_points=len(train), test_points=len(test), arms=table,
                best_arm=best, held_out_best_arm=held_out_best, held_out_learner=held_out_learner,
                held_out_buy_and_hold_return_pct=round(hold_return, 3))


# --------------------------------------------------------------------------- live trading

def quantize(qty, increment):
    inc = Decimal(str(increment))
    return str(Decimal(str(qty)).quantize(inc, rounding=ROUND_DOWN) if inc < 1 else (Decimal(str(qty)) // inc) * inc)


def wait_filled(client, order_id, timeout_s=120):
    end = time.time() + timeout_s
    while time.time() < end:
        o = client.order(order_id)
        if o.get("state") == "filled":
            return o
        if o.get("state") in ("canceled", "failed"):
            raise RuntimeError(f"order {order_id} ended {o.get('state')}: {o}")
        time.sleep(2)
    raise RuntimeError(f"order {order_id} not filled within {timeout_s}s; check the Robinhood app")


def fill_usd(order):
    qty = float(order.get("filled_asset_quantity") or 0)
    px = float(order.get("average_price") or 0)
    return qty, qty * px


def log_line(path, rec):
    new = not Path(path).exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rec))
        if new:
            w.writeheader()
        w.writerow(rec)


def run_live(client, agent, state_path, trades_path, poll_s, stop_path, max_cycles=None, sleep=time.sleep):
    pair = client.trading_pair()
    inc, min_size = float(pair["asset_increment"]), float(pair["min_order_size"])
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        if Path(stop_path).exists():
            print(f"STOP file found ({stop_path}); stopping. Any open position stays in your account.")
            return "stopped"
        try:
            q = client.quote()
        except Exception as e:  # network hiccup: wait and retry, never guess a price
            print(f"quote failed: {e}")
            sleep(poll_s)
            continue
        agent.observe(q)
        d = agent.decide(q)
        if d and d[0] == "buy":
            _, arm, usd = d
            usd = min(usd, float(client.account()["buying_power"]))
            qty = quantize(usd / q.ask * 0.995, inc)   # 0.5% headroom so a moving price can't exceed the budget
            if float(qty) >= min_size:
                o = wait_filled(client, client.market_order("buy", qty)["id"])
                got, paid = fill_usd(o)
                agent.opened(arm, got, paid, time.time())
                print(f"BUY  {got} XRP for ${paid:.4f} (arm {arm} {agent.cfg.arms[arm]})")
        elif d and d[0] == "sell":
            pos = agent.position
            qty = quantize(min(pos.quantity, client.holdings()), inc)
            if float(qty) > 0:
                o = wait_filled(client, client.market_order("sell", qty)["id"])
                got, proceeds = fill_usd(o)
                rec = agent.closed_position(proceeds, time.time(), d[1])
                log_line(trades_path, rec)
                print(f"SELL {got} XRP for ${proceeds:.4f}  P&L ${rec['pnl_usd']:+.4f} ({rec['return_pct']:+.3f}%) {d[1]}")
        Path(state_path).write_text(json.dumps(agent.to_dict(), indent=1))
        sleep(poll_s)
    return "cycles"


# --------------------------------------------------------------------------- command line

def make_client(args):
    api_key = os.environ.get("RH_API_KEY")
    if not api_key:
        raise SystemExit("set RH_API_KEY to the API key Robinhood gave you")
    key_file = Path(args.private_key)
    if not key_file.exists():
        raise SystemExit(f"private key file {key_file} not found (run: python3 xrp_agent.py keygen)")
    return RobinhoodClient(api_key, load_private_key(key_file.read_text()))


def load_config(path):
    if path and Path(path).exists():
        return Config(**json.loads(Path(path).read_text()))
    return Config()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen"); k.add_argument("--out", default="rh_private_key.txt")
    c = sub.add_parser("check"); c.add_argument("--private-key", default="rh_private_key.txt")
    s = sub.add_parser("study"); s.add_argument("csv"); s.add_argument("--spread", type=float, action="append",
        help="Robinhood spread per side as a fraction, e.g. 0.005 (repeatable); default 0.0025, 0.005, 0.01")
    s.add_argument("--config"); s.add_argument("--out", default="study.json")
    r = sub.add_parser("run"); r.add_argument("--private-key", default="rh_private_key.txt"); r.add_argument("--config")
    r.add_argument("--state", default="agent_state.json"); r.add_argument("--trades", default="trades.csv")
    r.add_argument("--study", help="study.json to start the arms from"); r.add_argument("--poll", type=float, default=5.0)
    r.add_argument("--confirm-live", action="store_true", help="required: this trades real money")
    a = ap.parse_args(argv)

    if a.cmd == "keygen":
        pub = keygen(a.out)
        print(f"Private key written to {a.out} (only you can read it; never share it).")
        print("Public key to paste into Robinhood when you create the API credential:")
        print(pub)
    elif a.cmd == "check":
        cl = make_client(a)
        acct = cl.account()
        q = cl.quote()
        print(json.dumps(dict(account_status=acct.get("status"), buying_power=acct.get("buying_power"),
                              xrp_mid=q.mid, you_pay_ask=q.ask, you_get_bid=q.bid,
                              round_trip_spread_pct=round(100 * (q.ask / q.bid - 1), 4),
                              xrp_held=cl.holdings(), trading_pair=cl.trading_pair()), indent=1))
    elif a.cmd == "study":
        cfg = load_config(a.config)
        hist = read_history(a.csv)
        if len(hist) < 100:
            raise SystemExit("need at least 100 price points")
        out = [study(hist, cfg, sp) for sp in (a.spread or [0.0025, 0.005, 0.01])]
        Path(a.out).write_text(json.dumps(out, indent=1))
        for res in out:
            top = sorted(res["arms"], key=lambda r: r["return_pct"], reverse=True)[:5]
            print(f"\nspread {100 * res['spread_per_side']:.2f}% per side ({100 * 2 * res['spread_per_side']:.2f}% round trip)")
            print("  best arms on the first 70% of history:")
            for t in top:
                print(f"    dip {100 * t['dip']:.1f}%  take {100 * t['take_profit']:.1f}%  trades {t['trades']:4d}  "
                      f"return {t['return_pct']:+8.2f}%  max drawdown {t['max_drawdown_pct']:6.2f}%  open at end {t['open_at_end']}")
            hb, hl = res["held_out_best_arm"], res["held_out_learner"]
            print(f"  on the unseen last 30%: best arm {hb['return_pct']:+.2f}% ({hb['trades']} trades), "
                  f"learner {hl['return_pct']:+.2f}% ({hl['trades']} trades), "
                  f"buy and hold {res['held_out_buy_and_hold_return_pct']:+.2f}%")
        print(f"\nfull results: {a.out}")
    elif a.cmd == "run":
        if not a.confirm_live:
            raise SystemExit("run trades real money; add --confirm-live to start")
        cfg = load_config(a.config)
        agent = Agent(cfg)
        if Path(a.state).exists():
            agent.load_dict(json.loads(Path(a.state).read_text()))
            print(f"resumed: realized ${agent.realized_usd:+.4f}, open position: {agent.position is not None}")
        elif a.study:
            res = json.loads(Path(a.study).read_text())
            agent.seed_stats(min(res, key=lambda r: abs(r["spread_per_side"] - 0.005))["arms"])
        stop = Path(a.state).with_name("STOP")
        print(f"trading {SYMBOL} with up to ${agent.capital():.2f}; create {stop} or press Ctrl-C to stop")
        try:
            run_live(make_client(a), agent, a.state, a.trades, a.poll, stop)
        except KeyboardInterrupt:
            Path(a.state).write_text(json.dumps(agent.to_dict(), indent=1))
            print("\nstopped by Ctrl-C; state saved. Any open position stays in your account.")
        except Exception as e:
            Path(a.state).write_text(json.dumps(agent.to_dict(), indent=1))
            raise SystemExit(f"stopped on an error, state saved: {e}\n"
                             "Check the Robinhood app for any order that filled before restarting.")


if __name__ == "__main__":
    main()
