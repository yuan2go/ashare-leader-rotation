"""Regression checks for rolling endpoints and paired release comparisons."""
from datetime import date, timedelta
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'research')]

import exp_50_pool_phase2b as study


class Phase2BTests(unittest.TestCase):
    def test_rolling_windows_preserve_the_anchor_and_isolate_short_tail(self):
        first, last=date(2023,1,20),date(2026,11,20)
        dates=[first+timedelta(days=i) for i in range((last-first).days+1)]
        market=[d.isoformat() for d in dates if d.weekday()<5]
        windows=study.make_windows(market,first.isoformat(),last.isoformat(),
                                   ('2026-01-01',last.isoformat()))
        complete=[w for w in windows if w.track=='滚动' and w.complete]
        self.assertEqual(len(complete),6)
        self.assertEqual((complete[0].start,complete[0].first,complete[0].last),
                         ('2023-01-21','2023-01-23','2024-01-19'))
        self.assertEqual([w.start for w in complete[::2]],['2023-01-21','2024-01-21','2025-01-21'])
        tail=next(w for w in windows if not w.complete)
        self.assertEqual((tail.key,tail.start,tail.last),('R7','2026-01-21','2026-11-20'))
        self.assertEqual(study.shift_months(date(2024,2,29),12),date(2025,2,28))

    def test_delay_and_factor_drawdown_deltas_have_opposite_direction(self):
        a=dict(net=.1,dd=-.2,calmar=.5)
        b=dict(net=.08,dd=-.25,calmar=.3)
        result=study.execution_delta({'2bp':a,'T+2':b})
        self.assertAlmostEqual(result['net_pp'],-2.)
        self.assertAlmostEqual(result['dd_pp'],5.)
        self.assertAlmostEqual(result['retention'],.8)
        self.assertIsNone(study.retention(-.1,-.09))
        self.assertAlmostEqual(study.delta(b,a)['improved_dd_pp'],-5.)

    def test_a_good_short_tail_cannot_rescue_f3_or_the_structure(self):
        def book(calmar, net, dd):
            return {'2bp':dict(net=net,dd=dd,calmar=calmar),
                    'T+2':dict(net=net,dd=dd,calmar=calmar)}

        research={}
        for key in ('OOS','R1','R2','R3','R4','R5','R6','R7'):
            research[key]={study.BASE:book(.8,.20,-.10),study.EQUAL:book(.2,.1,-.15),
                           study.B0:book(.5,.1,-.15),study.MODES[0]:book(.9,.22,-.09),
                           study.MODES[1]:book(.6,.15,-.09)}
        windows={key:dict(track='连续' if key=='OOS' else '滚动',complete=key!='R7')
                 for key in research}
        strong=dict(research=research,windows=windows)
        weak_books={key:{**row,study.MODES[0]:book(.9,.22,-.09) if key in ('OOS','R7','R1','R2','R3')
                   else book(.7,.18,-.10)} for key,row in research.items()}
        weak=dict(research=weak_books,windows=windows)
        result=study.summarize_pools({'50 股':strong,'49 股':weak})
        self.assertTrue(result['ready'])
        self.assertEqual(result['qualifying_F3_modes'],[])
        self.assertEqual(result['factor'][study.MODES[0]]['49 股']['2bp']['qualifying_windows'],
                         ['R1','R2','R3'])
        for key in ('R1','R2','R3','R4'):
            weak_books[key][study.BASE]['T+2']=dict(net=-.1,dd=-.10,calmar=-.3)
        self.assertFalse(study.summarize_pools({'50 股':strong,'49 股':weak})['ready'])


if __name__=='__main__':
    unittest.main()
