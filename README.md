# Competitive watch, powered by Linkup

Turns a competitor watchlist into a weekly newsletter. Every story is a real, dated article with its source link. One run covers hundreds of competitors and market terms, which is far beyond what a chat agent with a search tool gets through.

```
config/*.csv ─► retrieve (Linkup) ─► filters ─► triage (LLM) ─► consolidate (LLM) ─► render ─► newsletter.md
                 1 search/target      no LLM     Claude or your agent                no LLM
```

On a real watchlist of ~950 competitor alerts and ~190 market terms across 3 business lines, one weekly run made 1,143 Linkup searches in about 2.5 minutes. The result was 172 deduplicated stories, with every bullet linked to its source.

## Quick start
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt     # Python 3.10+
echo "LINKUP_API_KEY=..." > .env                                         # https://app.linkup.so
echo "ANTHROPIC_API_KEY=..." >> .env                                     # optional, see below
.venv/bin/python watch.py run                                            # runs the sample config in config.sample/
```
Newsletters land in `runs/<date>/<business line>/newsletter.md`.

**No Anthropic key?** Any LLM agent can do the two LLM steps itself. Point it at **[AGENTS.md](AGENTS.md)**.

## Use your own watchlist
1. `cp -r config.sample config` (`config/` is gitignored and takes precedence over `config.sample/`).
2. Edit these files in `config/`:
   - `company.md`: who you are, what you sell, your main rivals. The triage step uses it to judge relevance.
   - `business_lines.csv`: one newsletter per line.
   - `alerts.csv`: your competitors. It accepts Boolean alerts like `(Galp AND EV charging)` or `Qonto or (anytime AND bank)`. Run `python watch.py import-alerts config/alerts.csv` to turn it into `competitors.csv`.
   - `terms.csv`: market keywords, one search each.
   - `media.csv`: the news and trade sites those keywords are searched on (business line `ALL` applies to every line).

## How it works
1. **Retrieve**: one Linkup `fast` search per competitor, using the brand plus its alert keywords, limited to the week with `fromDate`. Market terms are searched only on your media list.
2. **Filter** (deterministic):
   - drop results outside the week
   - drop job, profile and app-store URLs
   - drop articles that never mention the brand
   - keep at most 6 results per target
   - remove duplicate URLs
3. **Triage** (LLM): keeps only real, recent news about the target and drops listicles, SEO pages and stock noise. It writes an English headline, summary and "why it matters" line, and assigns an event type and an importance from 1 to 3. Rules are in `prompts/triage.md`.
4. **Consolidate** (LLM, small): articles about the same event get one story ID.
5. **Render** (deterministic): Top stories, Competitor moves by event type, Market signals, then a coverage line.

### Query shape matters
Search for the **bare brand name** on `fast` depth with `fromDate`. News-phrased questions ("what's the latest news about X this week?") on `standard` depth with `fromDate` return off-topic pages: daily digests, unrelated LinkedIn posts.

## Commands
| Command | What it does |
|---|---|
| `run` | retrieve → triage → consolidate → render (needs `ANTHROPIC_API_KEY`) |
| `retrieve` | Linkup searches + filters → `candidates.json` |
| `triage` / `consolidate` | LLM steps via Claude |
| `export-batches` / `merge-batches` | Agent mode: split candidates for an agent to triage, then merge its verdicts |
| `render` | Writes `newsletter.md` |
| `import-alerts` | Boolean alerts CSV → `competitors.csv` |

Common flags: `--bl <code|ALL>`, `--end YYYY-MM-DD` (default today), `--days 7`, `--per-target 6`, `--reuse` (re-filter saved results without new searches).
