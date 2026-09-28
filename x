#!/usr/bin/env bash
# Short commands for running the XRP agent from a phone. Owner: Alan Fuller (alanfuller15).
#   ./x test     run the offline tests
#   ./x keygen   make your key pair; prints the public key for Robinhood
#   ./x setkey   store the API key Robinhood gives you (typed hidden, saved owner-only)
#   ./x check    read-only: buying power, XRP price, your real spread
#   ./x start    start live trading in the background (keeps running after you disconnect)
#   ./x status   is it running, profit so far, open position, last trades
#   ./x watch    watch the bot live (leave with ctrl-b then d)
#   ./x stop     stop the bot (an open position stays in your Robinhood account)
#   ./x update   get the latest version of the bot from GitHub
set -euo pipefail
cd "$(dirname "$0")"
PY="$HOME/xrp-venv/bin/python"
KEYFILE="$HOME/.rh_api_key"

need_key() {
  [ -s "$KEYFILE" ] || { echo "No API key saved yet: run ./x setkey"; exit 1; }
  export RH_API_KEY="$(cat "$KEYFILE")"
}

case "${1:-help}" in
  test)   "$PY" test_xrp_agent.py | tail -1 ;;
  keygen) "$PY" xrp_agent.py keygen ;;
  setkey)
    read -r -s -p "Paste the Robinhood API key, then press return (it won't show): " k; echo
    [ -n "$k" ] || { echo "nothing entered"; exit 1; }
    ( umask 077; printf '%s\n' "$k" > "$KEYFILE" )
    echo "Saved to $KEYFILE (only you can read it)." ;;
  check)  need_key; "$PY" xrp_agent.py check ;;
  start)
    need_key
    if tmux has-session -t xrp 2>/dev/null; then echo "Already running. ./x status or ./x watch"; exit 0; fi
    rm -f STOP
    extra=""; [ -f study.json ] && extra="--study study.json"
    tmux new-session -d -s xrp "RH_API_KEY=\$(cat '$KEYFILE') '$PY' xrp_agent.py run --confirm-live $extra 2>&1 | tee -a bot.log"
    sleep 3; tail -3 bot.log; echo "Started. ./x status to check, ./x stop to stop." ;;
  status)
    if tmux has-session -t xrp 2>/dev/null; then echo "Bot: RUNNING"; else echo "Bot: not running"; fi
    [ -f agent_state.json ] && "$PY" - <<'EOF'
import json
s = json.load(open("agent_state.json"))
p = s.get("position")
print(f"Realized profit: ${s['realized_usd']:+.4f}")
print("Open position: " + (f"{p['quantity']} XRP, cost ${p['cost_usd']:.4f}" if p else "none"))
EOF
    [ -f trades.csv ] && { echo "Last trades:"; tail -5 trades.csv | cut -d, -f5,6,10,11,12; }
    [ -f bot.log ] && { echo "Last log lines:"; tail -3 bot.log; } ;;
  watch)  tmux attach -t xrp ;;
  stop)
    touch STOP
    echo "Stop requested; the bot stops within a few seconds. An open position stays in your account." ;;
  update) git pull --ff-only ;;
  *)      sed -n '2,12p' "$0" ;;
esac
