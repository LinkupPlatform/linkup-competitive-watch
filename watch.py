#!/usr/bin/env python3
"""Weekly competitive watch, powered by Linkup.

  python watch.py import-alerts my_alerts.csv     # turn Boolean alerts ("(Galp AND EV charging)") into config/competitors.csv
  python watch.py retrieve --bl ALL               # Linkup search + filters -> runs/<end>/<BL>/candidates.json
  python watch.py triage   --bl ALL               # Claude triage -> judged.json   (or let your agent do it: AGENTS.md)
  python watch.py consolidate --bl ALL            # merge duplicate stories -> story_map.json
  python watch.py render   --bl ALL               # -> newsletter.md (deterministic, no LLM)
  python watch.py run      --bl ALL               # all of the above

Config is read from ./config if it exists, else ./config.sample (override with WATCH_CONFIG=/path).
Agent mode (no Anthropic key): `export-batches` -> agent writes batches/*.out.json -> `merge-batches` -> `consolidate` -> `render`.
Only LINKUP_API_KEY is required for retrieval. Stdlib only, except `anthropic` (triage/consolidate via Claude).
"""
import argparse, concurrent.futures as cf, csv, datetime as dt, glob, json, os, re, sys, time, unicodedata
import urllib.error, urllib.request
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PROMPTS, RUNS = ROOT / 'prompts', ROOT / 'runs'
CONFIG = Path(os.environ.get('WATCH_CONFIG') or (ROOT / 'config' if (ROOT / 'config' / 'competitors.csv').exists()
                                                 else ROOT / 'config.sample'))
BUSINESS_LINES = {r['code']: r['label'] for r in csv.DictReader(open(CONFIG / 'business_lines.csv', newline=''))}
COMPANY_PROFILE = (CONFIG / 'company.md').read_text().strip()
COMPANY_NAME = COMPANY_PROFILE.splitlines()[0].lstrip('# ').strip()
EVENT_SECTIONS = [('product_launch', 'Product launches'), ('partnership_client_win', 'Partnerships & client wins'),
                  ('funding_mna', 'Funding, M&A'), ('financial_results', 'Results & financials'),
                  ('leadership', 'Leadership'), ('expansion_market_entry', 'Expansion'),
                  ('regulation_legal', 'Regulation & legal'), ('strategy_interview', 'Strategy & interviews'),
                  ('market_trend', 'Market trends'), ('other', 'Other')]
JUNK_URL = re.compile(r'linkedin\.com/(in|company)/|/jobs?/|/careers?/|/job-|/vacature|indeed\.|glassdoor\.|welcometothejungle|'
                      r'studentjob|jooble|is-.*-down|downdetector|/tag/|/search\?|apps\.apple\.com|play\.google\.com', re.I)


def load_env():
    for p in (ROOT / '.env', Path.cwd() / '.env'):
        if p.exists():
            for line in p.read_text().splitlines():
                if '=' in line and not line.lstrip().startswith('#'):
                    k, v = line.strip().split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"\''))


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ============================== watchlist ==============================
def fold(s):
    """lowercase + strip accents, for matching."""
    return ''.join(c for c in unicodedata.normalize('NFKD', s.lower()) if not unicodedata.combining(c))


def parse_alert(raw):
    """Alerts can use Boolean syntax: '(Galp AND EV Charging)', 'Qonto or(anytime and bank)', 'vale* despensa'.
    Returns one {brand, context, query} per OR-alternative. brand = first AND-clause, the rest is context."""
    s = raw.replace('«', ' ').replace('»', ' ').replace('"', ' ')
    out = []
    for alt in re.split(r'(?i)\)\s*or\b|\bor\s*\(|\s+or\s+', s):
        alt = re.sub(r'[()]', ' ', alt)
        clauses = [re.sub(r'\s+', ' ', c).strip() for c in re.split(r'(?i)\s+and\s+', alt)]
        clauses = [c for c in clauses if c]
        if not clauses:
            continue
        brand, context = clauses[0], clauses[1:]
        query = ' '.join([brand] + context).replace('*', '').strip()
        out.append({'brand': brand, 'context': ' | '.join(context), 'query': query})
    return out


def brand_regex(brand):
    r"""'vale* despensa' -> \bvale\w*[\s-]*despensa\b, matched on accent-folded text."""
    words = [w for w in re.split(r'\s+', fold(brand)) if w]
    pats = [re.escape(w.rstrip('*')) + (r'\w*' if w.endswith('*') else '') for w in words]
    return re.compile(r'(?<!\w)' + r'[\s\-\.]*'.join(pats) + r'(?!\w)')


