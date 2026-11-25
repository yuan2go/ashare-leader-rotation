"""Standard-library Tushare transport and market-data field mappings."""

from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from http.client import IncompleteRead, RemoteDisconnected
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DAILY_FIELDS = ('ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'pre_close', 'vol', 'amount')
SUSPEND_FIELDS = ('ts_code', 'trade_date', 'suspend_type', 'suspend_timing')
BASIC_FIELDS = ('ts_code', 'symbol', 'list_date', 'delist_date', 'list_status', 'market')
NAME_FIELDS = ('ts_code', 'name', 'start_date', 'end_date', 'ann_date')
DIVIDEND_FIELDS = ('ts_code', 'div_proc', 'ex_date', 'record_date', 'pay_date', 'imp_ann_date',
                   'div_listdate', 'cash_div_tax', 'stk_bo_rate', 'stk_co_rate')
CAP = 5000


class TushareError(ConnectionError):
    pass


def local_settings(root):
    settings = {}
    path = Path(root) / '.env'
    if path.is_file():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            match = re.fullmatch(r'\s*(?:export\s+)?(TUSHARE_HTTP_URL|TUSHARE_TOKEN)\s*=\s*(.*?)\s*', line)
            if match:
                key, value = match.groups()
                if value[:1] in ("'", '"') and value[-1:] == value[:1]:
                    value = value[1:-1]
                settings[key] = value
    return {k: os.environ.get(k, settings.get(k, '')) for k in ('TUSHARE_HTTP_URL', 'TUSHARE_TOKEN')}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def source_id(api, params):
    symbol = params.get('ts_code')
    suffix = f'?ts_code={symbol}' if isinstance(symbol, str) and re.fullmatch(r'\d{6}\.(?:SH|SZ)', symbol) else ''
    return f'tushare_pro:{api}{suffix}'


def ts_code(code):
    if not re.fullmatch(r'(?:60|00|30)\d{4}', code):
        raise ValueError('Tushare 仅接入支持整手契约的沪深 A 股')
    return code + ('.SH' if code.startswith('60') else '.SZ')


class TushareClient:
    def __init__(self, root, *, offline=False, transport=None, pause=sleep, request_interval=1.25):
        settings = local_settings(root)
        self._url, self._token = settings['TUSHARE_HTTP_URL'], settings['TUSHARE_TOKEN']
        self.cache = Path(root) / 'data' / 'fetch_cache' / 'tushare'
        self.offline = offline
        self.receipts = {}
        self._transport = transport or build_opener(NoRedirect()).open
        self._pause = pause
        self._interval = request_interval
        self._last_request = 0.0
        if self._url:
            try:
                parsed = urlsplit(self._url)
            except ValueError:
                raise TushareError('Tushare HTTPS 端点格式无效') from None
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or self._token and self._token in self._url):
                raise TushareError('Tushare HTTPS 端点格式无效，须使用无凭据和查询参数的端点')

    @property
    def configured(self):
        return bool(self._url and self._token)

    def _frame(self, api, params, fields, label):
        body = json.dumps(dict(api_name=api, token=self._token, params=params, fields=','.join(fields)),
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
        request = Request(self._url, data=body, method='POST',
                          headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
        reason = '响应无效'
        for attempt in range(6):
            throttled = False
            wait = self._interval - (monotonic()-self._last_request)
            if wait > 0:
                self._pause(wait)
            self._last_request = monotonic()
            try:
                with self._transport(request, timeout=20) as response:
                    raw = response.read()
                if self._token.encode('utf-8') in raw:
                    raise TushareError(f'{label} 响应含凭据，已拒收')
                payload = json.loads(raw, parse_float=Decimal)
                result_code = payload.get('code')
                if result_code == 429:
                    reason = '服务限流 429'
                    throttled = True
                    if attempt < 5:
                        self._pause(2**(attempt+1))
                    continue
                if result_code != 0:
                    code = result_code if type(result_code) is int else '未知'
                    raise TushareError(f'{label} 服务拒绝请求，代码 {code}')
                data = payload['data']
                returned = data['fields']
                values = data['items']
                if returned == [] and values == []:
                    returned = list(fields)
                if (not isinstance(returned, list) or not isinstance(values, list) or
                        set(returned) != set(fields) or len(returned) != len(fields) or
                        any(not isinstance(row, list) or len(row) != len(returned) or
                            any(isinstance(value, (dict, list)) for value in row) for row in values)):
                    raise TushareError(f'{label} 响应列与请求不一致')
                if any(isinstance(value, str) and self._token in value for row in values for value in row):
                    raise TushareError(f'{label} 响应含凭据，已拒收')
                return dict(source=label, retrieved=datetime.now(timezone.utc).isoformat(),
                            rows=[dict(zip(returned, (str(x) if isinstance(x, Decimal) else x for x in row)))
                                  for row in values])
            except HTTPError as exc:
                reason = f'HTTP {exc.code}'
                throttled = exc.code == 429
                if exc.code not in (408, 425, 429, 500, 502, 503, 504):
                    break
            except TushareError:
                raise
            except (URLError, TimeoutError, OSError, IncompleteRead, RemoteDisconnected):
                reason = '网络连接失败'
            except (ValueError, TypeError, KeyError, AttributeError):
                reason = '响应无法解析'
            if attempt >= (5 if throttled else 2):
                break
            self._pause(2**(attempt+int(throttled)))
        raise TushareError(f'{label} 请求失败：{reason}')

    def query(self, api, params, fields):
        if (not re.fullmatch(r'[a-z][a-z0-9_]*', api) or not fields or
                any(not re.fullmatch(r'[a-z][a-z0-9_]*', field) for field in fields)):
            raise ValueError('Tushare 接口名称或字段不合法')
        label = source_id(api, params)
        identity = json.dumps([self._url, api, params, list(fields)], sort_keys=True, separators=(',', ':'))
        path = self.cache / (sha256(identity.encode('utf-8')).hexdigest() + '.json')
        if path.is_file():
            raw = path.read_bytes()
            if self._token and self._token.encode('utf-8') in raw:
                raise TushareError(f'{label} 缓存含凭据，已拒收')
            frame = json.loads(raw)
        else:
            if self.offline or not self.configured:
                raise TushareError(f'{label} 缺本地缓存或有效配置')
            frame = self._frame(api, params, fields, label)
            raw = json.dumps(frame, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
            self.cache.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self.cache, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
            try:
                temporary.replace(path)
            finally:
                with suppress(FileNotFoundError):
                    temporary.unlink()
        if frame.get('source') != label:
            raise TushareError(f'{label} 缓存来源不一致')
        self.receipts[path.stem] = dict(source=label, retrieved=frame['retrieved'], cache_sha=sha256(raw).hexdigest())
        return frame['rows']

    def dated(self, api, params, fields, start, end):
        """Query whole windows first, splitting saturated responses into safe chunks."""
        lo, hi = date.fromisoformat(start), date.fromisoformat(end)
        if lo > hi:
            raise ValueError('Tushare 日期范围倒置')

        def fetch(left, right):
            rows = self.query(api, dict(params, start_date=left.strftime('%Y%m%d'),
                                        end_date=right.strftime('%Y%m%d')), fields)
            if len(rows) < CAP:
                return rows
            if left == right:
                raise TushareError(f'{source_id(api, params)} 单日触及返回行数上限')
            if left.year != right.year:
                stop = date(left.year, 12, 31)
            else:
                stop = left + (right - left) // 2
            return fetch(left, stop) + fetch(stop + timedelta(days=1), right)

        return fetch(lo, hi)

    def daily(self, code, start, end):
        return self.dated('daily', {'ts_code': ts_code(code)}, DAILY_FIELDS, start, end)

    def suspensions(self, code, start, end):
        return self.dated('suspend_d', {'ts_code': ts_code(code)}, SUSPEND_FIELDS, start, end)

    def instrument(self, code):
        symbol = ts_code(code)
        rows = self.query('stock_basic', {'ts_code': symbol}, BASIC_FIELDS)
        if len(rows) != 1 or rows[0]['ts_code'] != symbol or rows[0]['symbol'] != code:
            raise TushareError(f'{symbol} 上市资料缺失或代码不匹配')
        return rows[0]

    def names(self, code):
        symbol = ts_code(code)
        rows = self.query('namechange', {'ts_code': symbol}, NAME_FIELDS)
        if any(r['ts_code'] != symbol for r in rows):
            raise TushareError(f'{symbol} 名称历史代码不匹配')
        return rows

    def dividends(self, code, start, end):
        symbol = ts_code(code)
        rows = self.query('dividend', {'ts_code': symbol}, DIVIDEND_FIELDS)
        if len(rows) >= CAP:
            raise TushareError(f'{source_id("dividend", {"ts_code": symbol})} 权益历史触及返回行数上限')
        return implemented_dividends(rows, code, start, end)


def iso_date(value):
    if not value:
        return ''
    return date.fromisoformat(str(value)).isoformat()


def scaled(value, factor):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError('Tushare 成交量或成交额无效')
    return format(amount * factor, 'f')


def daily_bars(rows, symbol):
    quotes = {}
    for row in rows:
        day = iso_date(row['trade_date'])
        if row['ts_code'] != symbol or day in quotes:
            raise ValueError('Tushare 日线代码有误或日期重复')
        quote = {k: str(row[k]) for k in ('open', 'high', 'low', 'close', 'pre_close')}
        if not all(Decimal(v).is_finite() and Decimal(v) > 0 for v in quote.values()):
            raise ValueError('Tushare 日线价格无效')
        quotes[day] = dict(quote, volume=scaled(row['vol'], 100), amount=scaled(row['amount'], 1000))
    return quotes


def suspended_days(rows, symbol):
    result = set()
    for row in rows:
        if row['ts_code'] != symbol:
            raise ValueError('Tushare 停牌代码不匹配')
        timing = re.sub(r'\s+', '', str(row.get('suspend_timing') or ''))
        if row['suspend_type'] == 'S' and timing in ('', '全天', '09:30-15:00', '9:30-15:00'):
            result.add(iso_date(row['trade_date']))
    return result


def name_at(rows, day):
    matching = []
    for r in rows:
        start, end, announced = map(iso_date, (r['start_date'], r['end_date'], r['ann_date']))
        if start and start <= day and (not end or day <= end) and (not announced or announced <= day):
            matching.append((start, announced, r['name']))
    return max(matching)[2] if matching else None


def risk_flag(name):
    if not name:
        return ''
    return str(int(bool(re.match(r'^(?:\*?ST|S\*?ST|退)|.*退市', re.sub(r'\s+', '', name).upper()))))


def aligned_market(client, code, start, end, market_days):
    """Return source observations, leaving unexplained dates absent for the caller to audit."""
    symbol = ts_code(code)
    information = client.instrument(code)
    listing, delisting = map(iso_date, (information['list_date'], information['delist_date']))
    if not listing or information['list_status'] not in ('L', 'P', 'D'):
        raise TushareError(f'{symbol} 上市状态缺证')
    names = client.names(code)
    start = max(start, listing)
    bars = daily_bars(client.daily(code, start, end), symbol)
    suspensions = suspended_days(client.suspensions(code, start, end), symbol)
    expected = {day for day in market_days if start <= day <= end and (not delisting or day <= delisting)}
    if (bars.keys() | suspensions) - expected:
        raise TushareError(f'{symbol} 日线或停牌日超出上市市场日历')
    return dict(code=code, listing=listing, delisting=delisting, names=names,
                bars=bars, suspensions=suspensions, expected=expected,
                unknown_days=sorted(expected - bars.keys() - suspensions),
                conflicting_days=sorted(bars.keys() & suspensions))


def implemented_dividends(rows, code, start, end):
    symbol = ts_code(code)
    events = []
    for row in rows:
        if row['ts_code'] != symbol:
            raise ValueError('Tushare 权益代码不匹配')
        ex = iso_date(row['ex_date'])
        if row['div_proc'] != '实施' or not start <= ex <= end:
            continue
        cash = Decimal(scaled(row.get('cash_div_tax') or 0, 1))
        bonus = sum(Decimal(scaled(row.get(k) or 0, 1)) for k in ('stk_bo_rate', 'stk_co_rate'))
        if not cash and not bonus:
            continue
        events.append(dict(code=code, ex_date=ex, announce_date=iso_date(row['imp_ann_date']),
                           pay_date=iso_date(row['pay_date']) if cash else '',
                           share_date=iso_date(row['div_listdate']) if bonus else '',
                           cash_per_share=str(cash), bonus_per_share=str(bonus),
                           record_date=iso_date(row['record_date']), source=source_id('dividend', {'ts_code': symbol})))
    return events
