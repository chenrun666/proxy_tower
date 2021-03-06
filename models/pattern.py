import re
import sys
import json
import asyncio
import datetime
import traceback
from collections import OrderedDict

import aioredis
from lxml import etree
from pygtrie import CharTrie


class Checker(object):

    def __init__(self, global_blacklist=None):
        self.global_blacklist = global_blacklist or list()

    @staticmethod
    def _status_code_checker(status_code):
        return status_code is not None and (status_code == 404 or status_code < 400)

    @staticmethod
    def _xpath_checker(html, xpath, value):
        try:
            et = etree.HTML(html)
            assert et.xpath(xpath)[0] == value
        except IndexError:
            return 'xpath check failed, {} not found'.format(xpath)
        except AssertionError:
            return 'xpath check failed, value not equal'
        except Exception:
            return ''.join(traceback.format_exception(*sys.exc_info()))

    def check(self, status_code, text, rule, value):
        if not self._status_code_checker(status_code):
            return 'status_code check failed, get {}'.format(status_code)
        for word in self.global_blacklist:
            if word in text:
                return 'global blacklist check failed, get {}'.format(word)
        if rule == 'whitelist':
            if value not in text:
                return 'whitelist check failed, {} not found'.format(value)
        elif rule and value and len(rule.strip()) != 0 and len(value.strip()) != 0:
            return self._xpath_checker(text, rule, value)


class Pattern(object):

    def __init__(self, pattern_str, rule, value, checker, saver=None):
        self._pattern_str = pattern_str
        self.checker = checker
        self.saver = saver
        self.rule = rule
        self.value = value
        self.counter_lock = asyncio.Lock()
        self.success_counter = OrderedDict()
        self.fail_counter = OrderedDict()

    def __str__(self):
        return self._pattern_str

    @property
    def success_rate(self):
        x = list()
        y = list()
        for t in self.success_counter:
            if t in self.fail_counter:
                y.append((self.success_counter[t]/(self.fail_counter[t] + self.success_counter[t])) * 100)
            else:
                y.append(100)
            x.append(t)
        return [x, y]

    async def check(self, response):
        rule, value = self.rule, self.value
        text = await response.text()
        result = self.checker.check(response.status, text, rule, value)
        if result is None:
            return list()
        else:
            return [result]

    async def counter(self, now, valid):
        async with self.counter_lock:
            c = self.success_counter if valid else self.fail_counter
            if now in c:
                c[now] += 1
            else:
                c[now] = 1
            del_num = len(c) - 10
            if del_num > 0:
                for _ in range(del_num):
                    c.popitem(last=False)

    async def score_and_save(self, proxy, response):
        if self.saver is None:
            return
        await self.saver.save_result(str(self), str(proxy), response)

    def to_dict(self):
        return {
            'pattern': self._pattern_str,
            'rule': self.rule,
            'value': self.value
        }

    def dumps(self):
        return json.dumps(self.to_dict())


class PatternManager(object):

    def __init__(self, checker, saver, redis_addr='redis://localhost', password=None):
        self._redis_addr = redis_addr
        self._password = password
        self.checker = checker
        self.saver = saver
        self._patterns = dict()
        self.key = 'response_check_pattern'

    async def __aenter__(self):
        self.redis = await aioredis.create_redis_pool(self._redis_addr, password=self._password, encoding='utf8')
        self.t = await self._init_trie()
        self._patterns = {str(pattern): pattern for pattern in await self.patterns()}
        await self.add('public_proxies', None, None)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.redis is not None:
            self.redis.close()
            await self.redis.wait_closed()

    async def patterns(self, format_type='raw'):
        d = await self.redis.hgetall(self.key)
        patterns = [json.loads(v) for v in d.values()]
        if format_type == 'raw':
            patterns = [Pattern(pattern['pattern'],
                                pattern['rule'],
                                pattern['value'],
                                self.checker,
                                self.saver
                                ) for pattern in patterns]
        return patterns

    def get_pattern(self, pattern_str):
        if pattern_str in self._patterns:
            return self._patterns[pattern_str]

    def status(self):
        items = list()
        now = datetime.datetime.now()
        x = [(now-datetime.timedelta(minutes=i)).strftime("%H:%M") for i in range(9, -1, -1)]
        patterns = self._patterns.values()
        for pattern in patterns:
            times, values = pattern.success_rate
            y = [0] * 10
            for i, t in enumerate(times):
                try:
                    ind = x.index(t)
                except ValueError:
                    pass
                else:
                    y[ind] = values[i]
            items.append({'pattern': str(pattern), 'serial': y})
        return x, items

    async def _init_trie(self):
        d = await self.redis.hgetall(self.key)
        return CheckPatternTrie(d)

    async def restore_trie(self, t):
        await self.redis.hmset(self.key, t)

    async def add(self, pattern, rule, value):
        p = Pattern(str(pattern), rule, value, self.checker, self.saver)
        self._patterns[str(pattern)] = p
        self.t[str(pattern)] = p.dumps()
        await self.redis.hset(self.key, str(pattern), p.dumps())

    async def delete(self, pattern):
        del self.t[str(pattern)]
        del self._patterns[str(pattern)]
        await self.redis.hdel(self.key, str(pattern))

    async def update(self, pattern, rule, value):
        await self.add(pattern, rule, value)

    async def get_cookies(self, pattern_str):
        return await self.redis.srandmember(pattern_str + '_cookies')


class CheckPatternTrie(CharTrie):

    def __init__(self, *args, **kwargs):
        super(CheckPatternTrie, self).__init__(*args, **kwargs)

    def __setitem__(self, key, value):
        super(CheckPatternTrie, self).__setitem__(self._remove_http_prefix(key), value)

    def closest_pattern(self, url):
        url = self._remove_http_prefix(url)
        step = self.longest_prefix(url)
        pattern_str, check_rule_json = step.key, step.value
        if pattern_str is None:
            pattern_str, check_rule_json = 'public_proxies', json.dumps({'rule': None, 'value': None})
        return pattern_str, check_rule_json

    @staticmethod
    def _remove_http_prefix(url):
        if url.startswith('http'):
            url = re.sub(r'https?://', '', url, 1)
        return url