def domain(u):
    return urlparse(u.strip() if '://' in u else 'https://' + u.strip()).netloc.lower().removeprefix('www.')


def cmd_import_alerts(a):
    """CSV with columns business_line, category, alert, websites -> CONFIG/competitors.csv (alerts parsed, deduped)."""
    comps, seen = [], set()
    for r in csv.DictReader(open(a.path, newline='', encoding='utf-8-sig')):
        if not (r.get('alert') or '').strip():
            continue
        doms = ';'.join(d for d in (domain(u) for u in re.split(r'[;,\s]+', r.get('websites') or '') if u.strip()) if d)
        for p in parse_alert(r['alert']):
            key = (r['business_line'], p['query'].lower())
            if key not in seen:
                seen.add(key)
                comps.append({'business_line': r['business_line'], 'category': r.get('category', ''),
                              'alert': r['alert'].strip(), **p, 'domains': doms})
    with open(CONFIG / 'competitors.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['business_line', 'category', 'alert', 'brand', 'context', 'query', 'domains'])
        w.writeheader()
        w.writerows(comps)
    log(f'{CONFIG / "competitors.csv"}: {len(comps)} rows')


def load_config(bl):
    rd = lambda f: list(csv.DictReader(open(CONFIG / f, newline='')))
    comps = [r for r in rd('competitors.csv') if r['business_line'] == bl]
    terms = [r['term'] for r in rd('terms.csv') if r['business_line'] == bl]
    media = sorted({r['domain'] for r in rd('media.csv') if r['business_line'] in (bl, 'ALL')})[:100]
    return comps, terms, media


# ============================== retrieve ==============================
def linkup_search(body, tries=3):
    req = urllib.request.Request('https://api.linkup.so/v1/search', data=json.dumps(body).encode(),
                                 headers={'Authorization': 'Bearer ' + os.environ['LINKUP_API_KEY'],
                                          'Content-Type': 'application/json'})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r).get('results', [])
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or i == tries - 1:
                return {'error': f'HTTP {e.code}: {e.read()[:200].decode(errors="replace")}'}
        except Exception as e:
            if i == tries - 1:
                return {'error': repr(e)[:200]}
        time.sleep(2 * (i + 1))


def build_queries(comps, terms, media, from_date, to_date, max_results):
    """Query shape matters: bare entity name on `fast` + fromDate. News-phrased queries ('latest X news this week')
    on `standard` + fromDate return off-topic pages."""
    base = {'depth': 'fast', 'outputType': 'searchResults', 'fromDate': from_date, 'toDate': to_date,
            'maxResults': max_results}
    qs = [{'kind': 'competitor', 'entity': c['brand'], 'alert': c['alert'], 'context': c['context'],
           'category': c['category'], 'domains': c['domains'], 'body': {**base, 'q': c['query']}} for c in comps]
    qs += [{'kind': 'signal', 'entity': t, 'alert': t, 'context': '', 'category': 'market signal', 'domains': '',
            'body': {**base, 'q': t, 'includeDomains': media}} for t in terms]
    return qs


def prefilter(rows, from_date, to_date, per_target=6):
    """Deterministic filters. Keeps at most `per_target` results per watch target (Linkup rank order) and each URL once."""
    cands, drops, seen, seen_url = [], defaultdict(int), set(), set()
    for r in rows:
        if isinstance(r['results'], dict):
            drops['search_error'] += 1
            continue
        rx = brand_regex(r['entity']) if r['kind'] == 'competitor' else None
        taken = 0
        for x in r['results']:
            url, pub = x.get('url', ''), (x.get('publishedAt') or '')[:10]
            title, content = x.get('name', ''), x.get('content') or ''
            if JUNK_URL.search(url):
                drops['junk_url'] += 1; continue
            if not pub:
                drops['no_date'] += 1; continue
            if not (from_date <= pub <= to_date):
                drops['out_of_window'] += 1; continue
            if rx and not rx.search(fold(f'{title} {content} {url}')):
                drops['brand_not_mentioned'] += 1; continue
            key = (r['kind'], r['entity'].lower(), url.split('?')[0].rstrip('/'))
            if key in seen or key[2] in seen_url:
                drops['duplicate'] += 1; continue
            if taken >= per_target:
                drops['over_per_target_cap'] += 1; continue
            seen.add(key); seen_url.add(key[2]); taken += 1
            cands.append({'i': len(cands), 'kind': r['kind'], 'watch_target': r['entity'], 'alert': r['alert'],
                          'context': r['context'], 'category': r['category'], 'target_domains': r['domains'],
                          'title': title[:200], 'url': url, 'source': domain(url), 'published': pub,
                          'snippet': re.sub(r'\s+', ' ', content)[:600]})
    return cands, dict(drops)


