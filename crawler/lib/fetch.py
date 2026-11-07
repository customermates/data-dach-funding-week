"""Polite HTTP for the public data series (ops/data).

Every request goes through `Fetcher.get`:
- an honest, descriptive User-Agent naming Customermates and mail@customermates.com (SEC EDGAR requires one);
- robots.txt of the host is read once per run and honoured (a disallowed URL raises Disallowed, nothing is fetched);
- a minimum interval per host (default 1 s; SEC allows 10 requests/s, we use 4);
- retries with backoff on 429, 5xx and network errors, honouring Retry-After;
- an optional route per host through the home exit (SOCKS5 127.0.0.1:41090, docs/browser-egress.md in workspace-ops),
  for hosts that block the OVH IP (TED, Bundesagentur Jobsuche); that path uses curl --socks5-hostname.
No model, no secrets in URLs or logs.
"""
import gzip
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib

USER_AGENT = 'Customermates Data (public company-level datasets; mail@customermates.com)'
SEC_USER_AGENT = 'Customermates mail@customermates.com'  # SEC's documented form: "Company Name admin@domain"


class Disallowed(RuntimeError):
    """robots.txt disallows this URL for our user agent."""


class FetchFailed(RuntimeError):
    def __init__(self, url, status, message=''):
        super().__init__('GET %s failed: %s %s' % (url, status, message))
        self.url, self.status = url, status


def _decode(raw, encoding):
    """Decompresses per Content-Encoding; archives (Wayback id_) may send the header with an already plain body."""
    try:
        if encoding == 'gzip' and raw[:2] == b'\x1f\x8b':
            return gzip.decompress(raw)
        if encoding == 'deflate':
            return zlib.decompress(raw)
    except (OSError, zlib.error):
        pass
    return raw


class Fetcher:
    def __init__(self, user_agent=USER_AGENT, intervals=None, default_interval=1.0, proxies=None,
                 retries=4, timeout=60, opener=None, sleep=time.sleep, clock=time.monotonic, respect_robots=True):
        self.user_agent = user_agent
        self.intervals = intervals or {}
        self.default_interval = default_interval
        self.proxies = proxies or {}
        self.retries = retries
        self.timeout = timeout
        self._opener = opener
        self.sleep, self.clock = sleep, clock
        self.respect_robots = respect_robots
        self._last = {}
        self._robots = {}
        self.requests = 0

    # ---- robots.txt ----
    def _robots_for(self, url):
        parts = urllib.parse.urlsplit(url)
        origin = parts.scheme + '://' + parts.netloc
        if origin not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                status, body, _ = self._raw(origin + '/robots.txt', robots=True)
                if status >= 400 and status < 500:
                    rp.allow_all = True  # no robots.txt: everything allowed (RFC 9309)
                else:
                    rp.parse(body.decode('utf-8', 'replace').splitlines())
            except FetchFailed as error:
                if error.status in (401, 403):
                    rp.disallow_all = True
                elif isinstance(error.status, int) and 400 <= error.status < 500:
                    rp.allow_all = True
                else:
                    raise
            self._robots[origin] = rp
        return self._robots[origin]

    def allowed(self, url):
        if not self.respect_robots:
            return True
        return self._robots_for(url).can_fetch(self.user_agent, url)

    # ---- transport ----
    def _wait(self, host):
        gap = self.intervals.get(host, self.default_interval)
        last = self._last.get(host)
        if last is not None:
            delay = gap - (self.clock() - last)
            if delay > 0:
                self.sleep(delay)
        self._last[host] = self.clock()

    def _open(self, req, host):
        if self._opener:
            return self._opener(req, timeout=self.timeout)
        return urllib.request.urlopen(req, timeout=self.timeout)

    def _curl(self, url, hdrs, proxy):
        """GET through a SOCKS5 proxy with curl (urllib has no SOCKS); returns (status, body, headers)."""
        cmd = ['curl', '-sS', '--compressed', '-m', str(self.timeout), '--socks5-hostname', proxy.split('://', 1)[-1],
               '-o', '-', '-w', '\n%{http_code}']
        for key, value in hdrs.items():
            if key.lower() != 'accept-encoding':
                cmd += ['-H', '%s: %s' % (key, value)]
        out = subprocess.run(cmd + [url], capture_output=True, timeout=self.timeout + 15)
        if out.returncode != 0:
            raise urllib.error.URLError('curl exit %s' % out.returncode)
        body, _, code = out.stdout.rpartition(b'\n')
        status = int(code or 0)
        if status >= 400:
            raise urllib.error.HTTPError(url, status, 'HTTP %s' % status, {}, None)
        return status, body, {}

    def _raw(self, url, headers=None, robots=False):
        host = urllib.parse.urlsplit(url).netloc
        hdrs = {'User-Agent': self.user_agent, 'Accept-Encoding': 'gzip, deflate'}
        hdrs.update(headers or {})
        attempt = 0
        while True:
            attempt += 1
            self._wait(host)
            self.requests += 1
            try:
                if host in self.proxies and not self._opener:
                    return self._curl(url, hdrs, self.proxies[host])
                with self._open(urllib.request.Request(url, headers=hdrs), host) as res:
                    body = _decode(res.read(), res.headers.get('Content-Encoding'))
                    return res.status, body, dict(res.headers)
            except urllib.error.HTTPError as error:
                status = error.code
                retry_after = error.headers.get('Retry-After') if error.headers else None
                if robots and 400 <= status < 500:
                    raise FetchFailed(url, status) from None
                if status == 429 or status >= 500:
                    if attempt <= self.retries:
                        wait = float(retry_after) if (retry_after or '').isdigit() else min(300, 5 * 2 ** attempt)
                        self.sleep(wait)
                        continue
                raise FetchFailed(url, status) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                if attempt <= self.retries:
                    self.sleep(min(120, 3 * 2 ** attempt))
                    continue
                raise FetchFailed(url, 'network', str(getattr(error, 'reason', error))) from None

    def get(self, url, headers=None):
        if not self.allowed(url):
            raise Disallowed(url)
        status, body, hdrs = self._raw(url, headers)
        return body

    def get_text(self, url, headers=None):
        return self.get(url, headers).decode('utf-8', 'replace')

    def get_json(self, url, headers=None):
        return json.loads(self.get(url, headers))


HOME_EXIT = os.environ.get('CM_DATA_HOME_EXIT', 'socks5://127.0.0.1:41090')
