"""離線驗證全市場流程、交易所解析與通知副作用邊界。"""
import argparse
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests

from src.data import Calendar, DataSource, Http, parse_daily, parse_date
from src.main import TAIPEI, execute
from src.notify import render, send, send_failure_alert
from src.prediction import make_report, rank, validate
from src.storage import Store


class PipelineTests(unittest.TestCase):
    """每個案例使用獨立資料庫與可控時間，不接外部服務。"""

    def setUp(self):
        """建立暫存 SQLite 及合成的120根正常日K。"""
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'market.sqlite3'
        self.store = Store(self.path)
        self.dates = pd.bdate_range(end='2026-09-10', periods=120)
        self.rows = [dict(symbol='2330', date=d.date().isoformat(), market='TWSE', name='台積電',
                          open=100., high=110., low=90., close=100., volume=1000., amount=100000.)
                     for d in self.dates]
        self.report = dict(date='2026-09-10', target_date='2026-09-11', universe_count=2,
                           predictions=[], skipped=[], top20=[])

    def tearDown(self):
        """關閉測試資料庫並清除暫存資料。"""
        self.store.db.close()
        self.temp.cleanup()

    def populate(self, symbols=('2330',)):
        """將相同合成行情分配給 symbols，便於驗證全名單掃描。"""
        for symbol in symbols:
            self.store.save_bars([dict(r, symbol=symbol) for r in self.rows], 'TWSE', '2026-09-10')

    def test_storage_upsert_and_no_future(self):
        """重複日期更新不增列，讀取截止日後的資料不洩漏。"""
        self.populate()
        self.store.save_bars([dict(self.rows[-1], close=105.)], 'TWSE', '2026-09-10')
        self.assertEqual(len(self.store.history('2330', '2026-09-10')), 120)
        self.assertEqual(self.store.history('2330', '2026-09-10').iloc[-1]['close'], 105.)
        self.assertEqual(self.store.history('2330', '2026-09-09').iloc[-1]['date'], '2026-09-09')

    def test_quality_rejects_missing_stale_and_bad_ohlc(self):
        """異常與過期資料不可進入預測排名。"""
        self.populate()
        original = self.store.history('2330', '2026-09-10')
        validate(original, '2026-09-10')
        for column, value in [('open', float('nan')), ('high', 50), ('volume', 0), ('amount', float('inf'))]:
            with self.subTest(column=column):
                frame = original.copy()
                frame.loc[0, column] = value
                with self.assertRaises(ValueError):
                    validate(frame, '2026-09-10')
        with self.assertRaises(ValueError):
            validate(original, '2026-09-11')
        with self.assertRaises(ValueError):
            validate(original.iloc[1:], '2026-09-10')

    def test_ranking_positive_only_and_tie_break(self):
        """不足二十檔不補負值，排名結果不受輸入順序影響。"""
        values = [dict(symbol='B', score=.1), dict(symbol='A', score=.1),
                  dict(symbol='C', score=0), dict(symbol='D', score=-.2)]
        self.assertEqual([p['symbol'] for p in rank(values)], ['A', 'B'])
        self.assertEqual(len(rank([dict(symbol=str(i), score=.01) for i in range(30)])), 20)

    def test_entire_universe_including_etf(self):
        """全市場迴圈不可只跑2330；有行情ETF納入、興櫃缺值記錄略過。"""
        self.populate(('2330', '0050', '3455'))
        predictor = Mock()
        predictor.predict.return_value = {k: self.rows[0][k] for k in ('open', 'high', 'low', 'close', 'volume', 'amount')}
        predictor.predict.return_value['close'] = 105.
        report = make_report(self.store, {s: {'name': s} for s in ('2330', '0050', '3455', '1260')},
                             date(2026, 9, 10), date(2026, 9, 11), lambda: predictor)
        self.assertEqual(predictor.predict.call_count, 3)
        self.assertEqual({p['symbol'] for p in report['top20']}, {'2330', '0050', '3455'})
        self.assertEqual(report['skipped'][0]['symbol'], '1260')

    def test_scans_all_2565_symbols_without_shortlist(self):
        """即使多數資料不足，也必須檢查完整平台清單而非預設觀察股。"""
        store = Mock()
        store.history.return_value = pd.DataFrame()
        universe = {str(i): {'name': str(i)} for i in range(2565)}
        factory = Mock()
        report = make_report(store, universe, date(2026, 9, 10), date(2026, 9, 11), factory)
        self.assertEqual(store.history.call_count, 2565)
        self.assertEqual(len(report['skipped']), 2565)
        factory.assert_not_called()

    def test_model_loading_failure_is_not_silently_empty_success(self):
        """權重缺失應使整個工作失敗，不能冒充零候選的正常結果。"""
        self.populate()
        factory = Mock(side_effect=RuntimeError('missing weights'))
        with self.assertRaises(RuntimeError):
            make_report(self.store, {'2330': {'name': '台積電'}}, date(2026, 9, 10),
                        date(2026, 9, 11), factory)

    def test_prediction_failure_isolated(self):
        """單股推論失敗不阻止其餘商品被觀察。"""
        self.populate(('A', 'B'))
        predictor = Mock()
        predictor.predict.side_effect = [RuntimeError('failure'), dict(open=100, high=110, low=90, close=105, volume=1, amount=100)]
        report = make_report(self.store, {s: {'name': s} for s in ('A', 'B')}, date(2026, 9, 10),
                             date(2026, 9, 11), lambda: predictor)
        self.assertEqual(len(report['skipped']), 1)
        self.assertEqual(report['top20'][0]['symbol'], 'B')

    def test_platform_list_daily_cache(self):
        """同日重跑只呼叫一次商品清單，且沒有 stock_type 請求。"""
        http = Mock()
        http.get.return_value = {'2330': {'name': '台積電'}}
        source = DataSource(self.store, http)
        source.universe(date(2026, 9, 10))
        source.universe(date(2026, 9, 10))
        self.assertEqual(http.get.call_count, 1)
        source.universe(date(2026, 9, 11))
        self.assertEqual(http.get.call_count, 2)

    def test_bulk_parser_volume_and_etf(self):
        """兩市場的整批行情均以股／元儲存，ETF保留其實際市場。"""
        for market, fields in [('TWSE', ['證券代號','證券名稱','開盤價','最高價','最低價','收盤價','成交股數','成交金額']),
                               ('TPEX', ['代號','名稱','開盤','最高','最低','收盤','成交股數','成交金額(元)'])]:
            payload = dict(stat='ok', date='20260910', tables=[dict(fields=fields,
                           data=[['006201','ETF','100','110','90','105','1,000','100,000']])])
            rows = parse_daily(payload, market, date(2026, 9, 10))
            self.assertEqual(rows[0]['volume'], 1000)
            self.assertEqual(rows[0]['amount'], 100000)
            self.assertEqual(rows[0]['market'], market)
            with self.assertRaises(ValueError):
                parse_daily(payload, market, date(2026, 9, 11))

    def test_parser_rejects_schema_drift(self):
        """欄位調整必須阻止錯位資料入庫。"""
        with self.assertRaises(ValueError):
            parse_daily(dict(stat='ok', date='20260910', tables=[dict(fields=['代號'], data=[['A']])]),
                        'TPEX', date(2026, 9, 10))

    def test_date_formats(self):
        """支援交易所民國及西元日期。"""
        for text in ('1150910', '20260910', '115/09/10', '2026-09-10'):
            self.assertEqual(parse_date(text), date(2026, 9, 10))

    def test_calendar_skips_holiday_weekend_and_preserves_opening(self):
        """開始／最後交易日不是假日，跨年會使用新年度日曆。"""
        http = Mock()
        http.get.side_effect = [dict(stat='ok', data=[['2026-01-01','元旦',''], ['2026-01-02','國曆新年開始交易日','']]),
                                dict(stat='ok', data=[['2027-01-01','元旦','']])]
        cal = Calendar(self.store, http, date(2026, 9, 10))
        self.assertFalse(cal.is_session(date(2026, 7, 10)))
        self.assertFalse(cal.is_session(date(2026, 1, 1)))
        self.assertEqual(http.get.call_args.args[1]['date'], '20260101')
        self.assertTrue(cal.is_session(date(2026, 1, 2)))
        self.assertEqual(cal.shift(date(2026, 1, 2), 1), date(2026, 1, 5))
        self.assertEqual(cal.shift(date(2026, 12, 31), 1), date(2027, 1, 4))

    def test_settlement_only_day_overrides_legacy_last_session_label(self):
        """歷史表標題沿用最後交易日，但說明市場無交易者仍須休市。"""
        http = Mock()
        http.get.return_value = dict(stat='ok', data=[
            ['2022-01-26','農曆春節前最後交易日','農曆春節前最後交易。'],
            ['2022-01-27','農曆春節前最後交易日','1月27日市場無交易，僅辦理結算交割作業。']])
        calendar = Calendar(self.store, http, date(2026,9,15))
        self.assertTrue(calendar.is_session(date(2022,1,26)))
        self.assertFalse(calendar.is_session(date(2022,1,27)))

    def test_calendar_rejects_wrong_year(self):
        """未公布年度不能靜默退回舊年度。"""
        http = Mock()
        http.get.return_value = dict(stat='ok', data=[['2025-01-01','元旦','']])
        with self.assertRaises(ValueError):
            Calendar(self.store, http, date(2026, 9, 10)).holidays(2026)

    @patch('src.data.time.sleep')
    def test_get_retries_transient_errors(self, sleep):
        """429、5xx、逾時及分塊回應中斷可重試GET；最多五次。"""
        http = Http(0)
        limited = Mock(status_code=429, headers={'Retry-After': '2'})
        server = Mock(status_code=503, headers={})
        ok = Mock(status_code=200)
        ok.json.return_value = {'ok': True}
        http.session = Mock()
        http.session.get.side_effect = [limited, server, requests.Timeout(),
                                        requests.exceptions.ChunkedEncodingError(), ok]
        self.assertEqual(http.get('https://example.test'), {'ok': True})
        self.assertEqual(http.session.get.call_count, 5)
        http.session.get.side_effect = requests.exceptions.ChunkedEncodingError()
        with self.assertRaises(RuntimeError):
            http.get('https://example.test')
        self.assertEqual(http.session.get.call_count, 10)

    @patch('src.data.time.sleep')
    def test_long_rate_limit_does_not_retry_early(self, sleep):
        """上游要求超過一分鐘時停止，不能截短Retry-After後立即重打。"""
        http = Http(0)
        http.session = Mock()
        http.session.get.return_value = Mock(status_code=429, headers={'Retry-After': '120'})
        with self.assertRaisesRegex(RuntimeError, '60秒'):
            http.get('https://example.test')
        self.assertEqual(http.session.get.call_count, 1)

    @patch('src.data.time.sleep')
    def test_get_does_not_retry_permanent_error(self, sleep):
        """永久錯誤不應反覆增加上游負擔。"""
        http = Http(0)
        http.session = Mock()
        http.session.get.return_value.status_code = 404
        http.session.get.return_value.raise_for_status.side_effect = requests.HTTPError()
        with self.assertRaises(requests.HTTPError):
            http.get('https://example.test')
        self.assertEqual(http.session.get.call_count, 1)

    @patch('src.data.parse_daily')
    def test_incremental_sync_only_downloads_gaps(self, parse):
        """已下載日期不重抓，新缺日每市場只需一次GET。"""
        parse.return_value = self.rows[-1:]
        http = Mock()
        source = DataSource(self.store, http)
        universe = {'2330': {'name': '台積電'}}
        source.sync(universe, [date(2026, 9, 9)])
        self.assertEqual(http.get.call_count, 2)
        source.sync(universe, [date(2026, 9, 9), date(2026, 9, 10)])
        self.assertEqual(http.get.call_count, 4)

    def test_discord_sent_once(self):
        """成功訊息保留ID，同日重跑不再發POST。"""
        client = Mock()
        client.post.return_value = Mock(status_code=200)
        client.post.return_value.json.return_value = {'id': '123'}
        url = 'https://discord.com/api/webhooks/test/token'
        self.assertEqual(send(self.store, self.report, url, client), 'sent')
        self.assertEqual(send(self.store, self.report, url, client), 'already_sent')
        self.assertEqual(client.post.call_count, 1)
        self.assertEqual(client.post.call_args.kwargs['json']['allowed_mentions'], {'parse': []})

    def test_discord_timeout_not_blindly_retried(self):
        """POST逾時可能已送達，必須持久化unknown並阻止重送。"""
        client = Mock()
        client.post.side_effect = requests.Timeout('contains secret URL')
        for _ in range(2):
            with self.assertRaises(RuntimeError) as error:
                send(self.store, self.report, 'https://discord.com/api/webhooks/test/token', client)
            self.assertNotIn('secret', str(error.exception))
        self.assertEqual(client.post.call_count, 1)

    @patch('src.notify.time.sleep')
    def test_discord_rate_limit_retries(self, sleep):
        """只有明確未送達的429可以安全自動重試。"""
        limited = Mock(status_code=429)
        limited.json.return_value = {'retry_after': 1}
        ok = Mock(status_code=200)
        ok.json.return_value = {'id': '123'}
        client = Mock()
        client.post.side_effect = [limited, ok]
        send(self.store, self.report, 'https://discord.com/api/webhooks/test/token', client)
        self.assertEqual(client.post.call_count, 2)

    def test_discord_server_error_or_interruption_blocks_duplicate(self):
        """5xx與程序留下的sending都不能直接再次發送。"""
        client = Mock()
        client.post.return_value = Mock(status_code=503)
        url = 'https://discord.com/api/webhooks/test/token'
        with self.assertRaises(RuntimeError):
            send(self.store, self.report, url, client)
        self.assertEqual(self.store.delivery(self.report['date'])[0], 'unknown')
        self.store.set_delivery(self.report['date'], 'sending')
        with self.assertRaises(RuntimeError):
            send(self.store, self.report, url, client)
        self.assertEqual(client.post.call_count, 1)

    def test_failure_alert_has_no_mentions_or_sensitive_details(self):
        """最終告警只含固定維運資訊，不夾帶例外或觸發 Discord mention。"""
        client = Mock()
        client.post.return_value = Mock(status_code=200)
        webhook = 'https://discord.com/api/webhooks/test/secret-token'
        self.assertEqual(send_failure_alert(webhook, '2026-09-14', client), 'sent')
        payload = client.post.call_args.kwargs['json']
        self.assertEqual(payload['allowed_mentions'], {'parse': []})
        self.assertIn('2026-09-14', payload['content'])
        self.assertNotIn('secret-token', payload['content'])
        self.assertEqual(client.post.call_count, 1)

    def test_systemd_retries_then_alerts_once(self):
        """direct 模式略過中途 OnFailure，達啟動上限後才觸發單一告警單元。"""
        root = Path(__file__).resolve().parents[1]
        daily = (root / 'deploy/kronos-daily.service').read_text(encoding='utf-8')
        alert = (root / 'deploy/kronos-failure-notify.service').read_text(encoding='utf-8')
        self.assertIn('StartLimitBurst=3', daily)
        self.assertIn('Restart=on-failure', daily)
        self.assertIn('RestartMode=direct', daily)
        self.assertEqual(daily.count('OnFailure=kronos-failure-notify.service'), 1)
        self.assertIn('python -m src.alert', alert)

    @patch('src.main.send')
    @patch('src.main.DataSource')
    @patch('src.main.Calendar')
    @patch('src.main.make_report')
    def test_daily_saves_full_report_and_reuses_it(self, make, calendar, source, notify):
        """整合入口先保存報告，重跑不再次下載或推論；no-send全程不發送。"""
        calendar.return_value.is_session.return_value = True
        calendar.return_value.window.return_value = [date(2026, 9, 10)]
        calendar.return_value.shift.return_value = date(2026, 9, 11)
        source.return_value.universe.return_value = {'2330': {'name': '台積電'}}
        make.return_value = self.report
        args = argparse.Namespace(db=self.path, command='daily', date='2026-09-10', no_send=True)
        with patch('builtins.print'):
            execute(args, datetime(2026, 9, 11, 2, tzinfo=TAIPEI))
            execute(args, datetime(2026, 9, 11, 2, tzinfo=TAIPEI))
        self.assertEqual(make.call_count, 1)
        self.assertEqual(source.return_value.sync.call_count, 1)
        self.assertEqual(self.store.run('2026-09-10'), self.report)
        self.assertTrue((self.path.parent / 'reports/2026-09-10.json').exists())
        notify.assert_not_called()

    def test_discord_includes_twentieth_from_legacy_report(self):
        """舊報告即使只保存top10，通知仍從全量預測取出完整前20名。"""
        self.report['predictions'] = [dict(symbol=f'{i:04}', name='測試股票', score=.3-i*.001,
                                          prediction=dict(open=100, close=130-i*.1)) for i in range(25)]
        self.report['top10'] = self.report['predictions'][:10]
        text = render(self.report)
        self.assertIn('20. 0019', text)
        self.assertNotIn('21. 0020', text)
        self.assertLessEqual(len(text), 2000)

    def test_no_positive_candidates_message(self):
        """空榜通知說明不足二十檔與無有效預測。"""
        text = render(self.report)
        self.assertIn('正漲幅僅 0 檔', text)
        self.assertIn('沒有有效預測', text)

    @patch('src.main.Calendar')
    @patch('src.main.DataSource')
    def test_daily_holiday_does_not_fetch_universe(self, source, calendar):
        """休市日不查平台或發通知。"""
        calendar.return_value.is_session.return_value = False
        args = argparse.Namespace(db=self.path, command='daily', date=None, no_send=True)
        execute(args, datetime(2026, 9, 12, 18, tzinfo=TAIPEI))
        source.return_value.universe.assert_not_called()

    def test_daily_before_close_rejected(self):
        """未完成日K不可用於當日排行榜。"""
        args = argparse.Namespace(db=self.path, command='daily', date=None, no_send=True)
        with self.assertRaisesRegex(ValueError, '18:00'):
            execute(args, datetime(2026, 9, 10, 10, tzinfo=TAIPEI))

    @patch('src.main.Http')
    def test_preview_reads_saved_report_without_network(self, http):
        """預覽不載入模型、不請求網路、不送訊息。"""
        self.store.save_run(self.report)
        args = argparse.Namespace(db=self.path, command='preview', date=None, json=False)
        with patch('builtins.print') as output:
            execute(args)
        http.assert_not_called()
        self.assertIn('2026-09-11', output.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