def run_dir(a):
    d = RUNS / a.end / a.bl
    d.mkdir(parents=True, exist_ok=True)
    return d


def cmd_retrieve(a):
    if not os.environ.get('LINKUP_API_KEY'):
        sys.exit('LINKUP_API_KEY is not set (put it in .env)')
    comps, terms, media = load_config(a.bl)
    start = (dt.date.fromisoformat(a.end) - dt.timedelta(days=a.days)).isoformat()
    qs = build_queries(comps, terms, media, start, a.end, a.max_results)
    out = run_dir(a)
    t0 = time.time()
    if a.reuse and (out / 'raw.jsonl').exists():
        rows = [json.loads(l) for l in open(out / 'raw.jsonl')]
        log(f'[{a.bl}] reusing {out / "raw.jsonl"}')
    else:
        with cf.ThreadPoolExecutor(a.workers) as ex:
            rows = list(ex.map(lambda q: {**q, 'results': linkup_search(q['body'])}, qs))
        with open(out / 'raw.jsonl', 'w') as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
    n_raw = sum(len(r['results']) for r in rows if isinstance(r['results'], list))
    cands, drops = prefilter(rows, start, a.end, a.per_target)
    json.dump(cands, open(out / 'candidates.json', 'w'), ensure_ascii=False, indent=1)
    stats = {'business_line': a.bl, 'window': [start, a.end], 'competitor_queries': len(comps), 'term_queries': len(terms),
             'media_domains': len(media), 'linkup_calls': len(qs), 'raw_results': n_raw, 'drops': drops,
             'candidates': len(cands), 'retrieve_seconds': round(time.time() - t0)}
    json.dump(stats, open(out / 'stats.json', 'w'), indent=1)
    log(f'[{a.bl}] {len(qs)} Linkup calls, {n_raw} results -> {len(cands)} candidates ({stats["retrieve_seconds"]}s) {drops}')


# ============================== triage ==============================
def triage_prompt(bl):
    return ((PROMPTS / 'triage.md').read_text().replace('{business_line}', BUSINESS_LINES[bl])
            .replace('{company}', COMPANY_NAME).replace('{company_profile}', COMPANY_PROFILE))


def triage_schema():
    return json.loads((PROMPTS / 'triage_schema.json').read_text())


def cmd_triage(a):
    import anthropic
    out = run_dir(a)
    cands = json.load(open(out / 'candidates.json'))
    client, system, schema = anthropic.Anthropic(), triage_prompt(a.bl), triage_schema()
    fields = ('i', 'watch_target', 'kind', 'context', 'target_domains', 'title', 'url', 'published', 'snippet')

    def one(batch):
        payload = json.dumps([{k: c[k] for k in fields} for c in batch], ensure_ascii=False)
        for attempt in range(4):
            try:
                resp = client.messages.create(
                    model=a.model, max_tokens=16000, system=system,
                    output_config={'effort': 'low', 'format': {'type': 'json_schema', 'schema': schema}},
                    messages=[{'role': 'user', 'content': 'Triage every result. Return one item per input `i`.\n\n' + payload}])
                if resp.stop_reason == 'refusal':
                    return []
                return json.loads(next(b.text for b in resp.content if b.type == 'text'))['items']
            except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError, json.JSONDecodeError):
                time.sleep(5 * (attempt + 1))
        log('[triage] batch failed after retries')
        return []

    batches = [cands[i:i + a.batch_size] for i in range(0, len(cands), a.batch_size)]
    t0 = time.time()
    with cf.ThreadPoolExecutor(a.workers) as ex:
        judged = [v for vs in ex.map(one, batches) for v in vs]
    json.dump(judged, open(out / 'judged.json', 'w'), ensure_ascii=False, indent=1)
    log(f'[{a.bl}] triage: {len(judged)}/{len(cands)} judged, {sum(v["keep"] for v in judged)} kept ({time.time() - t0:.0f}s)')


