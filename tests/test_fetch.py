"""Test collector adapters without network or an installed market-data client."""
from pathlib import Path
from decimal import Decimal
from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.error import URLError
import json
import os
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fetch_v4_data import (build_actions, compare_backup, market_calendar, normalize_stock,
                           pair_limits, reference_anomalies, sina_action_detail, tushare_blockers,
                           tushare_stock)
from inputs import trusted, MARKET_HOSTS, FUND_HOSTS, official_etf_history, verified_live_day
from tushare_client import TushareClient, TushareError, implemented_dividends


class Collector(unittest.TestCase):
    def test_short_week_requires_calendar(self):
        inferred=market_calendar(['2023-09-27','2023-09-28','2023-10-09','2023-10-13'], [],
                                 '2023-10-13','2023-10-13')
        self.assertEqual([r['week_end'] for r in inferred],['0','1','0','1'])
        with self.assertRaises(ValueError):
            market_calendar([r['date'] for r in inferred], ['2023-09-28','2023-10-09','2023-10-13'],
                            '2023-10-13','2023-10-13')

    def test_sources(self):
        self.assertTrue(trusted('https://datacenter-web.eastmoney.com/api',MARKET_HOSTS,False))
        self.assertTrue(trusted('https://www.baostock.com/baostock/index.php/Python_API',MARKET_HOSTS,False))
        self.assertFalse(trusted('https://www.baostock.com.bad.example/',MARKET_HOSTS,False))
        self.assertFalse(trusted('https://user@www.baostock.com/',MARKET_HOSTS,False))
        self.assertFalse(trusted('archive:v3.1:002714:quotes',MARKET_HOSTS,False))
        self.assertTrue(trusted('archive:v3.1:002714:quotes',MARKET_HOSTS,False,('archive:v3.1:002714:quotes',)))
        clean='tushare_pro:daily?ts_code=600001.SH'
        self.assertTrue(trusted(clean,MARKET_HOSTS,False))
        self.assertFalse(trusted(clean,FUND_HOSTS,False))
        for bad in (clean+'&token=offline-test',clean+'\n',clean.replace('.SH','.SZ'),
                    clean.replace('daily','trade_cal'),clean.replace('600001','688001')):
            self.assertFalse(trusted(bad,MARKET_HOSTS,False))

    def test_etf_release_basis_uses_exact_official_date_and_live_clock(self):
        sse=('https://query.sse.com.cn/commonQuery.do?'
             'sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L&STAT_DATE=2026-11-20')
        self.assertTrue(official_etf_history(sse,'2026-11-20'))
        self.assertFalse(official_etf_history(sse,'2026-11-19'))
        self.assertFalse(official_etf_history(sse.replace('query.sse.com.cn','sse.com.cn.invalid'),'2026-11-20'))
        self.assertEqual(verified_live_day('2026-11-20T14:56:19+08:00'),'2026-11-20')
        self.assertEqual(verified_live_day('2026-11-20T15:00:00.001+08:00'),'2026-11-21')
        with self.assertRaises(ValueError):
            verified_live_day('2026-11-20')

    def test_limit_rollover_and_explicit_suspension(self):
        self.assertEqual(pair_limits('300750','2018-06-11','2020-08-21',None,Decimal(10),'0'),('bounded','11.00','9.00'))
        self.assertEqual(pair_limits('300750','2018-06-11','2020-08-24',None,Decimal(10),'0'),('bounded','12.00','8.00'))
        self.assertEqual(pair_limits('600001','2023-04-10','2023-04-14',4,Decimal(10),'0'),('unlimited','',''))
        self.assertEqual(pair_limits('600001','2023-04-10','2023-04-17',5,Decimal(10),'0'),('bounded','11.00','9.00'))
        quotes=[dict(date='2020-08-21',preclose='10.00',tradestatus='0',open='10.00')]
        warnings=[]
        rows=normalize_stock('300750',{'listing':'2018-06-11','delisting':''},
                  ['2020-08-21','2020-08-24'],quotes,{}, {},'vendor','vendor',warnings.append)
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['status'],rows[0]['open']),('suspended',''))
        self.assertIn('2020-08-24',warnings[0])
        evidence={('300750','2020-08-21'):dict(reference='9.75',source='https://www.szse.cn/')}
        adjusted=normalize_stock('300750',{'listing':'2018-06-11','delisting':''},
                  ['2020-08-21'],quotes,{},evidence,'vendor','vendor',warnings.append)
        self.assertEqual((adjusted[0]['status'],adjusted[0]['close'],adjusted[0]['reference']),('suspended','','9.75'))

    def test_pay_and_listing_dates(self):
        base=dict(dividOperateDate='2025-05-20',dividPlanDate='2025-05-14',dividPayDate='2025-05-20',
                  dividStockMarketDate='2025-05-21',dividCashPsBeforeTax='0.5',dividStocksPs='0.0',
                  dividReserveToStockPs='0.2')
        b,issues=build_actions([base,base],'601999','vendor')
        self.assertFalse(issues)
        self.assertEqual(b['2025-05-20']['cash_per_share'],'0.5')
        self.assertEqual(b['2025-05-20']['share_date'],'2025-05-21')
        far=dict(base,dividPlanDate='2025-05-20')
        self.assertEqual(build_actions([far],'601999','vendor'),({},['2025-05-20']))

    def test_secondary_action_requires_explicit_dates_and_matches_reference(self):
        fields=[['除息日','2025-05-20'],['税前红利（报价币种）','1.0'],
                ['转增比例（10转增）','1.0'],['上市日','2025-05-21'],
                ['红利/配股起始日（送、转股到账日）','2025-05-20']]
        action=sina_action_detail('601999','2025-05-14','2025-05-20',fields,'vendor')
        tape=[dict(date='2025-05-19',close='11.10'),
              dict(date='2025-05-20',reference='10.00',close='10.01')]
        self.assertIn('2025-05-20',reference_anomalies(tape,{}))
        self.assertFalse(reference_anomalies(tape,{'2025-05-20':action}))
        with self.assertRaises(ValueError):
            sina_action_detail('601999','2025-05-14','2025-05-20',fields[:-1],'vendor')

    def test_frozen_comparison_does_not_accept_one_tick_or_volume_difference(self):
        prior=dict(date='2025-05-20',tradestatus='1',open='11.00',close='11.01',
                   high='11.02',low='10.99',volume='10000',preclose='10.996')
        candidate=dict(prior,preclose='11.00')
        self.assertFalse(compare_backup('002714',[candidate],[prior],lambda _:None))
        candidate['close']='11.02'
        self.assertEqual(compare_backup('002714',[candidate],[prior],lambda _:None),{'2025-05-20'})


