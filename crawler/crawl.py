#!/usr/bin/env python3
"""DACH Funding Week: who in Germany, Austria and Switzerland raised money this week, how much, and from which funds.

Sources (robots.txt honoured; we store facts and the link, never article text):
- Tech.eu: news sitemap and dated post sitemaps (its /feed/ is disallowed for crawlers); for funding headlines the
  article's meta description and opening paragraphs are read for extraction (not stored);
- Gründerszene (Business Insider Deutschland): RSS;
- presseportal.de: topic RSS feeds (Wirtschaft, Finanzen, Netzwelt).
Skipped on purpose: deutsche-startups.de (bars ClaudeBot, reserves text-and-data-mining rights) and EU-Startups
(robots.txt and feeds sit behind a Cloudflare challenge, so its crawling rules cannot be checked).

Steps: fetch and deduplicate items (script) -> keyword prefilter for funding news (script) -> a model extracts the round
(Claude Haiku, no tools) -> the script checks company, amount and investors against the item text, keeps DACH rounds,
drops individual people as investors, merges the same round from several sources, converts to EUR (ECB rate) and
aggregates per ISO week. Company-level facts only: no founder or partner names.

Outputs to the open repo customermates/data-dach-funding-week and the "Funding rounds" list of the Customermates Data
workspace: rounds.csv, weeks.csv, investors.csv.

  python3 -I ops/data/dach-funding/crawl.py [--backfill-days 35] [--no-crm] [--no-publish]
"""
import argparse
import datetime
import email.utils
import hashlib
import html
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'lib'))
import fetch  # noqa: E402
import publish  # noqa: E402

SERIES = 'dach-funding'
REPO = 'data-dach-funding-week'
MODEL = os.environ.get('CM_DATA_EXTRACT_MODEL', 'claude-haiku-5-5')
FX_URL = 'https://api.frankfurter.app/{}?from=EUR'
RSS = {
    'Gründerszene': 'https://www.businessinsider.de/gruenderszene/feed/',
    'presseportal Wirtschaft': 'https://www.presseportal.de/rss/wirtschaft.rss2',
    'presseportal Finanzen': 'https://www.presseportal.de/rss/finanzen.rss2',
    'presseportal Netzwelt': 'https://www.presseportal.de/rss/netzwelt.rss2',
}
TECHEU_NEWS = 'https://tech.eu/sitemap/news.xml'
TECHEU_INDEX = 'https://tech.eu/sitemap/index.xml'
FUNDING = re.compile(
    r'(?i)\b(rais(?:es|ed|ing)|funding|financing|investment round|series [a-h]\b|seed|pre-seed|secures?|secured|lands?|'
    r'bags?|closes?|backs?|backed|led by|growth round|venture debt|valuation|'
    r'finanzierung\w*|finanzierungsrunde|investitionsrunde|sammelt\w*|erhält|erhalten|holt|eingesammelt|millionen|'
    r'kapitalspritze|investor\w*|wachstumskapital|bewertung)\b|[€$£]\s?\d|\d\s?(?:m|mn|million|millionen|mio\.?)\b')
FIRM_WORDS = re.compile(r'(?i)\b(capital|ventures?|partners?|vc|fund|fonds|invest\w*|holding|group|gmbh|ag|se|bank|'
                        r'labs?|angels?|equity|growth|asset|management|foundation|stiftung|corporate|global|tech|'
                        r'kfw|eif|eic|htgf|accelerator|incubator|studio|family office)\b')
PERSON = re.compile(r"^[A-ZÄÖÜ][a-zäöüß'\-]+(?:\s+(?:von|van|de|der|zu))?\s+[A-ZÄÖÜ][a-zäöüß'\-]+$")
ROUNDS = ['Pre-seed', 'Seed', 'Series A', 'Series B', 'Series C', 'Series D', 'Series E+', 'Growth', 'Debt', 'Grant',
          'Undisclosed']