def cmd_export_batches(a):
    out = run_dir(a)
    cands = json.load(open(out / 'candidates.json'))
    bdir = out / 'batches'
    bdir.mkdir(exist_ok=True)
    fields = ('i', 'watch_target', 'kind', 'context', 'target_domains', 'title', 'url', 'published', 'snippet')
    n = 0
    for n, s in enumerate(range(0, len(cands), a.batch_size)):
        json.dump([{k: c[k] for k in fields} for c in cands[s:s + a.batch_size]],
                  open(bdir / f'batch_{n:02d}.json', 'w'), ensure_ascii=False, indent=1)
    (bdir / 'PROMPT.md').write_text(triage_prompt(a.bl) + '\n\n## Output\nFor each batch_NN.json write batch_NN.out.json: '
                                    'a JSON array, one object per input `i`, matching this schema (the `items` array):\n\n```json\n'
                                    + json.dumps(triage_schema(), indent=1) + '\n```\n')
    log(f'[{a.bl}] wrote {n + 1} batches of <= {a.batch_size} to {bdir}')


def cmd_merge_batches(a):
    out = run_dir(a)
    judged = []
    for p in sorted(glob.glob(str(out / 'batches' / 'batch_*.out.json'))):
        data = json.load(open(p))
        judged += data['items'] if isinstance(data, dict) else data
    n_in = len(json.load(open(out / 'candidates.json')))
    json.dump(judged, open(out / 'judged.json', 'w'), ensure_ascii=False, indent=1)
    log(f'[{a.bl}] merged {len(judged)} verdicts for {n_in} candidates, {sum(1 for v in judged if v.get("keep"))} kept')


# ============================== consolidate ==============================
CONSOLIDATE_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['map'], 'properties': {'map': {
    'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'required': ['i', 'story_id'],
                               'properties': {'i': {'type': 'integer'}, 'story_id': {'type': 'string'}}}}}}


def kept_items(out):
    return [v for v in json.load(open(out / 'judged.json')) if v.get('keep')]


def cmd_consolidate(a):
    """Batches are triaged independently, so the same event can get different story_ids. One pass over kept headlines
    assigns a canonical story_id per real-world event -> story_map.json (used by render)."""
    out = run_dir(a)
    kept = kept_items(out)
    listing = [{'i': v['i'], 'company': v.get('company', ''), 'headline': v.get('headline', ''),
                'story_id': v.get('story_id', '')} for v in kept]
    (out / 'stories_to_consolidate.json').write_text(json.dumps(listing, ensure_ascii=False, indent=1))
    if not os.environ.get('ANTHROPIC_API_KEY'):
        log(f'[{a.bl}] no ANTHROPIC_API_KEY: agent should write {out / "story_map.json"} '
            f'from stories_to_consolidate.json (see prompts/consolidate.md)')
        return
    import anthropic
    resp = anthropic.Anthropic().messages.create(
        model=a.model, max_tokens=32000, system=(PROMPTS / 'consolidate.md').read_text(),
        output_config={'effort': 'low', 'format': {'type': 'json_schema', 'schema': CONSOLIDATE_SCHEMA}},
        messages=[{'role': 'user', 'content': json.dumps(listing, ensure_ascii=False)}])
    mapping = json.loads(next(b.text for b in resp.content if b.type == 'text'))['map']
    json.dump(mapping, open(out / 'story_map.json', 'w'), ensure_ascii=False, indent=1)
    log(f'[{a.bl}] consolidated {len(listing)} items into {len({m["story_id"] for m in mapping})} stories')


# ============================== render ==============================
def fmt_date(iso):
    return dt.date.fromisoformat(iso).strftime('%-d %b')


