import unittest
from unittest.mock import patch

import pandas as pd

from stock_api import core
from stock_api.fetchers import (
    get_esb_stock_data,
    get_tpex_stock_data,
    get_twse_stock_data,
)


class StockFetcherTests(unittest.TestCase):
    @patch("stock_api.fetchers.time.sleep", return_value=None)
    @patch("stock_api.fetchers.safe_get_json")
    def test_twse_parses_numeric_and_special_values(self, mock_get, _mock_sleep):
        mock_get.return_value = {
            "stat": "OK",
            "fields": [
                "日期", "成交股數", "成交金額", "開盤價", "最高價",
                "最低價", "收盤價", "漲跌價差", "成交筆數", "註記",
            ],
            "data": [[
                "113/01/02", "1,234", "123,400", "--", "101.0",
                "99.0", "100.0", "X0.50", "88", "",
            ]],
        }

        result = get_twse_stock_data("2330", "2024-01-01", "2024-01-31")

        self.assertEqual(len(result), 1)
        self.assertEqual(result.loc[0, "capacity"], 1234)
        self.assertTrue(pd.isna(result.loc[0, "open"]))
        self.assertEqual(result.loc[0, "change"], 0.5)
        self.assertFalse(bool(result.loc[0, "close_is_proxy"]))

    @patch("stock_api.fetchers.time.sleep", return_value=None)
    @patch("stock_api.fetchers.safe_get_json")
    def test_tpex_supports_thousand_share_field(self, mock_get, _mock_sleep):
        mock_get.return_value = {
            "stat": "ok",
            "tables": [{
                "fields": [
                    "日 期", "成交仟股", "成交仟元", "開盤", "最高",
                    "最低", "收盤", "漲跌", "筆數",
                ],
                "data": [[
                    "113/01/02", "104", "7,177", "68.0", "70.0",
                    "67.0", "69.0", "+1.0", "85",
                ]],
            }],
        }

        result = get_tpex_stock_data("3455", "2024-01-01", "2024-01-31")

        self.assertEqual(result.loc[0, "capacity"], 104000)
        self.assertEqual(result.loc[0, "turnover"], 7177000)
        self.assertEqual(result.loc[0, "market"], "TPEX")

    @patch("stock_api.fetchers.time.sleep", return_value=None)
    @patch("stock_api.fetchers.safe_get_json")
    def test_tpex_supports_share_count_without_extra_multiplier(self, mock_get, _mock_sleep):
        mock_get.return_value = {
            "stat": "ok",
            "tables": [{
                "fields": [
                    "日 期", "成交股數", "成交金額", "開盤", "最高",
                    "最低", "收盤", "漲跌", "筆數",
                ],
                "data": [[
                    "113/01/02", "104,000", "7,177,000", "68.0", "70.0",
                    "67.0", "69.0", "+1.0", "85",
                ]],
            }],
        }

        result = get_tpex_stock_data("3455", "2024-01-01", "2024-01-31")

        self.assertEqual(result.loc[0, "capacity"], 104000)
        self.assertEqual(result.loc[0, "turnover"], 7177000)

    @patch("stock_api.fetchers.time.sleep", return_value=None)
    @patch("stock_api.fetchers.safe_get_json")
    def test_esb_output_is_numeric_and_marks_proxy_close(self, mock_get, _mock_sleep):
        mock_get.return_value = {
            "stat": "ok",
            "tables": [{
                "data": [
                    [
                        "113/01/02",
                        "1,000", "50,000", "51", "49", "50", "10",
                        "500", "26,000", "53", "50", "52", "5",
                    ],
                    [
                        "113/01/03",
                        "0", "0", "0", "0", "0", "0",
                        "0", "0", "0", "0", "0", "0",
                    ],
                ]
            }],
        }

        result = get_esb_stock_data("1260", "2024-01-01", "2024-01-31")

        self.assertEqual(result.loc[0, "capacity"], 1500)
        self.assertAlmostEqual(result.loc[0, "close"], 76000 / 1500)
        self.assertEqual(result.loc[0, "high"], 53)
        self.assertEqual(result.loc[0, "low"], 49)
        self.assertTrue(pd.isna(result.loc[0, "open"]))
        self.assertTrue(pd.isna(result.loc[1, "close"]))
        self.assertTrue(bool(result.loc[0, "close_is_proxy"]))

        for column in ["open", "high", "low", "close", "change"]:
            self.assertTrue(pd.api.types.is_numeric_dtype(result[column]), column)

    @patch("stock_api.fetchers.time.sleep", return_value=None)
    @patch("stock_api.fetchers.safe_get_json")
    def test_esb_rejects_unexpected_column_count(self, mock_get, _mock_sleep):
        mock_get.return_value = {
            "stat": "ok",
            "tables": [{"data": [["113/01/02", "1", "2"]]}],
        }

        with self.assertRaisesRegex(ValueError, "欄位數"):
            get_esb_stock_data("1260", "2024-01-01", "2024-01-31")

    def test_legacy_schema_preserves_market_and_proxy_flag(self):
        source = pd.DataFrame([{
            "date": pd.Timestamp("2024-01-02"),
            "capacity": 1000,
            "turnover": 50000,
            "high": 51.0,
            "low": 49.0,
            "close": 50.0,
            "change": 1.0,
            "transaction_volume": 10,
            "stock_code_id": "1260",
            "open": float("nan"),
            "market": "ESB",
            "close_is_proxy": True,
        }])

        result = core.to_legacy_schema(source)

        self.assertIn("market", result.columns)
        self.assertIn("close_is_proxy", result.columns)
        self.assertTrue(bool(result.loc[0, "close_is_proxy"]))


if __name__ == "__main__":
    unittest.main()
