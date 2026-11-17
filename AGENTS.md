# AGENTS.md: running the weekly competitive watch

You are an agent producing weekly competitive-watch newsletters. Run every command from this folder, and follow the steps in order.

## 0. Setup (once)
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # Python 3.10+
echo "LINKUP_API_KEY=..." > .env                                       # required
echo "ANTHROPIC_API_KEY=..." >> .env                                   # optional: lets the script do steps 2 and 3 itself
```
The watchlist is read from `config/` if it exists, otherwise from `config.sample/`. See README → "Use your own watchlist".

## 1. Retrieve (Linkup, no LLM)
```bash
.venv/bin/python watch.py retrieve --bl ALL            # --end YYYY-MM-DD (default today), --days 7
```
This writes `runs/<end>/<BL>/candidates.json` and `stats.json`.

## 2. Triage, using either option A or option B
**A. With `ANTHROPIC_API_KEY`:**
```bash
.venv/bin/python watch.py triage --bl ALL
```
**B. You do it (no extra key):**
```bash
.venv/bin/python watch.py export-batches --bl ALL --batch-size 300
```
For every `runs/<end>/<BL>/batches/batch_NN.json`:
- Read `batches/PROMPT.md`, which holds the rules and the output schema.
- Judge every item and write `batch_NN.out.json`: a JSON array with one object per input `i`.
  - Kept items have every schema field.
  - Dropped items can be just `{"i": n, "keep": false, "reason": "..."}`.
- Batches are independent, so they can be triaged in parallel.

Then run:
```bash
.venv/bin/python watch.py merge-batches --bl ALL      # writes judged.json
```
Rules that matter most:
- Keep only dated news genuinely about the watched company, or real market developments.
- Facts must come only from the title or snippet.
- Give articles about the same event the same `story_id`.

## 3. Consolidate duplicate stories
```bash
.venv/bin/python watch.py consolidate --bl ALL
```
With `ANTHROPIC_API_KEY`, this writes `story_map.json` for you. Without it, the command writes `stories_to_consolidate.json` (kept headlines only, small). Follow `prompts/consolidate.md` and write `runs/<end>/<BL>/story_map.json`: `[{"i": n, "story_id": "..."}]`, one entry per item.

## 4. Render (no LLM)
```bash
.venv/bin/python watch.py render --bl ALL             # writes runs/<end>/<BL>/newsletter.md
```

## 5. Check before sending
- Every bullet must have at least one source link, and the window in the header must be right.
- If a story is wrong, set `keep` to false in `judged.json` and re-run `render`. Don't hand-edit the markdown.

## Don'ts
- Don't rewrite the search queries as questions ("latest news about X this week") and don't switch them to `standard` depth. With `fromDate`, that shape returns off-topic pages. Bare names on `fast` + `fromDate` are what work.
- Don't commit `.env`, `config/` (the private watchlist) or `runs/`.