class Tushare(unittest.TestCase):
    symbol = '600001.SH'
    mock_key = 'offline-fixture-key'

    def make_client(self, root, **kwargs):
        with patch.dict(os.environ, {}, clear=True):
            (Path(root)/'.env').write_text('TUSHARE_HTTP_URL=https://quotes.example/\n'
                                         f'TUSHARE_TOKEN={self.mock_key}\n', encoding='utf-8')
            return TushareClient(root, request_interval=0, **kwargs)

    @staticmethod
    def reply(fields, items):
        return BytesIO(json.dumps(dict(code=0, data=dict(fields=fields, items=items))).encode())

    def test_post_retry_cache_and_safe_receipt(self):
        calls, pauses = [], []

        def transport(request, timeout):
            calls.append((request.get_method(), json.loads(request.data), timeout))
            if len(calls) == 1:
                raise URLError('Internal gateway diagnostic')
            return self.reply(['ts_code'], [[self.symbol]])

        with TemporaryDirectory() as root:
            client = self.make_client(root, transport=transport, pause=pauses.append)
            params, fields = dict(ts_code=self.symbol), ('ts_code',)
            expected = [dict(ts_code=self.symbol)]
            self.assertEqual(client.query('daily', params, fields), expected)
            self.assertEqual(pauses, [1])
            self.assertEqual(calls, [('POST', dict(api_name='daily', token=self.mock_key,
                                                 params=params, fields='ts_code'), 20)]*2)
            never = Mock(side_effect=AssertionError('cache miss'))
            cached = self.make_client(root, transport=never, offline=True)
            self.assertEqual(cached.query('daily', params, fields), expected)
            self.assertEqual(cached.receipts, client.receipts)
            payload = next(client.cache.glob('*.json')).read_text(encoding='utf-8')
            self.assertIn(f'tushare_pro:daily?ts_code={self.symbol}', payload)
            self.assertNotIn(self.mock_key, payload)
            self.assertNotIn('quotes.example', payload)
            never.assert_not_called()

    def test_reflected_credentials_are_rejected_without_cache_or_exception_leak(self):
        with TemporaryDirectory() as root:
            # Exercise decoded-value checking as well as raw response checking.
            returned = self.reply(['ts_code'], [[self.mock_key]]).getvalue().replace(b'offline', b'\\u006fffline')
            client = self.make_client(root, transport=lambda *_args, **_kw: BytesIO(returned))
            with self.assertRaisesRegex(TushareError, '响应含凭据') as error:
                client.query('daily', {'ts_code': self.symbol}, ('ts_code',))
            self.assertNotIn(self.mock_key, str(error.exception))
            self.assertFalse(client.cache.exists())

    def test_gateway_json_rate_limit_retries(self):
        gateway = Mock(side_effect=[BytesIO(b'{"code":429}'), self.reply(['ts_code'], [[self.symbol]])])
        pauses = []
        with TemporaryDirectory() as root:
            client = self.make_client(root, transport=gateway, pause=pauses.append)
            self.assertEqual(client.query('daily', {'ts_code': self.symbol}, ('ts_code',)),
                             [dict(ts_code=self.symbol)])
            self.assertEqual(pauses,[2])

    def test_explicit_empty_result_can_be_cached(self):
        with TemporaryDirectory() as root:
            client = self.make_client(root, transport=lambda *_a, **_kw:self.reply([], []))
            self.assertEqual(client.query('suspend_d', {'ts_code': self.symbol}, ('ts_code',)), [])
            self.assertEqual(len(client.receipts),1)

    def test_saturated_query_splits_year_boundary(self):
        ranges = []

        def transport(request, **_):
            params = json.loads(request.data)['params']
            start, end = params['start_date'], params['end_date']
            ranges.append((start, end))
            items = [[start]] if start == end else [[start], [end]]
            return self.reply(['trade_date'], items)

        with TemporaryDirectory() as root, patch('tushare_client.CAP', 2):
            client = self.make_client(root, transport=transport)
            rows = client.dated('daily', {}, ('trade_date',), '2025-12-31', '2026-01-01')
            self.assertEqual(rows, [dict(trade_date='20251231'), dict(trade_date='20260101')])
            self.assertEqual(ranges, [('20251231','20260101'), ('20251231','20251231'), ('20260101','20260101')])

    def test_market_mapping_requires_explicit_full_suspension_and_implemented_action(self):
        code = '600001'
        market_days = ['2025-05-19', '2025-05-20', '2025-05-21']
        client = Mock()
        client.instrument.return_value = dict(list_date='20200519', delist_date=None, list_status='L')
        client.names.return_value = [dict(name='示例', start_date='20200519', end_date=None, ann_date='20200518')]
        base = dict(ts_code=self.symbol, open='10', high='10.1', low='9.9', close='10',
                    pre_close='10', vol='23.01', amount='56.789')
        client.daily.return_value = [dict(base, trade_date='20250519'), dict(base, trade_date='20250521')]
        stop = dict(ts_code=self.symbol, trade_date='20250520', suspend_type='S', suspend_timing='13:00-14:00')
        client.suspensions.return_value = [stop]
        with self.assertRaisesRegex(ValueError, '2025-05-20'):
            tushare_stock(client, code, market_days, {}, {})
        stop['suspend_timing'] = '全天'
        event = dict(ts_code=self.symbol, ex_date='20250521', record_date='20250520', pay_date='20250521',
                     imp_ann_date='20250519', div_listdate='20250522', cash_div_tax='0.10',
                     stk_bo_rate='0.10', stk_co_rate='0.20', div_proc='实施')
        client.dividends.return_value = implemented_dividends([event,dict(event,div_proc='预案')], code,
                                                               market_days[0], market_days[-1])
        meta, stocks, actions, observed, name, issues = tushare_stock(client, code, market_days, {}, {})
        self.assertEqual(meta['listing'], '2020-05-19')
        self.assertEqual(name, '示例')
        self.assertEqual(issues, [])
        self.assertEqual((Decimal(stocks[0]['volume']), Decimal(observed[0]['amount'])), (2301, 56789))
        self.assertEqual([row['status'] for row in stocks], ['open', 'suspended', 'open'])
        self.assertEqual((stocks[1]['open'], stocks[1]['reference'], observed[1]['amount']), ('', '', '0'))
        self.assertEqual(stocks[1]['source'], f'tushare_pro:suspend_d?ts_code={self.symbol}')
        self.assertEqual((len(actions), actions[market_days[-1]]['bonus_per_share']), (1, '0.30'))

    def test_tushare_requires_calibration(self):
        with TemporaryDirectory() as root:
            root = Path(root)
            self.assertEqual(len(tushare_blockers(root, '2026-11-20')), 1)
            research = root/'results'/'research'
            research.mkdir(parents=True)
            (research/'tushare_baseline_check.json').write_text(
                json.dumps(dict(asof='2026-11-20', calibration_passed=True)), encoding='utf-8')
            self.assertIn('切片签名', tushare_blockers(root, '2026-11-20')[0])


if __name__=='__main__':
    unittest.main()
