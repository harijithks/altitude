#!/usr/bin/env python3
"""Altitude job watcher.

For every application in the JOBS_JSON secret it:
  1. Reads the official notice page and looks for new keywords (admit card, exam date,
     next stage, interview, result, deadline).
  2. Runs a web search for the application and reports new results.
It sends a phone push through ntfy.sh (topic kept in the NTFY_TOPIC secret).
The first run only records a baseline, so you are not flooded with old items.
"""
import os, re, json, datetime, hashlib, urllib.request, urllib.parse
from urllib.parse import urljoin, urlparse
from html.parser import HTMLParser

JOBS = json.loads(os.environ.get('JOBS_JSON') or '[]')
NTFY = (os.environ.get('NTFY_TOPIC') or '').strip()
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state.json')
UA = 'Mozilla/5.0 (compatible; AltitudeWatch/1.0)'
OPEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'openings.json')
OPEN_SOURCES = [
    'https://ssc.gov.in/',
    'https://www.rrbcdg.gov.in/',
    'https://www.upsc.gov.in/whats-new',
    'https://www.tnpsc.gov.in/',
    'https://www.keralapsc.gov.in/',
]
DISCOVERY_QUERIES = [
    'civil engineering recruitment 2026 apply online',
    'assistant engineer civil notification 2026 last date',
    'junior engineer civil vacancy 2026 apply',
    'civil engineering government jobs 2026 apply now',
]
JOB_WORDS = ['recruit', 'notification', 'apply', 'vacanc', 'advertisement', 'engineer']
MAX_ALERTS_PER_RUN = 8

PATTERNS = {
    'Admit card released': [r'admit card', r'hall ticket', r'call letter', r'e-?admit'],
    'Exam date announced': [r'exam(ination)? (date|schedule)', r'date of (the )?exam', r'exam city'],
    'Next stage': [r'second stage', r'stage[- ]?(2|ii)\b', r'phase[- ]?(2|ii)\b'],
    'Interview or document verification': [r'interview', r'document verification', r'personality test'],
    'Result or answer key': [r'\bresult', r'shortlist', r'merit list', r'answer key', r'scorecard'],
    'Deadline or correction window': [r'last date', r'correction window', r'deadline'],
}


class Visible(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript') and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def get(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode('utf-8', 'ignore')


def visible_text(html):
    p = Visible()
    p.feed(html)
    return re.sub(r'\s+', ' ', ' '.join(p.parts)).strip()


def labels_in(text):
    low = text.lower()
    return sorted(label for label, pats in PATTERNS.items() if any(re.search(p, low) for p in pats))


def web_search(query):
    try:
        html = get('https://html.duckduckgo.com/html/?q=' + urllib.parse.quote(query))
    except Exception as e:
        print('search failed:', query, e)
        return []
    out = []
    for href, title in re.findall(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)[:8]:
        m = re.search(r'uddg=([^&"]+)', href)
        url = urllib.parse.unquote(m.group(1)) if m else href
        out.append((url, re.sub(r'<[^>]+>', '', title).strip()))
    return out


def notify(title, body, click=''):
    print('NOTIFY', title, '|', body.replace('\n', ' / '))
    if not NTFY:
        return
    headers = {'Title': title.encode('ascii', 'ignore').decode(), 'Priority': 'high', 'Tags': 'bell'}
    if click:
        headers['Click'] = click
    try:
        req = urllib.request.Request('https://ntfy.sh/' + NTFY, data=body.encode('utf-8'), headers=headers, method='POST')
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:
        print('push failed:', e)


def is_civil_opening(text):
    low = text.lower()
    return 'civil' in low and any(w in low for w in JOB_WORDS)


def discover():
    found = []
    for src in OPEN_SOURCES:
        try:
            html = get(src)
        except Exception as e:
            print('source failed:', src, e)
            continue
        for href, txt in re.findall(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
            title = re.sub(r'<[^>]+>|\s+', ' ', txt).strip()
            if title and is_civil_opening(title):
                found.append({'title': title[:160], 'url': urljoin(src, href), 'source': urlparse(src).netloc})
    for q in DISCOVERY_QUERIES:
        for url, title in web_search(q):
            if is_civil_opening(title):
                found.append({'title': title[:160], 'url': url, 'source': urlparse(url).netloc})
    return found


def run_discovery(state):
    seen = state.setdefault('_openings_seen', {})
    listing = json.load(open(OPEN_FILE)) if os.path.exists(OPEN_FILE) else []
    new = []
    for item in discover():
        h = hashlib.sha1(item['url'].encode()).hexdigest()[:12]
        if h in seen:
            continue
        seen[h] = item['title'][:80]
        item['found'] = datetime.date.today().isoformat()
        new.append(item)
        listing.insert(0, item)
    for item in new[:MAX_ALERTS_PER_RUN]:
        notify('Civil opening: ' + item['title'][:60], 'Source: ' + item['source'] + '\n' + item['url'], item['url'])
    if len(new) > MAX_ALERTS_PER_RUN:
        notify('Civil openings', str(len(new) - MAX_ALERTS_PER_RUN) + ' more found. Open the Altitude app to see them.')
    state['_openings_seen'] = dict(list(seen.items())[-600:])
    with open(OPEN_FILE, 'w') as f:
        json.dump(listing[:150], f, indent=1)


def main():
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    run_discovery(state)
    for job in JOBS:
        name = job.get('name', 'Application')
        key = hashlib.sha1(name.encode()).hexdigest()[:12]
        st = state.setdefault(key, {'pages': {}, 'seen': {}, 'seeded': False})
        seeded = st.get('seeded', False)

        for url in job.get('urls', []):
            try:
                text = visible_text(get(url))
            except Exception as e:
                print('fetch failed:', url, e)
                continue
            labels = labels_in(text)
            prev = st['pages'].get(url, {}).get('labels', [])
            new = [l for l in labels if l not in prev]
            st['pages'][url] = {'labels': labels}
            if seeded and new:
                notify(name + ': update found', ', '.join(new) + '\n' + url, url)

        for q in job.get('queries', [name]):
            for url, title in web_search(q):
                h = hashlib.sha1(url.encode()).hexdigest()[:12]
                if h in st['seen']:
                    continue
                st['seen'][h] = title[:120]
                if seeded:
                    notify(name + ': new web result', title + '\n' + url, url)

        st['seeded'] = True
        st['seen'] = dict(list(st['seen'].items())[-300:])

    with open(STATE, 'w') as f:
        json.dump(state, f, indent=1)


if __name__ == '__main__':
    main()
