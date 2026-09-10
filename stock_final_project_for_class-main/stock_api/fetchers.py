import time

import pandas as pd

from .utils import month_starts, roc_to_ad, safe_get_json, clean_numeric


OUTPUT_COLUMNS = [
    "date", "stock_code_id", "market",
    "capacity", "turnover", "open", "high", "low", "close", "change",
    "transaction_volume", "close_is_proxy",
]


def _empty_result() -> pd.DataFrame:
    return pd.DataFrame(columns=OUTPUT_COLUMNS)


def _require_columns(df: pd.DataFrame, required_columns: list[str], market: str) -> None:
    """檢查官方回傳欄位，欄位調整時提供可讀的錯誤訊息。"""
    missing = [column for column in required_columns if column not in df.columns]
    if missing:
        raise ValueError(
            f"{market} 回傳資料缺少必要欄位: {missing}; "
            f"實際欄位: {list(df.columns)}"
        )


# TWSE
def get_twse_stock_data(stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
    all_df = []

    for month_start in month_starts(start_date, end_date):
        params = {
            "response": "json",
            "date": pd.Timestamp(month_start).strftime("%Y%m%d"),
            "stockNo": stock_code,
        }

        raw = safe_get_json(url, params=params)

        if raw.get("stat") != "OK" or not raw.get("data"):
            time.sleep(2.0)
            continue

        df = pd.DataFrame(raw["data"], columns=raw["fields"])

        df = df.rename(columns={
            "日期": "date",
            "成交股數": "capacity",
            "成交金額": "turnover",
            "開盤價": "open",
            "最高價": "high",
            "最低價": "low",
            "收盤價": "close",
            "漲跌價差": "change",
            "成交筆數": "transaction_volume",
        })

        numeric_cols = [
            "capacity", "turnover", "open", "high",
            "low", "close", "change", "transaction_volume",
        ]
        _require_columns(df, ["date", *numeric_cols], "TWSE")

        df["date"] = df["date"].apply(roc_to_ad)
        for col in numeric_cols:
            df[col] = clean_numeric(df[col])

        df["stock_code_id"] = str(stock_code)
        df["market"] = "TWSE"
        df["close_is_proxy"] = False

        all_df.append(df)
        time.sleep(2.0)

    if not all_df:
        return _empty_result()

    result = pd.concat(all_df, ignore_index=True)
    result = result[
        (result["date"] >= pd.Timestamp(start_date))
        & (result["date"] <= pd.Timestamp(end_date))
    ].copy()
    result = result.sort_values("date").reset_index(drop=True)

    return result[OUTPUT_COLUMNS]


# TPEX
def get_tpex_stock_data(stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    url = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
    all_df = []

    for month_start in month_starts(start_date, end_date):
        params = {
            "code": stock_code,
            "date": month_start,
            "id": "",
            "response": "json",
        }

        raw = safe_get_json(url, params=params)

        if raw.get("stat", "").lower() != "ok":
            time.sleep(2.0)
            continue

        tables = raw.get("tables", [])
        if not tables:
            time.sleep(2.0)
            continue

        table = tables[0]
        data = table.get("data", [])
        fields = table.get("fields", [])

        if not data or not fields:
            time.sleep(2.0)
            continue

        df = pd.DataFrame(data, columns=fields)

        # TPEX 歷史資料曾使用不同名稱，依實際欄名決定單位換算。
        capacity_candidates = {
            "成交仟股": 1000,
            "成交張數": 1000,
            "成交股數": 1,
        }
        turnover_candidates = {
            "成交仟元": 1000,
            "成交金額": 1,
        }

        capacity_source = next(
            (name for name in capacity_candidates if name in df.columns), None
        )
        turnover_source = next(
            (name for name in turnover_candidates if name in df.columns), None
        )

        if capacity_source is None:
            raise ValueError(
                "TPEX 回傳資料找不到成交量欄位; "
                f"實際欄位: {list(df.columns)}"
            )
        if turnover_source is None:
            raise ValueError(
                "TPEX 回傳資料找不到成交金額欄位; "
                f"實際欄位: {list(df.columns)}"
            )

        df = df.rename(columns={
            "日 期": "date",
            capacity_source: "capacity",
            turnover_source: "turnover",
            "開盤": "open",
            "最高": "high",
            "最低": "low",
            "收盤": "close",
            "漲跌": "change",
            "筆數": "transaction_volume",
        })

        numeric_cols = [
            "capacity", "turnover", "open", "high",
            "low", "close", "change", "transaction_volume",
        ]
        _require_columns(df, ["date", *numeric_cols], "TPEX")

        df["date"] = df["date"].apply(roc_to_ad)
        for col in numeric_cols:
            df[col] = clean_numeric(df[col])

        df["capacity"] = df["capacity"] * capacity_candidates[capacity_source]
        df["turnover"] = df["turnover"] * turnover_candidates[turnover_source]

        df["stock_code_id"] = str(stock_code)
        df["market"] = "TPEX"
        df["close_is_proxy"] = False

        all_df.append(df)
        time.sleep(2.0)

    if not all_df:
        return _empty_result()

    result = pd.concat(all_df, ignore_index=True)
    result = result[
        (result["date"] >= pd.Timestamp(start_date))
        & (result["date"] <= pd.Timestamp(end_date))
    ].copy()
    result = result.sort_values("date").reset_index(drop=True)

    return result[OUTPUT_COLUMNS]


# ESB
def get_esb_stock_data(stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    url = "https://www.tpex.org.tw/www/zh-tw/emerging/historical"
    all_df = []

    for month_start in month_starts(start_date, end_date):
        params = {
            "type": "Monthly",
            "date": month_start,
            "code": stock_code,
            "id": "",
            "response": "json",
        }

        raw = safe_get_json(url, params=params)

        if raw.get("stat", "").lower() != "ok":
            time.sleep(2.0)
            continue

        tables = raw.get("tables", [])
        if not tables:
            time.sleep(2.0)
            continue

        table = tables[0]
        data = table.get("data", [])

        if not data:
            time.sleep(2.0)
            continue

        # ESB 目前為日期 + 兩組各 6 欄。先檢查長度，避免欄位調整後靜默錯位。
        expected_column_count = 13
        invalid_rows = [index for index, row in enumerate(data) if len(row) != expected_column_count]
        if invalid_rows:
            row_lengths = sorted({len(data[index]) for index in invalid_rows})
            raise ValueError(
                "ESB 回傳欄位數與預期不符; "
                f"預期 {expected_column_count} 欄，實際出現 {row_lengths} 欄"
            )

        columns = [
            "date",
            "capacity_1", "turnover_1", "high_1", "low_1", "avg_1", "txn_1",
            "capacity_2", "turnover_2", "high_2", "low_2", "avg_2", "txn_2",
        ]
        df = pd.DataFrame(data, columns=columns)
        df["date"] = df["date"].apply(roc_to_ad)

        numeric_cols = [
            "capacity_1", "turnover_1", "high_1", "low_1", "avg_1", "txn_1",
            "capacity_2", "turnover_2", "high_2", "low_2", "avg_2", "txn_2",
        ]
        for col in numeric_cols:
            df[col] = clean_numeric(df[col]).fillna(0)

        df["capacity"] = df["capacity_1"] + df["capacity_2"]
        df["turnover"] = df["turnover_1"] + df["turnover_2"]
        df["transaction_volume"] = df["txn_1"] + df["txn_2"]

        high_candidates = df[["high_1", "high_2"]].mask(
            df[["high_1", "high_2"]] == 0
        )
        low_candidates = df[["low_1", "low_2"]].mask(
            df[["low_1", "low_2"]] == 0
        )
        df["high"] = pd.to_numeric(
            high_candidates.max(axis=1, skipna=True), errors="coerce"
        )
        df["low"] = pd.to_numeric(
            low_candidates.min(axis=1, skipna=True), errors="coerce"
        )

        # ESB 無標準收盤價，以兩種成交方式的加權平均成交價作為代理值。
        df["close"] = df["turnover"].div(df["capacity"].replace(0, float("nan")))

        # ESB 無標準開盤價，使用 float NaN 以維持數值 dtype。
        df["open"] = float("nan")

        df["stock_code_id"] = str(stock_code)
        df["market"] = "ESB"
        df["close_is_proxy"] = True

        all_df.append(df)
        time.sleep(2.0)

    if not all_df:
        return _empty_result()

    result = pd.concat(all_df, ignore_index=True)
    result = result[
        (result["date"] >= pd.Timestamp(start_date))
        & (result["date"] <= pd.Timestamp(end_date))
    ].copy()
    result = result.sort_values("date").reset_index(drop=True)

    # 用代理 close 計算日變動。
    result["change"] = pd.to_numeric(result["close"].diff(), errors="coerce")

    return result[OUTPUT_COLUMNS]