PROMPT = """You read one startup or tech news item (title and summary) and decide whether it announces a new funding
round of a company. Answer with one JSON object only, no prose, no code fence:
{"is_funding_round": true|false,
 "company": "<company name as written>" or null,
 "country": "<ISO country code of the company's headquarters, e.g. DE, AT, CH, FR>" or null,
 "city": "<headquarters city as written>" or null,
 "round": one of %s or null,
 "amount": <number as written, e.g. 12 or 12.5 or 580> or null,
 "amount_unit": "million" | "billion" | "thousand" | "unit" | null,
 "currency": "EUR" | "USD" | "GBP" | "CHF" | null,
 "investors": ["<investment firm or institution as written>", ...],
 "sector": "<sector in 1-3 words>" or null,
 "ai": true|false, "ai_agents": true|false}
Rules: only a round the company just raised (not a fund raising money from LPs, not an acquisition, not an IPO, not a
roundup of several deals; for a roundup return is_funding_round false). Investors: firms, funds, banks and public
bodies only; never individual people (no business angels or partners by name). Copy names and numbers exactly as
written; never guess. ai is true if the company builds AI products; ai_agents if it builds AI agents.

Title: {title}
Summary: {summary}
Source: {source}"""


def strip(text):
    """Plain text from HTML that may itself be entity-escaped (RSS descriptions)."""
    text = html.unescape(text or '')
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', text))).strip()


def parse_rss(xml, source):
    items = []
    for block in re.findall(r'<item\b.*?</item>', xml, re.S | re.I):
        def tag(name):
            m = re.search(r'<%s[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</%s>' % (name, name), block, re.S | re.I)
            return m.group(1).strip() if m else ''
        link = strip(tag('link'))
        date = tag('pubDate')
        try:
            day = email.utils.parsedate_to_datetime(date).date().isoformat()
        except (TypeError, ValueError):
            day = None
        if link:
            items.append({'source': source, 'url': link, 'title': strip(tag('title')), 'summary': strip(tag('description'))[:1200],
                          'date': day})
    return items


def parse_news_sitemap(xml):
    items = []
    for block in re.findall(r'<url>.*?</url>', xml, re.S):
        loc = re.search(r'<loc>\s*(.*?)\s*</loc>', block, re.S)
        title = re.search(r'<n:title>(.*?)</n:title>', block, re.S)
        date = re.search(r'<n:publication_date>(.*?)</n:publication_date>', block, re.S)
        if loc:
            items.append({'source': 'Tech.eu', 'url': loc.group(1).strip(), 'title': strip(title.group(1) if title else ''),
                          'summary': '', 'date': date.group(1)[:10] if date else None})
    return items


def slug_items(xml, since):
    """Dated Tech.eu post URLs (/YYYY/MM/DD/slug/) from a post sitemap, on or after since; title from the slug."""
    out = []
    for loc in re.findall(r'<loc>\s*(https://tech\.eu/(\d{4})/(\d{2})/(\d{2})/([^<]+?))\s*</loc>', xml):
        url, y, m, d, slug = loc
        day = '%s-%s-%s' % (y, m, d)
        if day >= since:
            out.append({'source': 'Tech.eu', 'url': url, 'title': slug.strip('/').replace('-', ' '), 'summary': '', 'date': day})
    return out


def is_candidate(item):
    text = item['title'] + ' ' + item['summary']
    return bool(FUNDING.search(text))


def item_key(url):
    return hashlib.sha256(url.rstrip('/').lower().encode()).hexdigest()[:16]


def parse_json(text):
    text = re.sub(r'^```(?:json)?|```$', '', text.strip(), flags=re.M).strip()
    return json.loads(text[text.find('{'):text.rfind('}') + 1])


def run_model(prompt, runner=None):
    cmd = ['workspace', 'claude', 'auto', '-p', '--model', MODEL, '--tools', '', '--strict-mcp-config',
           '--mcp-config', '{"mcpServers":{}}', '--setting-sources', '', '--no-session-persistence',
           '--system-prompt', 'You are a precise data extraction function. Output JSON only.', '--output-format', 'text']
    run = runner or (lambda c, p: subprocess.run(c, input=p.encode(), capture_output=True, timeout=300, cwd='/tmp'))
    out = run(cmd, prompt)
    if out.returncode != 0:
        raise RuntimeError('extraction failed: ' + (out.stderr or out.stdout).decode(errors='replace')[-200:])
    return parse_json(out.stdout.decode(errors='replace'))


