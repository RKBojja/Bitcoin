import unittest
from unittest.mock import patch
from trading_bot import DeltaClient, get_atm_daily_options
from datetime import datetime

class TestTradingBot(unittest.TestCase):
    def setUp(self):
        self.client = DeltaClient("key", "secret")

    def test_signature_generation(self):
        client = DeltaClient("a207900b7693435a8fa9230a38195d", "7b6f39dcf660ec1c7c664f612c60410a2bd0c258416b498bf0311f94228f")
        sig = client._generate_signature("GET", "/v2/orders", "?product_id=1&state=open", "", "1542110948")
        self.assertEqual(sig, "4e38dda3e6477092f360ba70399266d8145630b22bcc34c0ec7f804d5746877a")

    @patch('trading_bot.DeltaClient.get_products')
    def test_get_atm_daily_options(self, mock_get_products):
        today_str = datetime.now().strftime("%d%m%y")
        mock_get_products.return_value = [
            {"symbol": f"C-BTC-60000-{today_str}", "id": 101},
            {"symbol": f"P-BTC-60000-{today_str}", "id": 102},
            {"symbol": f"C-BTC-65000-{today_str}", "id": 103},
            {"symbol": f"P-BTC-65000-{today_str}", "id": 104},
        ]

        atm_call, atm_put = get_atm_daily_options(self.client, 64000)
        self.assertEqual(atm_call['id'], 103)
        self.assertEqual(atm_put['id'], 104)

        atm_call_2, atm_put_2 = get_atm_daily_options(self.client, 61000)
        self.assertEqual(atm_call_2['id'], 101)
        self.assertEqual(atm_put_2['id'], 102)

if __name__ == '__main__':
    unittest.main()
