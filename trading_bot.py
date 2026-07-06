import hmac
import hashlib
import time
import requests
import yaml
import json
import logging
from datetime import datetime

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class DeltaClient:
    def __init__(self, api_key, api_secret, base_url="https://api.india.delta.exchange"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url

    def _generate_signature(self, method, path, query_string, payload, timestamp):
        signature_data = method + timestamp + path + query_string + payload
        return hmac.new(
            self.api_secret.encode('utf-8'),
            signature_data.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

    def request(self, method, path, params=None, payload=None):
        timestamp = str(int(time.time()))
        query_string = ""
        if params:
            sorted_params = sorted(params.items())
            query_string = "?" + "&".join([f"{k}={v}" for k, v in sorted_params])

        body = ""
        if payload:
            body = json.dumps(payload)

        signature = self._generate_signature(method, path, query_string, body, timestamp)

        headers = {
            'api-key': self.api_key,
            'timestamp': timestamp,
            'signature': signature,
            'User-Agent': 'python-trading-bot',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }

        url = f"{self.base_url}{path}{query_string}"
        try:
            response = requests.request(method, url, data=body, headers=headers, timeout=10)
            return response.json()
        except Exception as e:
            logger.error(f"Request failed: {e}")
            return {"success": False, "error": str(e)}

    def get_ticker(self, symbol):
        res = self.request("GET", f"/v2/tickers/{symbol}")
        return res.get("result") if res.get("success") else None

    def get_l2_orderbook(self, symbol):
        res = self.request("GET", f"/v2/l2orderbook/{symbol}")
        return res.get("result") if res.get("success") else None

    def place_order(self, product_id, size, side, order_type="limit_order", limit_price=None):
        payload = {
            "product_id": int(product_id),
            "size": int(size),
            "side": side,
            "order_type": order_type
        }
        if limit_price is not None:
            payload["limit_price"] = str(round(float(limit_price)))
        return self.request("POST", "/v2/orders", payload=payload)

    def cancel_order(self, order_id, product_id):
        payload = {"id": int(order_id), "product_id": int(product_id)}
        return self.request("DELETE", "/v2/orders", payload=payload)

    def get_order(self, order_id):
        res = self.request("GET", f"/v2/orders/{order_id}")
        return res.get("result") if res.get("success") else None

    def get_products(self, query_params):
        res = self.request("GET", "/v2/products", params=query_params)
        return res.get("result") if res.get("success") else []

def get_atm_daily_options(client, spot_price):
    """Find today's expiry ATM call and put options."""
    today_str = datetime.now().strftime("%d%m%y")
    # Fetch live options for today
    products = client.get_products({"contract_types": "call_options,put_options", "states": "live"})

    btc_today = [p for p in products if "BTC" in p['symbol'] and p['symbol'].endswith(today_str)]

    if not btc_today:
        return None, None

    # Helper to parse strike from symbol C-BTC-STRIKE-DDMMYY
    def get_strike(p):
        try: return float(p['symbol'].split('-')[2])
        except: return 1e9

    # Group by strike
    strike_map = {}
    for p in btc_today:
        s = get_strike(p)
        if s not in strike_map: strike_map[s] = {'C': None, 'P': None}
        if p['symbol'].startswith('C-'): strike_map[s]['C'] = p
        elif p['symbol'].startswith('P-'): strike_map[s]['P'] = p

    # Find closest strike that has both C and P
    valid_strikes = [s for s, instruments in strike_map.items() if instruments['C'] and instruments['P']]
    if not valid_strikes: return None, None

    closest_strike = min(valid_strikes, key=lambda s: abs(s - spot_price))
    return strike_map[closest_strike]['C'], strike_map[closest_strike]['P']

def main():
    try:
        with open("config.yaml", "r") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Could not load config.yaml: {e}")
        return

    client = DeltaClient(config['api_key'], config['api_secret'])

    expiry = config['expiry']
    legs = [
        {"type": "C", "strike": config['call_buy_strike'], "side": "buy", "qty": config['quantity'], "name": "Call Buy"},
        {"type": "C", "strike": config['call_sell_strike'], "side": "sell", "qty": config['quantity'] * 3, "name": "Call Sell"},
        {"type": "P", "strike": config['put_buy_strike'], "side": "buy", "qty": config['quantity'], "name": "Put Buy"},
        {"type": "P", "strike": config['put_sell_strike'], "side": "sell", "qty": config['quantity'] * 3, "name": "Put Sell"},
    ]

    logger.info("Resolving products...")
    for leg in legs:
        symbol = f"{leg['type']}-BTC-{leg['strike']}-{expiry}"
        leg['symbol'] = symbol
        res = client.request("GET", f"/v2/products/{symbol}")
        if res.get("success"):
            leg['product_id'] = res['result']['id']
        else:
            logger.error(f"Could not find product {symbol}. Exiting.")
            return

    active_orders = {} # index -> order object
    executed_qty = [0] * len(legs)

    # Hedge Tracking: product_id -> quantity (positive for long)
    hedge_positions = {}

    logger.info("Starting execution loop...")

    try:
        daily_options_cache = []
        last_cache_update = 0
        cache_ttl = 300

        while any(executed_qty[i] < legs[i]['qty'] for i in range(len(legs))):
            # 1. Update Market Data
            btc_ticker = client.get_ticker("BTCUSD")
            if not btc_ticker:
                time.sleep(1)
                continue
            spot_price = float(btc_ticker['spot_price'])

            if time.time() - last_cache_update > cache_ttl:
                daily_options_cache = client.get_products({"contract_types": "call_options,put_options", "states": "live"})
                last_cache_update = time.time()

            # Find ATM instruments
            atm_call, atm_put = get_atm_daily_options(client, spot_price)

            total_delta = 0
            # 2. Manage Main Legs
            for i, leg in enumerate(legs):
                if i in active_orders:
                    order = client.get_order(active_orders[i]['id'])
                    if order:
                        executed_qty[i] = int(order['size']) - int(order['unfilled_size'])
                        if order['state'] in ['closed', 'cancelled']:
                            del active_orders[i]
                        else:
                            active_orders[i] = order
                    else:
                        del active_orders[i]

                if executed_qty[i] > 0:
                    ticker = client.get_ticker(leg['symbol'])
                    if ticker and ticker.get('greeks'):
                        delta = float(ticker['greeks']['delta'])
                        side_mult = 1 if leg['side'] == 'buy' else -1
                        total_delta += executed_qty[i] * delta * side_mult

                if executed_qty[i] < leg['qty']:
                    ob = client.get_l2_orderbook(leg['symbol'])
                    if not ob: continue
                    best_bid = float(ob['buy'][0]['price']) if ob['buy'] else 0
                    best_ask = float(ob['sell'][0]['price']) if ob['sell'] else 1e9
                    best_bid_size = int(ob['buy'][0]['size']) if ob['buy'] else 0
                    best_ask_size = int(ob['sell'][0]['size']) if ob['sell'] else 0

                    if i not in active_orders:
                        target_price = (best_bid + 1) if leg['side'] == "buy" else (best_ask - 1)
                        logger.info(f"Placing order: {leg['name']} @ {target_price}")
                        res = client.place_order(leg['product_id'], leg['qty'] - executed_qty[i], leg['side'], limit_price=target_price)
                        if res.get("success"): active_orders[i] = res['result']
                    else:
                        curr_order = active_orders[i]
                        curr_price = float(curr_order['limit_price'])
                        curr_unfilled = int(curr_order['unfilled_size'])
                        is_best = (curr_price > best_bid or (curr_price == best_bid and best_bid_size <= curr_unfilled)) if leg['side'] == 'buy' else (curr_price < best_ask or (curr_price == best_ask and best_ask_size <= curr_unfilled))

                        if not is_best:
                            target_price = (best_bid + 1) if leg['side'] == "buy" else (best_ask - 1)
                            logger.info(f"Updating {leg['name']} order to {target_price}")
                            client.cancel_order(curr_order['id'], leg['product_id'])
                            res = client.place_order(leg['product_id'], leg['qty'] - executed_qty[i], leg['side'], limit_price=target_price)
                            if res.get("success"): active_orders[i] = res['result']

            # 3. Dynamic Delta Hedging
            # Target: total_delta + sum(hedge_qty * hedge_delta) = 0

            # Current net hedge delta
            current_hedge_delta = 0
            for pid, h_qty in hedge_positions.items():
                if h_qty == 0: continue
                # We'd need symbol to get delta. Let's simplify and always use the LATEST atm instruments.
                pass

            # Simplified Logic per user request:
            # If total_delta is positive (Net Long) -> Buy ATM Puts
            # If total_delta is negative (Net Short) -> Buy ATM Calls

            if total_delta > 0.001 and atm_put:
                ticker_h = client.get_ticker(atm_put['symbol'])
                if ticker_h and ticker_h.get('greeks'):
                    h_delta = float(ticker_h['greeks']['delta']) # Puts have negative delta
                    # needed: total_delta + qty * h_delta = 0 => qty = -total_delta / h_delta
                    target_qty = round(-total_delta / h_delta)
                    # Current put quantity
                    curr_put_qty = hedge_positions.get(atm_put['id'], 0)
                    trade_qty = target_qty - curr_put_qty
                    if abs(trade_qty) >= 1:
                        side = "buy" if trade_qty > 0 else "sell"
                        logger.info(f"HEDGING (Put): {side} {abs(trade_qty)} {atm_put['symbol']} (Main Delta: {total_delta:.4f})")
                        res = client.place_order(atm_put['id'], abs(trade_qty), side, order_type="market_order")
                        if res.get("success"): hedge_positions[atm_put['id']] = curr_put_qty + (trade_qty if side == "buy" else -trade_qty)

            elif total_delta < -0.001 and atm_call:
                ticker_h = client.get_ticker(atm_call['symbol'])
                if ticker_h and ticker_h.get('greeks'):
                    h_delta = float(ticker_h['greeks']['delta']) # Calls have positive delta
                    target_qty = round(-total_delta / h_delta)
                    curr_call_qty = hedge_positions.get(atm_call['id'], 0)
                    trade_qty = target_qty - curr_call_qty
                    if abs(trade_qty) >= 1:
                        side = "buy" if trade_qty > 0 else "sell"
                        logger.info(f"HEDGING (Call): {side} {abs(trade_qty)} {atm_call['symbol']} (Main Delta: {total_delta:.4f})")
                        res = client.place_order(atm_call['id'], abs(trade_qty), side, order_type="market_order")
                        if res.get("success"): hedge_positions[atm_call['id']] = curr_call_qty + (trade_qty if side == "buy" else -trade_qty)

            # If total delta is near zero, we might still have old hedge positions to close
            if abs(total_delta) < 0.001:
                for pid, h_qty in hedge_positions.items():
                    if h_qty != 0:
                        side = "sell" if h_qty > 0 else "buy"
                        logger.info(f"Delta neutral. Reducing hedge in {pid}: {side} {abs(h_qty)}")
                        res = client.place_order(pid, abs(h_qty), side, order_type="market_order")
                        if res.get("success"): hedge_positions[pid] = 0

            time.sleep(1)

        # 4. Final Square off
        logger.info("Main legs executed. Squaring off all hedges...")
        for pid, h_qty in hedge_positions.items():
            if h_qty != 0:
                side = "sell" if h_qty > 0 else "buy"
                client.place_order(pid, abs(h_qty), side, order_type="market_order")

        logger.info("Strategy complete.")

    except KeyboardInterrupt:
        logger.info("Bot stopped. Cancelling orders...")
        for i, order in active_orders.items():
            client.cancel_order(order['id'], legs[i]['product_id'])

if __name__ == "__main__":
    main()
