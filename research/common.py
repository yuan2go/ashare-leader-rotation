"""Shared, read-only access to the verified development tape."""
import csv
import hashlib
import io
import json
import os
import sys
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from tempfile import NamedTemporaryFile
from time import sleep
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine import scan
from inputs import load, prepare

CODE = '600428'
CHINA = timezone(timedelta(hours=8))
DATA = ROOT / 'data' / 'research'
OUTPUT = ROOT / 'results' / 'research' / f'{CODE}.json'
PLUGIN_INPUTS = ('calendar', 'index', 'stocks', 'actions')


def get_json(url, params, *, referer=None):
    encoded = urlencode(params, safe=',')
    req = Request(f'{url}?{encoded}',
                  headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json',
                           'Referer': referer or url})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=20) as response:
                return json.load(response)
        except (OSError, ValueError):
            if attempt == 2:
                raise
            sleep(1 + attempt)


def atomic_text(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False, mode='w', encoding='utf-8', newline='') as stream:
        temporary = Path(stream.name)
        stream.write(body)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_csv(path, fields, rows):
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fields, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(path, output.getvalue())


class Tape:
    def __init__(self):
        raw = load(ROOT)
        self.data = prepare(raw)
        if raw['policy']['status'] != 'approved' or self.data['errors']:
            raise SystemExit('专项研究需要已批准且通过审核的开发期行情')
        self.freeze = raw['policy']['freeze']
        self.days = self.data['days']
        self.positions = {day: i for i, day in enumerate(self.days)}
        self.quotes = self.data['stocks'][CODE]
        self.volumes = {r['date']: float(r['volume']) for r in raw['tables']['stocks']
                        if r['code'] == CODE and r['status'] == 'open' and r['date'] <= self.freeze}
        self.name = self.data['universe'][CODE]['name']
        self.open_days = [day for day in self.days if self.opened(day)]
        self.states = scan(self.data)['states']
        self.identity = {key: hashlib.sha256((ROOT / 'data' / f'{key}.csv').read_bytes()).hexdigest()
                         for key in PLUGIN_INPUTS}

    def opened(self, day):
        return self.quotes.get(day, {}).get('opened', False)

    def first_open(self, day, same_day):
        pos = (bisect_left if same_day else bisect_right)(self.open_days, day)
        return self.positions[self.open_days[pos]] if pos < len(self.open_days) else None

    def group(self, i):
        state = self.states[i].get(CODE, {}) if i >= 0 else {}
        return {'signal_date': self.days[i] if i >= 0 else None,
                't1': int(state.get('gate', False)), 'warm': state.get('warm', False)}

    def returns(self, entry, horizon):
        end = entry + horizon - 1
        if end >= len(self.days):
            return {'reason': '冻结日内不足持有期'}
        start_day, end_day = self.days[entry], self.days[end]
        if not self.opened(start_day) or not self.opened(end_day):
            return {'reason': '入场或到期日停牌'}
        opening = self.quotes[start_day]['open'] * self.states[entry][CODE]['factor']
        closing = self.quotes[end_day]['close'] * self.states[end][CODE]['factor']
        benchmark = self.data['benchmark']
        gross = closing / opening - 1
        market = benchmark[end_day][1] / benchmark[start_day][0] - 1
        return {'exit': end_day, 'gross': gross, 'market': market, 'excess': gross - market}

    def save(self, key, experiment):
        previous = json.loads(OUTPUT.read_text(encoding='utf-8')) if OUTPUT.exists() else {}
        report = {'schema_version': 1, 'code': CODE, 'name': self.name, 'asof': self.freeze,
                  'input_sha256': self.identity, 'experiments': {}}
        if all(previous.get(field) == report[field] for field in report if field != 'experiments'):
            report['experiments'].update(previous['experiments'])
        report['experiments'][key] = experiment
        atomic_text(OUTPUT, json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def average(values):
    return mean(values) if values else None


def summarize(events, horizons, compute):
    result = {}
    for label, group in (('all', None), ('up', 1), ('down', 0)):
        result[label] = {}
        for h in horizons:
            key = str(h)
            complete = [(event['entry'], event['returns'][key]) for event in events
                        if (group is None or event['t1'] == group) and 'gross' in event['returns'][key]]
            independent = []
            last = ''
            for entry, trade in sorted(complete, key=lambda row: (row[0], row[1]['exit'])):
                if entry > last:
                    last = trade['exit']
                    independent.append(trade)
            trades = [v for _, v in complete]
            result[label][key] = {'n': len(trades), 'independent': len(independent),
                                  **compute(trades, independent)}
    return result