def norm(text):
    return re.sub(r'[^a-z0-9äöüß]+', ' ', (text or '').lower()).strip()


def appears(name, text):
    n = norm(name)
    return bool(n) and n in norm(text)


def amount_appears(amount, text):
    if amount is None:
        return True
    nums = set()
    for m in re.finditer(r'\d[\d.,]*', text):
        t = m.group(0).rstrip('.,')
        for c in (t.replace(',', ''), t.replace('.', '').replace(',', '.'), t.replace(',', '.')):
            try:
                nums.add(round(float(c), 3))
            except ValueError:
                pass
    return round(float(amount), 3) in nums


def check(ext, item):
    """Script checks of a model extraction against the item text. Returns a round dict or None (with reason)."""
    text = item['title'] + ' ' + item['summary'] + ' ' + item['url'].replace('-', ' ')
    if not ext.get('is_funding_round'):
        return None, 'not a round'
    if not ext.get('company') or not appears(ext['company'], text):
        return None, 'company not in text'
    if ext.get('country') not in ('DE', 'AT', 'CH', 'LI'):
        return None, 'not DACH'
    if not amount_appears(ext.get('amount'), text):
        return None, 'amount not in text'
    investors = []
    for inv in ext.get('investors') or []:
        inv = (inv or '').strip()
        if not inv or not appears(inv, text):
            continue
        if PERSON.match(inv) and not FIRM_WORDS.search(inv):
            continue  # an individual's name: never stored
        investors.append(inv)
    unit = {'billion': 1e9, 'million': 1e6, 'thousand': 1e3, 'unit': 1}.get(ext.get('amount_unit') or 'million', 1e6)
    amount = float(ext['amount']) * unit if ext.get('amount') is not None else None
    rnd = ext.get('round') if ext.get('round') in ROUNDS else 'Undisclosed'
    return {'company': ext['company'].strip(), 'country': ext['country'], 'city': ext.get('city'), 'round': rnd,
            'amount': amount, 'currency': ext.get('currency') if amount else None, 'investors': investors,
            'sector': ext.get('sector'), 'ai': bool(ext.get('ai')), 'ai_agents': bool(ext.get('ai_agents')),
            'announced': item['date'], 'source_urls': [item['url']], 'sources': [item['source']]}, None


def merge_rounds(rounds):
    """Same company and round within 21 days -> one row with all source links (first announcement date kept)."""
    merged = []
    for r in sorted(rounds, key=lambda r: r['announced'] or ''):
        for m in merged:
            same = norm(m['company']) == norm(r['company']) and (m['round'] == r['round'] or 'Undisclosed' in (m['round'], r['round']))
            if same and abs((datetime.date.fromisoformat(m['announced']) - datetime.date.fromisoformat(r['announced'])).days) <= 21:
                for key in ('source_urls', 'sources', 'investors'):
                    m[key] += [x for x in r[key] if x not in m[key]]
                for key in ('amount', 'currency', 'city', 'sector'):
                    m[key] = m[key] or r[key]
                if m['round'] == 'Undisclosed':
                    m['round'] = r['round']
                m['ai'] = m['ai'] or r['ai']
                m['ai_agents'] = m['ai_agents'] or r['ai_agents']
                break
        else:
            merged.append(json.loads(json.dumps(r)))
    return merged


def week_start(day):
    d = datetime.date.fromisoformat(day)
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


def to_eur(amount, currency, rates):
    if amount is None:
        return None
    if currency in (None, 'EUR'):
        return round(amount) if currency == 'EUR' else None
    rate = rates.get(currency)
    return round(amount / rate) if rate else None


def round_key(r):
    return 'funding:%s:%s:%s' % (re.sub(r'[^a-z0-9]+', '-', r['company'].lower()).strip('-'), r['round'].lower().replace(' ', '-'),
                                 r['announced'][:7])


