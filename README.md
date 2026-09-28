# XRP trading agent for Robinhood

Owner: Alan Fuller (alanfuller15). Built with Claude Code.

This agent trades XRP-USD in your Robinhood account through Robinhood's official Crypto Trading API.
It starts with $100 and reinvests its profits. There is no limit on how many trades it makes or how much profit it takes.

## How it trades

- **It works from the prices you actually get.**
  - Every quote it reads has Robinhood's spread built in.
  - It buys at the ask price including the buy spread.
  - It sells only when the bid price including the sell spread beats the position's full cost by the take-profit margin.
  - So every trade it closes for profit has made money after the spread.
- **It buys dips.** It buys when the ask is a set percentage below XRP's recent average price. The average covers roughly the last 4 hours, and you can change that.
- **It learns.**
  - Each "arm" pairs a dip size with a profit target. There are 20 arms: 5 dip sizes times 4 profit targets.
  - It scores each arm by realized profit per hour held.
  - It mostly uses the best-scoring arm, but keeps testing the others, so it follows XRP as its behaviour changes.
- **It holds rather than selling at a loss.** If XRP falls after a buy, it waits for the target. The loss is not avoided; it stays open in your account until XRP recovers. Set `stop_loss` in the config (for example `0.10` for 10%) if you would rather cut losses.
- **It only touches its own XRP.** It sells only the XRP it bought itself. XRP you already hold in the account is never sold.

## Setup (on a computer that stays on)

The agent cannot run inside the Robinhood phone app. It runs on a computer and trades through your account's API key. Its trades show up in the app as usual.

1. **Install the Python packages.** You need Python 3.10 or newer.
   ```
   pip install cryptography requests
   ```
2. **Make your key pair.**
   ```
   python3 xrp_agent.py keygen
   ```
   This writes `rh_private_key.txt`, which only you can read. Never share it or paste it anywhere. The command also prints a **public key**.
3. **Create the API credential at Robinhood.**
   - Sign in on Robinhood's website on a computer. API access is set up on the web, not in the phone app.
   - Find the Crypto Trading API section and create a credential. If you can't find it, search Robinhood's help centre for "crypto trading API".
   - Paste in the public key from step 2 and give it trading permission.
   - Robinhood gives you an **API key**.
4. **Give the agent the API key.**
   ```
   export RH_API_KEY="the key Robinhood gave you"
   ```
5. **Check the connection.** This is read-only and places no orders.
   ```
   python3 xrp_agent.py check
   ```
   It shows your buying power, the live XRP price, the price you would pay, the price you would get, and the **real round-trip spread**. Compare that spread with the spreads used in the study.

## Study XRP's history (optional, no account needed)

1. Download XRP/USD hourly or 1-minute data from cryptodatadownload.com.
2. Delete the file's first line, the website text above the column names.
3. Run:
   ```
   python3 xrp_agent.py study Bitstamp_XRPUSD_1h.csv --spread 0.005
   ```

Use the spread from `check` if you have it. Put it in as a fraction per side: a 1% round trip is `--spread 0.005`.

The study scores all 20 arms on the first 70% of the history. It then tests the best arm and the learner on the last 30%, which none of the scoring saw, and compares both against simply buying and holding. The last-30% numbers are the ones to trust.

## Trade live

```
python3 xrp_agent.py run --confirm-live --study study.json
```

- `--study` starts the arms from what the history showed. Leave it out to start fresh.
- `--config config.json` changes settings. Copy `config.example.json` to start.
- The agent saves its state to `agent_state.json` and restarts where it left off.
- Every closed trade goes into `trades.csv`, with its profit or loss and the arm it used.
- To stop, create a file named `STOP` next to `agent_state.json`, or press Ctrl-C. An open position stays in your account; you can sell it in the app.
- If an order errors out, the agent saves its state and stops. Check the app before you restart it.

## Settings (`config.json`)

| Setting | Default | Meaning |
|---|---|---|
| `budget_usd` | 100 | Starting money the agent may use |
| `reinvest_profits` | true | Profits add to what it trades with |
| `average_minutes` | 240 | Time window of the "recent average" price that dips are measured from |
| `max_round_trip_spread` | 0.03 | Don't buy while the spread is wider than 3% round trip |
| `extra_fee` | 0 | Any fee on top of the spread, per side (0.001 = 0.1%) |
| `stop_loss` | 0 | 0 = never sell at a loss; 0.10 = sell if down 10% |
| `explore_min` | 0.05 | Smallest share of buys that try a non-best arm |

## Limits worth knowing

- **Not tested against Robinhood's live servers.** The agent was built from Robinhood's documented API, and all 14 offline tests pass against a simulated Robinhood. It has never been run against the real servers, so run `check` first and watch the first trade in the app.
- **No guaranteed profit.** A closed trade makes money, but an open one can sit at a loss for a long time.
- **Tested only on simulated prices.** On random simulated prices, the study usually ends with a losing position still open. That is what happens when there is no real pattern to trade.
- **Real history is the real test.** Run the study on actual XRP history before you give it money.
