# Threads Job Monitor

A bot that watches Threads for job vacancies in Indonesia and pushes them to
Telegram, so leads arrive same-day instead of three days late.

Built from `PRD: Threads Job Monitor Bot` (draft v2). Two discovery sources
feed one shared pipeline:

```
SOURCE A (fast lane)              SOURCE B (wide net)
Profile monitor                   Keyword search
watched accounts                  site:threads.net "loker" etc
      |                                  |
      | poll every 30 min                | run every 6 hrs
      | pull hidden JSON                 | search API -> post URLs
      | from profile pages               | scrape each new URL
      |                                  |
      +------------> [ DEDUPE ] <--------+
                          |
                     [ FILTER ]  keyword include/exclude
                          |
                    [ EXTRACT ]  email, links, role, deadline
                          |
                     [ DRAFT ]   if email found
                          |
                    [ NOTIFY ]   Telegram, one msg per lead
                          |
              [ ACCOUNT SUGGESTION ]  if source B hit an
                                      unwatched account
```

**The bot drafts. It does not send.** There is no email send path anywhere in
`src/`, and `tests/test_no_send_path.py` fails if one appears. PRD section 9
has the reasoning; read it before adding one.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # Threads does not render without JS

cp .env.example .env                 # fill in the Telegram token and chat id
python -m src.main --check           # validates config, prints what is missing
```

Telegram: create a bot with [@BotFather](https://t.me/BotFather) for
`TELEGRAM_BOT_TOKEN`, then message [@userinfobot](https://t.me/userinfobot) for
`TELEGRAM_CHAT_ID`. Set `TELEGRAM_OPERATOR_CHAT_ID` to Yus's own chat so source
alerts and watch-list suggestions do not land in her feed.

Source B needs a search provider. Google Custom Search gives 100 queries/day
free, which covers 10 queries run a few times daily:

```
SEARCH_PROVIDER=google_cse
GOOGLE_CSE_API_KEY=...
GOOGLE_CSE_ENGINE_ID=...    # create at programmablesearchengine.google.com
```

Brave is the alternative (`SEARCH_PROVIDER=brave`, `BRAVE_API_KEY=...`).
Swapping providers is a one-file change in `src/search_api.py`. Leave
`SEARCH_PROVIDER=none` to run Source A alone -- Source B being unconfigured
never stops Source A.

## Running

```bash
python -m src.main                   # run forever: this is what systemd starts
python -m src.main --once source_a   # one watch-list poll
python -m src.main --once source_b   # one search cycle
python -m src.main --once all --dry-run   # print notifications, send nothing
python -m src.main --stats           # leads by source and status, source health
python -m src.main --check           # validate config and exit
```

Start with `--once source_a --dry-run`. It answers "does this find real leads"
without a Telegram bot existing yet.

### Deploying

`deploy/threads-monitor.service` is a systemd unit; edit the paths and:

```bash
sudo cp deploy/threads-monitor.service /etc/systemd/system/
sudo systemctl enable --now threads-monitor
journalctl -u threads-monitor -f
```

PM2 works too: `pm2 start "python -m src.main" --name threads-monitor --cwd
/opt/threads-monitor --interpreter /opt/threads-monitor/.venv/bin/python`.

## Configuration

Everything tunable is in `config.yaml`; secrets are only in `.env`. Adding or
removing a watched account is one line in `source_a.watched_accounts`.

**The values shipped in `config.yaml` are placeholders.** PRD section 12 flags
four things as blocking questions to answer with the job seeker, and the bot
works but finds the wrong leads until they are:

1. **Seed watch list** -- 10 to 15 real accounts. Scroll together, collect them.
2. **Search queries**, Group 2 especially. The casual phrasings Indonesian
   people actually use for "my company is hiring". She knows these; guessing
   them is how Source B ends up worthless.
3. **Include keywords** -- role titles she would actually take.
4. **Hard excludes** -- cities, seniority, contract types that are non-starters.

Also: `templates/application_email.txt` should be rewritten by her, not by us,
or the drafts will not sound like her. Edits are picked up without a restart.

## How it behaves

- **Dedupe is cross-source.** A post found by both sources notifies once, and
  seen posts survive restarts, so a reboot never replays the backlog.
- **First poll of a new account skips its backlog** (`first_run_max_age_hours`),
  so adding an account does not dump 40 old posts into the chat.
- **Nothing is dropped silently.** Every filtered post is logged with its reason
  to the `filtered_log` table; `--stats` shows the most recent. Review these
  weekly for the first month -- a filter that is too tight loses real leads.
- **Sources fail independently**, with their own backoff (2, 4, 8, 16, 32, 60
  minutes on 403/429) and their own health record. After 5 consecutive failed
  cycles a source pauses for an hour and Yus gets one alert, then one more when
  it recovers.
- **Silent failure is alerted too.** Three consecutive cycles that return zero
  posts with no errors raises an alert, because a scraper that quietly returns
  nothing is worse than one that crashes: usually it means Meta changed the
  hidden JSON shape.
- **Undeliverable notifications are queued**, not lost, and retried every five
  minutes.
- **Discovery suggests, it does not promote.** Once an unwatched account has
  produced two passing posts via search, Yus gets one suggestion. Promotion is
  a manual config edit, so one spam account cannot poison the fast lane.

## Layout

```
src/
  sources/
    profiles.py     Source A: watched-account polling
    search.py       Source B: search API -> URLs -> scrape
    base.py         shared Source interface, so adding a third is cheap
  scraper.py        Playwright fetch + hidden JSON extraction
  parser.py         jmespath mapping into a clean Post object
  search_api.py     search providers behind one interface
  filters.py        keyword include/exclude
  extract.py        email, URL, role title, deadline regex
  drafts.py         template rendering
  pipeline.py       dedupe -> filter -> extract -> draft -> notify -> discover
  notify.py         Telegram send + button callbacks
  discovery.py      account suggestion logic
  store.py          SQLite: seen posts, seen URLs, leads, queue, counts, health
  loop.py           scheduling, per-source backoff, error alerts
  config.py         YAML + env loading
  main.py           entrypoint
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

89 tests, no network. The scraper's Playwright layer is faked; the JSON parsing
it feeds is tested against fixture pages shaped like the real hidden payload.
Every P0 acceptance criterion in the PRD has a test.

## Status against the PRD

**Built (all P0):** R1 profile scraper, R2 search client, R3 post-URL scraper,
R4 shared store and cross-source dedupe, R5 keyword filter, R6 field
extraction, R7 Telegram notification with inline buttons, R8 draft generation,
R9 account discovery, R10 rate limiting and error handling, R11 config.

**Two P1 items came along because they were nearly free:** the applied tracker
(the buttons needed somewhere to write, so `--stats` reports leads found,
applied and ignored, split by source) and the post age cutoff.

**Not built, deliberately:** weekly digest summary message, Source B query
tuning report (the query that found each URL is already recorded, so the report
is a read query away), daily digest mode, near-duplicate detection. All P1 or
P2 in the PRD, all best decided after a week of real data.

**Untested against live Threads.** The sandbox this was built in has no egress
to threads.net, so the Playwright fetch path has never run against a real page.
Everything downstream of the fetch is tested. Expect the first live run to need
adjustment in `src/parser.py`'s `POST_CONTAINER_KEYS` if Meta's payload shape
has moved -- run `--once source_a --dry-run` first and check the log for
"no posts parsed".
