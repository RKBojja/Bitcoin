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
            if response.status_code >= 400:
                logger.error(f"API Error: {response.status_code} {response.text}")
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

def get_atm_daily_option(client, spot_price):
    today_str = datetime.now().strftime("%d%m%y")
    products = client.get_products({"contract_types": "call_options", "states": "live"})
    btc_today_calls = [p for p in products if p['symbol'].startswith('C-BTC-') and p['symbol'].endswith(today_str)]
    if not btc_today_calls: return None
    closest_call = min(btc_today_calls, key=lambda p: abs(float(p['symbol'].split('-')[2]) - spot_price))
    return closest_call

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
    hedge_qty = 0
    hedge_product_id = None

    logger.info("Starting execution loop...")

    try:
        # Cache daily options to avoid hitting rate limits
        daily_options_cache = []
        last_cache_update = 0
        cache_ttl = 300 # 5 minutes

        while any(executed_qty[i] < legs[i]['qty'] for i in range(len(legs))):
            btc_ticker = client.get_ticker("BTCUSD")
            if not btc_ticker:
                time.sleep(1)
                continue
            spot_price = float(btc_ticker['spot_price'])

            # Update daily options cache if needed
            if time.time() - last_cache_update > cache_ttl:
                logger.info("Updating daily options cache...")
                daily_options_cache = client.get_products({"contract_types": "call_options", "states": "live"})
                last_cache_update = time.time()

            # Find ATM daily call for hedging from cache
            today_str = datetime.now().strftime("%d%m%y")
            btc_today_calls = [p for p in daily_options_cache if p['symbol'].startswith('C-BTC-') and p['symbol'].endswith(today_str)]

            atm_call = None
            if btc_today_calls:
                atm_call = min(btc_today_calls, key=lambda p: abs(float(p['symbol'].split('-')[2]) - spot_price))
                hedge_product_id = atm_call['id']

            total_delta = 0
            for i, leg in enumerate(legs):
                # Check status of active order
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

                # Calculate cumulative delta for executed parts
                if executed_qty[i] > 0:
                    ticker = client.get_ticker(leg['symbol'])
                    if ticker and ticker.get('greeks'):
                        delta = float(ticker['greeks']['delta'])
                        side_mult = 1 if leg['side'] == 'buy' else -1
                        total_delta += executed_qty[i] * delta * side_mult

                # Price Management for unexecuted part
                if executed_qty[i] < leg['qty']:
                    ob = client.get_l2_orderbook(leg['symbol'])
                    if not ob: continue

                    best_bid = float(ob['buy'][0]['price']) if ob['buy'] else 0
                    best_ask = float(ob['sell'][0]['price']) if ob['sell'] else 1e9

                    best_bid_size = int(ob['buy'][0]['size']) if ob['buy'] else 0
                    best_ask_size = int(ob['sell'][0]['size']) if ob['sell'] else 0

                    if i not in active_orders:
                        target_price = (best_bid + 1) if leg['side'] == 'buy' else (best_ask - 1)
                        logger.info(f"Placing initial order for {leg['name']} at {target_price}")
                        res = client.place_order(leg['product_id'], leg['qty'] - executed_qty[i], leg['side'], limit_price=target_price)
                        if res.get("success"):
                            active_orders[i] = res['result']
                    else:
                        curr_order = active_orders[i]
                        curr_price = float(curr_order['limit_price'])
                        curr_unfilled = int(curr_order['unfilled_size'])

                        is_best = False
                        if leg['side'] == 'buy':
                            # Best if I am at top and NO ONE ELSE is at my price or higher
                            # If best_bid > my_price, I am not best.
                            # If best_bid == my_price and best_bid_size > my_unfilled, I am not uniquely best.
                            if curr_price < best_bid or (curr_price == best_bid and best_bid_size > curr_unfilled):
                                is_best = False
                            else:
                                is_best = True
                            target_price = best_bid + 1 if not is_best else curr_price
                        else:
                            if curr_price > best_ask or (curr_price == best_ask and best_ask_size > curr_unfilled):
                                is_best = False
                            else:
                                is_best = True
                            target_price = best_ask - 1 if not is_best else curr_price

                        if not is_best:
                            logger.info(f"Updating {leg['name']} order to {target_price} (Market best: {best_bid if leg['side']=='buy' else best_ask})")
                            client.cancel_order(curr_order['id'], leg['product_id'])
                            res = client.place_order(leg['product_id'], leg['qty'] - executed_qty[i], leg['side'], limit_price=target_price)
                            if res.get("success"):
                                active_orders[i] = res['result']

            # Delta Hedging
            if total_delta != 0 and atm_call:
                ticker_hedge = client.get_ticker(atm_call['symbol'])
                if ticker_hedge and ticker_hedge.get('greeks'):
                    h_delta = float(ticker_hedge['greeks']['delta'])
                    if h_delta != 0:
                        target_hedge_qty = -total_delta / h_delta
                        qty_to_trade = round(target_hedge_qty - hedge_qty)
                        if abs(qty_to_trade) >= 1:
                            side = "buy" if qty_to_trade > 0 else "sell"
                            logger.info(f"HEDGING: {side} {abs(qty_to_trade)} contracts of {atm_call['symbol']} (Net Delta: {total_delta:.4f})")
                            res = client.place_order(atm_call['id'], abs(qty_to_trade), side, order_type="market_order")
                            if res.get("success"):
                                hedge_qty += qty_to_trade

            time.sleep(1)

        if hedge_qty != 0 and hedge_product_id:
            side = "sell" if hedge_qty > 0 else "buy"
            logger.info(f"Square off hedge: {side} {abs(hedge_qty)} contracts.")
            client.place_order(hedge_product_id, abs(hedge_qty), side, order_type="market_order")

        logger.info("Task complete.")

    except KeyboardInterrupt:
        logger.info("Bot stopped. Cancelling active orders...")
        for i, order in active_orders.items():
            client.cancel_order(order['id'], legs[i]['product_id'])

if __name__ == "__main__":
    main()
