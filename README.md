# Bitcoin Ratio Spread Trading Bot (Delta Exchange India)

This repository contains a professional Python-based trading bot designed to trade Bitcoin ratio spreads on **Delta Exchange India** with minimum slippage and automated delta hedging.

## Features

- **Slippage Minimization**: Automatically places limit orders at $1 better than the current best bid (for buys) or $1 better than the best ask (for sells).
- **Dynamic Price Chasing**: Monitors the order book every second. If your order is no longer the best price, it cancels and replaces it with a new "best" price.
- **Automated Ratio Spreads**: Executes 1:3 ratio spreads for both calls and puts automatically.
- **Real-time Delta Hedging**: While waiting for all legs to fill, the bot calculates the net delta of executed positions and offsets it using At-The-Money (ATM) daily expiry options via market orders.
- **Auto-Cleanup**: Once all 4 main legs are executed, the bot automatically closes the hedge positions.
- **Rate-Limit Friendly**: Uses caching for product metadata to avoid exceeding Delta Exchange API limits.

## Configuration

Input variables are managed via `config.yaml`:

```yaml
api_key: "YOUR_DELTA_INDIA_API_KEY"
api_secret: "YOUR_DELTA_INDIA_API_SECRET"
quantity: 10              # Number of contracts for the buy leg
expiry: "270625"          # Expiry in DDMMYY format
call_buy_strike: 100000   # Strike for the Call Buy leg
call_sell_strike: 105000  # Strike for the Call Sell leg (3x quantity)
put_buy_strike: 90000     # Strike for the Put Buy leg
put_sell_strike: 85000    # Strike for the Put Sell leg (3x quantity)
```

## Setup & Usage

1. **Install Dependencies**:
   ```bash
   pip install PyYAML requests
   ```

2. **Configure**: Update `config.yaml` with your credentials and trading parameters.

3. **Run**:
   ```bash
   python trading_bot.py
   ```

## Files
- `trading_bot.py`: The main execution script.
- `config.yaml`: User input and configuration.
- `test_bot.py`: Unit tests for critical logic.

## Safety Disclaimer
*Trading derivatives involves significant risk. This bot is provided as-is. Ensure you test it thoroughly on a Testnet account before using live funds. The author is not responsible for any financial losses.*
