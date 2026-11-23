"""PIT forecast, preliminary earnings and annual-report event study."""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import csv
import re
from urllib.parse import urlencode, urljoin, urlsplit

from common import CHINA, CODE, DATA, Tape, average, get_json, summarize, write_csv

SSE = 'https://query.sse.com.cn/security/stock/queryCompanyBulletin.do'
SSE_HOME = 'https://www.sse.com.cn/'
ARCHIVE = DATA / f'{CODE}_earnings_announcements.csv'
HORIZONS = (3, 5, 10, 20)
FIELDS = ('id', 'kind', 'title', 'date', 'published_at', 'timing_basis', 'disclosure_url', 'document_basis',
          'timestamp_url', 'retrieved_at')


def classify(title):
    if re.search(r'摘要|英文|取消|说明会|提示性|董事会决议|审计报告|问询|致歉|编制|网上|快报报表', title):
        return None
    if '业绩快报' in title:
        return 'preliminary'
    if re.search(r'业绩(?:预告|预增|预减|预亏|预盈|扭亏|续亏|续盈)', title):
        return 'forecast'
    if re.search(r'\d{4}\s*年(?:年度报告|度报告|报告|年报)(?:全文)?$', title.strip()):
        return 'annual'


def fetch(tape):
    params = {'isPagination': 'true', 'productId': CODE,
              'securityType': '0101,120100,020100,020200,120200',
              'beginDate': tape.data['policy']['start'], 'endDate': tape.freeze,
              'pageHelp.pageNo': 1, 'pageHelp.pageSize': 25}

    def official(filter_values):
        query = {**params, **filter_values}
        first = get_json(SSE, query, referer=SSE_HOME)
        page = first['pageHelp']
        expected = int(page['total'])
        reports = {r['URL']: r for r in first['result']}
        for n in range(2, int(page['pageCount']) + 1):
            if len(reports) >= expected:
                break
            later = get_json(SSE, {**query, 'pageHelp.pageNo': n}, referer=SSE_HOME)
            reports.update((r['URL'], r) for r in later['result'])
        if len(reports) != expected or any(r['SECURITY_CODE'] != CODE for r in reports.values()):
            raise ValueError('上交所公告索引覆盖不完整或代码不符')
        return reports.values(), f'{SSE}?{urlencode(query, safe=",")}'

    with ThreadPoolExecutor(max_workers=2) as pool:
        annual, interim = pool.map(official, ({'reportType2': 'DQBG'}, {'keyWord': '业绩'}))
    candidates = [(r, 'annual', annual[1]) for r in annual[0] if r['BULLETIN_TYPE'] == '年报']
    candidates.extend((r, kind, interim[1]) for r in interim[0]
                      if (kind := classify(r['TITLE'])) in ('preliminary', 'forecast'))
    retrieved = datetime.now(CHINA).isoformat(timespec='seconds')
    rows, years = [], set()
    for row, kind, index in candidates:
        day, title = row['SSEDATE'], row['TITLE']
        if not tape.data['policy']['start'] <= day <= tape.freeze:
            continue
        stamp = datetime.fromisoformat(row['ADDDATE']).replace(tzinfo=CHINA) if row.get('ADDDATE') else None
        precise = stamp.isoformat() if stamp and stamp.strftime('%H:%M:%S') != '00:00:00' else ''
        source = urljoin(SSE_HOME, row['URL'])
        link = urlsplit(source)
        if link.netloc != 'www.sse.com.cn' or not link.path.startswith('/disclosure/listedinfo/announcement/'):
            raise ValueError(f'上交所公告原文链接异常：{source}')
        if kind == 'annual':
            year = re.search(r'(\d{4})\s*年', title)
            years.add(int(year.group(1)) if year else int(row['BULLETIN_YEAR']))
        rows.append(dict(id=link.path.rsplit('/', 1)[-1], kind=kind, title=title, date=day,
                         published_at=precise, timing_basis='sse_adddate' if precise else 'sse_date_only',
                         disclosure_url=source, document_basis='issuer_pdf', timestamp_url=index,
                         retrieved_at=retrieved))
    if not set(range(int(tape.days[0][:4]) - 1, int(tape.freeze[:4]))) <= years:
        raise ValueError(f'年度报告覆盖出现缺口，实得 {sorted(years)}')
    if len({r['id'] for r in rows}) != len(rows):
        raise ValueError('公告原文编号重复')
    return sorted(rows, key=lambda r: (r['published_at'] or r['date'], r['id']))


def statistics(trades, _):
    gross = [r['gross'] for r in trades]
    wins = average([v for v in gross if v > 0])
    losses = average([-v for v in gross if v < 0])
    return {'gross': average(gross), 'excess': average([r['excess'] for r in trades]),
            'win': average([v > 0 for v in gross]), 'payoff': wins / losses if wins and losses else None}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true', help='复用已落盘的公告证据')
    args = parser.parse_args()
    tape = Tape()
    if args.offline:
        with ARCHIVE.open(encoding='utf-8', newline='') as stream:
            rows = list(csv.DictReader(stream))
    else:
        rows = fetch(tape)
        write_csv(ARCHIVE, FIELDS, rows)
    events = []
    for row in rows:
        stamp = datetime.fromisoformat(row['published_at']) if row['published_at'] else None
        day = stamp.date().isoformat() if stamp else row['date']
        if not tape.data['policy']['start'] <= day <= tape.freeze:
            continue
        clock = stamp.strftime('%H:%M:%S') if stamp else None
        preopen = clock is not None and clock < '09:25:00'
        session = ('日期级' if clock is None else '非交易日' if day not in tape.positions else
                   '盘前' if preopen else '盘后' if clock >= '15:00:00' else '盘中 / 集合竞价已结束')
        i = tape.first_open(day, preopen)
        group = tape.group(i - 1) if i is not None else dict(signal_date=None, t1=None, warm=False)
        events.append({**row, 'session': session, 'entry': tape.days[i] if i is not None else None, **group,
                       'returns': {str(h): tape.returns(i, h) if i is not None else {'reason': '冻结日前未开市'}
                                   for h in HORIZONS}})
    tape.save('earnings_event_study', {
        'title': '财报发布事件驱动', 'horizons': HORIZONS, 'available': [tape.days[0], tape.freeze],
        'notice_count': len(events), 'timed_count': sum(bool(row['published_at']) for row in events),
        'document_count': sum(row.get('document_basis')=='issuer_pdf' for row in events),
        'summary': summarize(events, HORIZONS, statistics), 'events': events,
        'source_urls': [SSE],
        'method': '用上交所公开披露索引 ADDDATE 作为可核验时点，并保留官方索引与发行人原文。'
                  '09:25 前可于当天开盘入场，集合竞价结束后及日期级公告等到下一可交易日。T1 取入场前一交易日，预热前属于 T1=0。'
                  '持有期含入场日，绝对收益为因果复权开盘至收盘，超额扣除该段沪深 300 价格收益。'
                  '各组、各期限按入场先后保留不重叠样本。'})
    print(f'{CODE} 财报实证：{len(events)} 条，带公开时点 {sum(bool(r["published_at"]) for r in events)} 条')


if __name__ == '__main__':
    main()