def weeks(rows):
    out = {}
    for r in rows:
        w = out.setdefault(r['week_start'], {'week_start': r['week_start'], 'rounds': 0, 'disclosed_rounds': 0, 'total_eur': 0,
                                             'ai_rounds': 0, 'ai_agent_rounds': 0, 'investors': {}})
        w['rounds'] += 1
        if r['amount_eur']:
            w['disclosed_rounds'] += 1
            w['total_eur'] += r['amount_eur']
        w['ai_rounds'] += int(r['ai'])
        w['ai_agent_rounds'] += int(r['ai_agents'])
        for inv in r['investors'].split('; ') if r['investors'] else []:
            w['investors'][inv] = w['investors'].get(inv, 0) + 1
    res = []
    for w in sorted(out.values(), key=lambda w: w['week_start']):
        top = sorted(w.pop('investors').items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        w['median_round_eur'] = None
        amounts = sorted(r['amount_eur'] for r in rows if r['week_start'] == w['week_start'] and r['amount_eur'])
        if amounts:
            w['median_round_eur'] = amounts[len(amounts) // 2] if len(amounts) % 2 else round((amounts[len(amounts) // 2 - 1] + amounts[len(amounts) // 2]) / 2)
        w['top_investors'] = '; '.join('%s (%d)' % kv for kv in top)
        res.append(w)
    return res


def investors(rows, year):
    agg = {}
    for r in rows:
        if not r['announced'].startswith(year):
            continue
        for inv in r['investors'].split('; ') if r['investors'] else []:
            a = agg.setdefault(inv, {'investor': inv, 'rounds': 0, 'ai_rounds': 0, 'disclosed_eur': 0, 'companies': []})
            a['rounds'] += 1
            a['ai_rounds'] += int(r['ai'])
            a['disclosed_eur'] += r['amount_eur'] or 0
            a['companies'].append(r['company'])
    out = sorted(agg.values(), key=lambda a: (-a['rounds'], -a['disclosed_eur'], a['investor']))
    for a in out:
        a['companies'] = '; '.join(a['companies'])
    return out


CRM_SPEC = {
    'label': 'Funding round', 'plural': 'Funding rounds', 'icon': 'banknote', 'key': 'Source key',
    'description': 'DACH Funding Week data series: funding rounds of companies in Germany, Austria and Switzerland, '
                   'from Tech.eu, Gründerszene and presseportal headlines and summaries. Company-level facts with source '
                   'links; extraction by model with script checks (ops/data/dach-funding). Investors are firms only. '
                   'Public dataset: github.com/customermates/data-dach-funding-week.',
    'fields': [
        {'label': 'Company', 'type': 'text'},
        {'label': 'Country', 'type': 'select', 'options': ['DE', 'AT', 'CH', 'LI']},
        {'label': 'City', 'type': 'text'},
        {'label': 'Round', 'type': 'select', 'options': ROUNDS},
        {'label': 'Amount', 'type': 'number', 'decimals': 0},
        {'label': 'Currency', 'type': 'select', 'options': ['EUR', 'USD', 'GBP', 'CHF']},
        {'label': 'Amount (EUR)', 'type': 'currency', 'currency': 'EUR', 'decimals': 0},
        {'label': 'Investors', 'type': 'text'},
        {'label': 'Sector', 'type': 'text'},
        {'label': 'AI', 'type': 'boolean'},
        {'label': 'AI agents', 'type': 'boolean'},
        {'label': 'Announced', 'type': 'date'},
        {'label': 'Week', 'type': 'date'},
        {'label': 'Source', 'type': 'url'},
        {'label': 'Source key', 'type': 'text'},
    ],
}


def crm_rows(rows):
    return [{'title': '%s %s' % (r['company'], r['round']), 'Company': r['company'], 'Country': r['country'], 'City': r['city'],
             'Round': r['round'], 'Amount': r['amount'], 'Currency': r['currency'], 'Amount (EUR)': r['amount_eur'],
             'Investors': r['investors'], 'Sector': r['sector'], 'AI': r['ai'], 'AI agents': r['ai_agents'],
             'Announced': r['announced'], 'Week': r['week_start'], 'Source': r['source_urls'].split(' ')[0],
             'Source key': r['source_key']} for r in rows]


README = """# DACH Funding Week

Who in Germany, Austria and Switzerland raised money, how much, and which funds backed them. One row per funding
round, aggregated per ISO week (Monday to Sunday). Updated daily by a script; maintained by
[Customermates](https://customermates.com).

- `rounds.csv`: company, country, city, round, amount (as announced and in EUR), investor firms, sector, AI and
  AI-agent flags, announcement date and source links.
- `weeks.csv`: rounds, disclosed total and median in EUR, AI share and the most active investors per week.
- `investors.csv`: investor firms by number of rounds in the current year.

## Method and caveats

- Sources: [Tech.eu](https://tech.eu), [Gründerszene](https://www.businessinsider.de/gruenderszene/) and
  [presseportal.de](https://www.presseportal.de). Only facts and links are stored, never article text.
- A keyword filter picks funding news; a model (Claude Haiku) extracts the round; a script checks that the company,
  amount and investors appear in the source text and keeps companies headquartered in DE, AT, CH or LI.
- The same round reported by several sources within 21 days is one row. Investors are firms and public bodies only;
  individual people are never stored. "AI" and "AI agents" are our own classification.
- Coverage is what these sources report: not every round is public, and amounts are as announced. EUR values use the
  ECB reference rate of the announcement day (frankfurter.app).

Licence of this dataset: CC BY 4.0, credit "Customermates Data".
"""


class Crawler:
    def __init__(self, f, state, model=run_model):
        self.f, self.state, self.model = f, state, model
        self.extractions = 0
        self.errors = []

    def collect(self, backfill_days):
        items = []
        for name, url in RSS.items():
            items += parse_rss(self.f.get_text(url), name)
        items += parse_news_sitemap(self.f.get_text(TECHEU_NEWS))
        if backfill_days:
            since = (datetime.date.today() - datetime.timedelta(days=backfill_days)).isoformat()
            index = re.findall(r'<loc>\s*(https://tech\.eu/sitemap/posts/\d+\.xml)\s*</loc>', self.f.get_text(TECHEU_INDEX))
            for sm in sorted(index, key=lambda u: int(re.search(r'(\d+)\.xml', u).group(1)))[-2:]:  # newest post sitemaps
                items += slug_items(self.f.get_text(sm), since)
        return items

    def enrich(self, item):
        """Tech.eu items have no summary: read the article's own meta title and description."""
        if item['source'] != 'Tech.eu' or item['summary']:
            return item
        page = self.f.get_text(item['url'])
        for prop in ('og:title',):
            m = re.search(r'<meta property="%s" content="([^"]*)"' % prop, page)
            if m:
                item['title'] = strip(m.group(1))
        m = re.search(r'<meta name="description" content="([^"]*)"', page)
        summary = strip(m.group(1)) if m else ''
        # the opening paragraphs usually name the headquarters ("Vienna-based ..."); read for extraction, never stored
        body = re.search(r'<article\b.*?</article>', page, re.S | re.I)
        paras = [strip(p) for p in re.findall(r'<p\b[^>]*>(.*?)</p>', body.group(0) if body else page, re.S | re.I)]
        lead = ' '.join(p for p in paras if len(p) > 60)[:700]
        item['summary'] = (summary + ' ' + lead).strip()[:1200]
        return item

    def process(self, items):
        seen = self.state['items']
        for item in items:
            key = item_key(item['url'])
            if key in seen or not item.get('date'):
                continue
            if not is_candidate(item):
                seen[key] = {'url': item['url'], 'status': 'no funding keywords', 'date': item['date']}
                continue
            try:
                item = self.enrich(item)
            except (fetch.FetchFailed, fetch.Disallowed) as error:
                seen[key] = {'url': item['url'], 'status': 'unreadable: %s' % error, 'date': item['date']}
                continue
            prompt = PROMPT.replace('%s', json.dumps(ROUNDS)).replace('{title}', item['title']).replace(
                '{summary}', item['summary'] or '-').replace('{source}', item['source'])
            try:
                ext = self.model(prompt)
            except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                self.errors.append('%s: %s' % (item['url'], str(error)[:120]))  # not marked seen: retried next run
                continue
            self.extractions += 1
            rnd, reason = check(ext, item)
            seen[key] = {'url': item['url'], 'status': 'round' if rnd else reason, 'date': item['date'], 'round': rnd}


def build(state, rates_for):
    rounds = [v['round'] for v in state['items'].values() if v.get('round')]
    rows = []
    for r in merge_rounds(rounds):
        rates = rates_for(r['announced'])
        rows.append(dict(r, amount_eur=to_eur(r['amount'], r['currency'], rates), week_start=week_start(r['announced']),
                         investors='; '.join(r['investors']), source_urls=' '.join(r['source_urls']),
                         sources='; '.join(r['sources']), source_key=round_key(r)))
    rows.sort(key=lambda r: (r['announced'], r['company']))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--backfill-days', type=int, default=0)
    ap.add_argument('--no-crm', action='store_true')
    ap.add_argument('--no-publish', action='store_true')
    args = ap.parse_args(argv)
    sdir = publish.state_dir(SERIES)
    with publish.RunLock(SERIES):
        state = publish.load_json(sdir / 'state.json', {'items': {}, 'fx': {}})
        if not state['items'] and not args.backfill_days:
            args.backfill_days = 35  # first run: five weeks of Tech.eu history
        f = fetch.Fetcher(default_interval=2.0, retries=2)
        crawler = Crawler(f, state)
        errors = []
        try:
            items = crawler.collect(args.backfill_days)
        except (fetch.FetchFailed, fetch.Disallowed) as error:
            items, errors = [], ['collect: %s' % error]
        crawler.process(items)
        errors += crawler.errors[:10]
        publish.save_json(sdir / 'state.json', state)
        fx = fetch.Fetcher(respect_robots=False)

        def rates_for(day):
            if day not in state['fx']:
                try:
                    state['fx'][day] = fx.get_json(FX_URL.format(day)).get('rates', {})
                except (fetch.FetchFailed, ValueError):
                    return {}
            return state['fx'][day]
        rows = build(state, rates_for)
        week_rows = weeks(rows)
        year = datetime.date.today().isoformat()[:4]
        files = {
            'rounds.csv': publish.csv_text(rows, ['announced', 'week_start', 'company', 'country', 'city', 'round', 'amount', 'currency',
                                                  'amount_eur', 'investors', 'sector', 'ai', 'ai_agents', 'sources', 'source_urls']),
            'weeks.csv': publish.csv_text(week_rows, ['week_start', 'rounds', 'disclosed_rounds', 'total_eur', 'median_round_eur',
                                                      'ai_rounds', 'ai_agent_rounds', 'top_investors']),
            'investors.csv': publish.csv_text(investors(rows, year), ['investor', 'rounds', 'ai_rounds', 'disclosed_eur', 'companies']),
            'README.md': README,
        }
        for name, text in files.items():
            (sdir / name).write_text(text)
        files.update(publish.code_files(HERE, ['crawl.py']), LICENSE=publish.LICENSE_NOTE)
        result = {'series': SERIES, 'items_seen': len(state['items']), 'new_items': len(items), 'extractions': crawler.extractions,
                  'rounds': len(rows), 'weeks': len(week_rows), 'requests': f.requests, 'errors': errors}
        if not args.no_publish:
            result['repo'] = publish.publish_repo(REPO, files, 'data: %s, %d rounds' % (publish.today(), len(rows)))
        if not args.no_crm:
            import cm_sink
            if cm_sink.available():
                try:
                    result['crm'] = cm_sink.Sink().upsert(CRM_SPEC, crm_rows(rows))
                except Exception as error:  # noqa: BLE001 - reported; the CSV run still counts
                    result['crm_error'] = str(error)[:300]
            else:
                result['crm'] = 'no own MCP login for the Data workspace yet'
        this_week = week_start(datetime.date.today().isoformat())
        closed = [w for w in week_rows if w['week_start'] < this_week]
        if closed and state.get('woken_week') != closed[-1]['week_start'] and datetime.date.today().weekday() == 0:
            w = closed[-1]
            state['woken_week'] = w['week_start']
            result['wake'] = publish.wake_content(state, {
                'series': 'DACH Funding Week', 'week_start': w['week_start'], 'rounds': w['rounds'], 'total_eur': w['total_eur'],
                'ai_rounds': w['ai_rounds'], 'top_investors': w['top_investors'],
                'list': 'Funding rounds (Customermates Data)', 'repo': 'https://github.com/customermates/' + REPO})
        state['last_run'] = result
        publish.save_json(sdir / 'state.json', state)
        print(json.dumps(result))
        return 1 if errors and not rows else 0


if __name__ == '__main__':
    sys.exit(main())
