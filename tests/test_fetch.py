"""Test collector adapters without network or an installed market-data client."""
from pathlib import Path
from decimal import Decimal
from tempfile import TemporaryDirectory
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fetch_v4_data import (build_actions, compare_backup, market_calendar, normalize_stock,
                           pair_limits, reference_anomalies, sina_action_detail)
from inputs import trusted, MARKET_HOSTS, FUND_HOSTS


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


if __name__=='__main__':
    unittest.main()