def cmd_render(a):
    out = run_dir(a)
    cands = {c['i']: c for c in json.load(open(out / 'candidates.json'))}
    stats = json.load(open(out / 'stats.json'))
    kept = []
    for v in json.load(open(out / 'judged.json')):
        if v.get('keep') and v.get('i') in cands:
            k = {**cands[v['i']], **v}
            k['company'] = (k.get('company') or k['watch_target']).strip()
            k['summary'] = k.get('summary') or k.get('headline') or k['title']
            k['event'] = k.get('event') if k.get('event') in dict(EVENT_SECTIONS) else 'other'
            k['importance'] = int(k.get('importance') or 1)
            kept.append(k)
    smap = {}
    if (out / 'story_map.json').exists():
        smap = {m['i']: m['story_id'] for m in json.load(open(out / 'story_map.json'))}
    stories = defaultdict(list)
    for k in kept:
        stories[smap.get(k['i']) or k.get('story_id') or k['url']].append(k)
    merged = []
    for sid, items in stories.items():
        items.sort(key=lambda k: (-k['importance'], k['published']))
        lead = items[0]
        links, seen = [], set()
        for k in sorted(items, key=lambda k: k['published']):
            u = k['url'].split('?')[0]
            if u not in seen:
                seen.add(u)
                links.append(f"[{k['source']}, {fmt_date(k['published'])}]({k['url']})")
        merged.append({**lead, 'links': links, 'n_sources': len(links),
                       'kind': 'competitor' if any(k['kind'] == 'competitor' for k in items) else 'signal'})
    merged.sort(key=lambda s: (-s['importance'], -s['n_sources'], s['company'].lower()))

    def bullet(s, why=False):
        text = f"- **{s['company']}**: {s['summary']}"
        if why and s.get('why_it_matters'):
            text += f" *Why it matters:* {s['why_it_matters']}"
        return text + ' ' + ' · '.join(s['links'])

    start, end = stats['window']
    top = [s for s in merged if s['importance'] >= 2][:a.top]
    top_ids = {id(s) for s in top}
    lines = [f"# {BUSINESS_LINES[a.bl]} competitive watch: week of {dt.date.fromisoformat(start).strftime('%-d %B %Y')}", '',
             f'_{fmt_date(start)} – {fmt_date(end)} {end[:4]} · generated with Linkup_', '', '## Top stories']
    lines += [bullet(s, why=True) for s in top] or ['- Nothing above minor this week.']
    lines += ['', '## Competitor moves']
    for event, title in EVENT_SECTIONS:
        sec = [s for s in merged if s['kind'] == 'competitor' and s['event'] == event and id(s) not in top_ids]
        if sec:
            lines += ['', f'### {title}'] + [bullet(s) for s in sec]
    sig = [s for s in merged if s['kind'] == 'signal' and id(s) not in top_ids]
    if sig:
        lines += ['', '## Market signals'] + [bullet(s) for s in sig]
    n_comp = len({k['watch_target'] for k in kept if k['kind'] == 'competitor'})
    lines += ['', '## Coverage', f"{stats['competitor_queries']} competitor alerts and {stats['term_queries']} market terms "
              f"(across {stats['media_domains']} media domains) were checked: {stats['raw_results']} results, "
              f"{stats['candidates']} after filters, {len(kept)} kept → {len(merged)} stories. "
              f"{n_comp} watched competitors had news this week."]
    (out / 'newsletter.md').write_text('\n'.join(lines) + '\n')
    stats.update(kept=len(kept), stories=len(merged), competitors_with_news=n_comp)
    json.dump(stats, open(out / 'stats.json', 'w'), indent=1)
    log(f'[{a.bl}] {len(merged)} stories -> {out / "newsletter.md"}')


def cmd_run(a):
    for bl in (BUSINESS_LINES if a.bl == 'ALL' else [a.bl]):
        sub = argparse.Namespace(**{**vars(a), 'bl': bl})
        cmd_retrieve(sub)
        cmd_triage(sub)
        cmd_consolidate(sub)
        cmd_render(sub)


# ============================== CLI ==============================
def main():
    load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest='cmd', required=True)
    p = sp.add_parser('import-alerts'); p.add_argument('path'); p.set_defaults(fn=cmd_import_alerts)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--bl', default='ALL', choices=list(BUSINESS_LINES) + ['ALL'])
    common.add_argument('--end', default=dt.date.today().isoformat(), help='last day of the window (YYYY-MM-DD)')
    common.add_argument('--days', type=int, default=7)
    common.add_argument('--max-results', type=int, default=10)
    common.add_argument('--per-target', type=int, default=6, help='max candidates kept per watch target')
    common.add_argument('--reuse', action='store_true', help='retrieve: re-filter the saved raw.jsonl, no new Linkup calls')
    common.add_argument('--workers', type=int, default=8)
    common.add_argument('--batch-size', type=int, default=25)
    common.add_argument('--model', default=os.environ.get('WATCH_MODEL', 'claude-opus-5-5'))
    common.add_argument('--top', type=int, default=5)
    for name, fn in (('retrieve', cmd_retrieve), ('triage', cmd_triage), ('consolidate', cmd_consolidate),
                     ('render', cmd_render), ('run', cmd_run),
                     ('export-batches', cmd_export_batches), ('merge-batches', cmd_merge_batches)):
        sp.add_parser(name, parents=[common]).set_defaults(fn=fn)
    a = ap.parse_args()
    if a.cmd not in ('run', 'import-alerts') and a.bl == 'ALL':
        for bl in BUSINESS_LINES:
            a.fn(argparse.Namespace(**{**vars(a), 'bl': bl}))
    else:
        a.fn(a)


if __name__ == '__main__':
    main()
