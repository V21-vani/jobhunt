# jobhunt

Pulls fresh **remote + paid** internships/jobs from many sources, dedupes, and keeps `JOBS.md` (readable list), `jobs.csv` and `data/jobs.json` up to date. Runs free on GitHub Actions every 6 hours; new matches can ping you on Telegram.

## Setup (5 min)
1. Create a GitHub repo, push this folder.
2. Repo â†’ Settings â†’ Actions â†’ General â†’ Workflow permissions â†’ **Read and write**.
3. Actions tab â†’ `update-jobs` â†’ **Run workflow**. `JOBS.md` appears in the repo.
4. (Optional) Telegram pings: make a bot with @BotFather, then add repo secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

Local run: `pip install -r requirements.txt && python jobhunt.py`

## Sources
| Reliable (public APIs/feeds) | Best-effort (HTML, may need selector tweaks) | Via your own config |
|---|---|---|
| Remotive, RemoteOK, WeWorkRemotely, Himalayas, Arbeitnow, Jobicy, HN Who's Hiring, r/forhire | Internshala, Unstop, YC jobs | Telegram channels, any RSS feed |
| | LinkedIn / Indeed / Google Jobs (`jobspy`, off by default, often blocked on CI IPs) | |

**X, Instagram, Wellfound:** no free official API and they block scrapers, so they aren't scraped directly. Workaround: make Google Alerts (deliver to RSS) such as `site:x.com "hiring" intern remote`, paste the feed URLs into `rss_feeds` in `config.yaml`.

**Telegram:** add public channel handles to `telegram_channels` (works for any channel that has a `t.me/s/<name>` preview).

## India-first filtering
`india_only: true` keeps a listing only if it is located in India (any city/state in `onsite_ok_locations`), or is remote and open to India (Worldwide / Anywhere / APAC / plain "Remote"). "Remote - US", "Berlin" and "Remote (EU)" are dropped. `remote_only: false` means on-site/hybrid India jobs are included too. Every row in `JOBS.md` has an **Apply** link, and `jobs.csv` has the same links.

## LinkedIn / Indeed / Naukri / Google Jobs
Handled by `python-jobspy` (on by default). These sites rate-limit and block cloud IPs, so the GitHub Actions run may return few or none. If the Source health line for `jobspy` shows 0 or FAILED, run it from your own laptop on home internet: `pip install python-jobspy && python jobhunt.py`. Scraping these sites goes against their terms of service, so keep volumes modest.

## Telegram / Reddit
Five public India job channels are preconfigured and were checked (public preview, recent posts, apply links). Reddit subs are in `config.yaml`; Reddit often blocks cloud IPs, so some may fail.

## Company career pages
Most companies run their careers page on a shared applicant-tracking system with a public JSON API, so one adapter reads every company on it: **Greenhouse, Lever, Ashby** (field names checked against live responses), **SmartRecruiters, Workable, Recruitee, Workday** (written from docs, not yet verified), plus **Amazon**.

- Seed companies live in `companies.yaml` (written from memory, wrong slugs are skipped automatically). Add your own.
- The list **grows by itself**: each run harvests board links from every listing found by the other sources and adds those companies to `data/ats_companies.json`.
- Not covered: companies with fully custom career sites (Google, Microsoft, Apple, most Indian IT services). Each needs its own adapter.
- Company boards are mostly on-site, so `onsite_ok_locations` in `config.yaml` also keeps non-remote jobs in those cities (default: India metros). Set `entry_level_only: false` to see all seniorities.

## Tuning
Everything is in `config.yaml`: keywords, roles, excluded seniority, `paid: strict|loose|off`, `max_age_days`, blocked countries, source toggles. The "Source health" section at the bottom of `JOBS.md` shows which sources worked on the last run.
