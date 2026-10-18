#!/usr/bin/env python3
"""jobhunt - pull fresh remote / paid internships & jobs from many sources,
filter them, dedupe, and write JOBS.md + jobs.csv + data/jobs.json.

Run:  python jobhunt.py            (reads config.yaml)
Env (optional): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID -> pings you with NEW matches.
"""
import csv
import hashlib
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
NOW = datetime.now(timezone.utc)
S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (compatible; jobhunt/1.0; personal job-search tool)"


# ----------------------------------------------------------------- helpers
def get(url, **kw):
    kw.setdefault("timeout", 25)
    r = S.get(url, **kw)
    r.raise_for_status()
    return r


def clean(t):
    if t is None:
        return ""
    t = BeautifulSoup(str(t), "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def to_dt(v):
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            n = float(v)
            if n > 1e11:
                n /= 1000
            return datetime.fromtimestamp(n, timezone.utc)
        s = str(v).strip()
        try:
            d = parsedate_to_datetime(s)
        except Exception:
            try:
                d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                d = datetime.strptime(re.sub(r"\s+", " ", s), "%B %d, %Y")  # e.g. Amazon: "September 15, 2026"
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def job(source, title, company, url, location="", remote=None, pay="", posted=None, text=""):
    d = to_dt(posted)
    return {
        "source": source,
        "title": clean(title)[:140],
        "company": clean(company)[:80],
        "url": url or "",
        "location": clean(location)[:80],
        "remote": remote,
        "pay": clean(pay)[:60],
        "posted": d.isoformat() if d else "",
        "text": clean(text)[:600],
    }


def job_id(j):
    p = urlparse(j["url"])
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "ref"))]
    key = urlunparse(p._replace(query=urlencode(q), fragment="")) if j["url"] else j["title"] + j["company"]
    return hashlib.sha1(key.lower().encode()).hexdigest()[:12]


def num(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def parse_feed(url, source, remote=None):
    """Generic RSS/Atom reader."""
    root = ET.fromstring(get(url).content)
    out = []
    for el in root.iter():
        if not el.tag.lower().endswith(("}item", "}entry")) and el.tag.lower() not in ("item", "entry"):
            continue
        f = {}
        for c in el:
            name = c.tag.split("}")[-1].lower()
            if name == "link":
                f["link"] = c.get("href") or (c.text or "").strip()
            else:
                f.setdefault(name, c.text or "")
        title = f.get("title", "")
        company = ""
        if ": " in title and source == "WeWorkRemotely":
            company, title = title.split(": ", 1)
        out.append(job(source, title, company, f.get("link", ""), f.get("region", ""), remote, "",
                       f.get("pubdate") or f.get("published") or f.get("updated"),
                       f.get("description") or f.get("summary") or f.get("content") or ""))
    return out


# ---------------------------------------------------------------- fetchers
def f_remotive(cfg):
    out = []
    for t in cfg["search_terms"]:
        for x in get("https://remotive.com/api/remote-jobs", params={"search": t}).json().get("jobs", []):
            out.append(job("Remotive", x.get("title"), x.get("company_name"), x.get("url"),
                           x.get("candidate_required_location", ""), True, x.get("salary", ""),
                           x.get("publication_date"), x.get("description")))
    return out


def f_remoteok(cfg):
    out = []
    for x in get("https://remoteok.com/api").json():
        if not isinstance(x, dict) or "position" not in x:
            continue
        pay = f"${x['salary_min']}-{x['salary_max']}" if num(x.get("salary_min")) else ""
        out.append(job("RemoteOK", x["position"], x.get("company"), x.get("url") or x.get("apply_url"),
                       x.get("location", ""), True, pay, x.get("date"),
                       x.get("description", "") + " " + " ".join(x.get("tags") or [])))
    return out


def f_weworkremotely(cfg):
    out = []
    for u in ["https://weworkremotely.com/remote-jobs.rss",
              "https://weworkremotely.com/categories/remote-programming-jobs.rss"]:
        out += parse_feed(u, "WeWorkRemotely", remote=True)
    return out


def f_himalayas(cfg):
    out, offset = [], 0
    for _ in range(5):
        jobs = get("https://himalayas.app/jobs/api", params={"limit": 100, "offset": offset}).json().get("jobs", [])
        if not jobs:
            break
        for x in jobs:
            pay = f"{x.get('minSalary')}-{x.get('maxSalary')} {x.get('currency', '')}" if num(x.get("minSalary")) else ""
            loc = ", ".join(x.get("locationRestrictions") or []) or "Worldwide"
            out.append(job("Himalayas", x.get("title"), x.get("companyName"),
                           x.get("applicationLink") or x.get("guid"), loc, True, pay, x.get("pubDate"),
                           (x.get("excerpt") or "") + " " + (x.get("employmentType") or "")))
        offset += len(jobs)
    return out


def f_arbeitnow(cfg):
    out = []
    for p in (1, 2, 3):
        for x in get("https://www.arbeitnow.com/api/job-board-api", params={"page": p}).json().get("data", []):
            out.append(job("Arbeitnow", x.get("title"), x.get("company_name"), x.get("url"),
                           x.get("location", ""), bool(x.get("remote")), "", x.get("created_at"),
                           x.get("description", "")))
    return out


def f_jobicy(cfg):
    out = []
    for t in cfg["search_terms"]:
        d = get("https://jobicy.com/api/v2/remote-jobs", params={"count": 50, "tag": t}).json()
        for x in d.get("jobs", []):
            pay = f"{x.get('annualSalaryMin')}-{x.get('annualSalaryMax')} {x.get('salaryCurrency', '')}" if x.get("annualSalaryMin") else ""
            out.append(job("Jobicy", x.get("jobTitle"), x.get("companyName"), x.get("url"),
                           x.get("jobGeo", ""), True, pay, x.get("pubDate"),
                           x.get("jobExcerpt") or x.get("jobDescription")))
    return out


def f_hn_whoishiring(cfg):
    base = "https://hn.algolia.com/api/v1"
    hits = get(f"{base}/search_by_date", params={"tags": "story,author_whoishiring", "hitsPerPage": 5}).json()["hits"]
    story = next(h for h in hits if "who is hiring" in (h.get("title") or "").lower())
    cm = get(f"{base}/search", params={"tags": f"comment,story_{story['objectID']}", "hitsPerPage": 1000}).json()["hits"]
    out = []
    for c in cm:
        raw = (c.get("comment_text") or "").replace("<p>", "\n")
        txt = clean(raw)
        if not txt:
            continue
        out.append(job("HN Who's Hiring", txt.split("|")[0][:100] + " | " + (txt.split("|")[1] if "|" in txt else "")[:40],
                       "", f"https://news.ycombinator.com/item?id={c['objectID']}", "", None, "",
                       c.get("created_at"), txt))
    return out


def f_reddit(cfg):
    out = []
    for sub in cfg.get("reddit_subs", []):
        for j in parse_feed(f"https://www.reddit.com/r/{sub}/new/.rss", f"r/{sub}"):
            if sub.lower() == "forhire" and "[hiring]" not in j["title"].lower():
                continue
            if sub.lower() != "forhire" and not HIRING_WORDS.search(j["title"]):
                continue
            out.append(j)
    return out


def f_telegram(cfg):
    out = []
    for ch in cfg.get("telegram_channels", []):
        try:
            soup = BeautifulSoup(get(f"https://t.me/s/{ch}").text, "html.parser")
        except Exception as e:
            print(f"  telegram {ch}: {e}")
            continue
        for m in soup.select(".tgme_widget_message"):
            body = m.select_one(".tgme_widget_message_text")
            link = m.select_one("a.tgme_widget_message_date")
            if not body or not link:
                continue
            ext = next((a["href"] for a in body.select("a[href]") if "t.me" not in a["href"]), "")
            when = (m.select_one("time") or {}).get("datetime") if m.select_one("time") else None
            text = body.get_text("\n")
            first = next((l.strip() for l in text.splitlines() if l.strip()), "")
            out.append(job(f"Telegram/{ch}", first[:120], "", ext or link["href"], "", None, "", when, text))
    return out


def f_internshala(cfg):
    """Best-effort HTML scrape; selectors may need tuning if Internshala changes layout."""
    out = []
    urls = ["https://internshala.com/internships/work-from-home-internships/",
            "https://internshala.com/jobs/work-from-home-jobs/",
            "https://internshala.com/internships/", "https://internshala.com/fresher-jobs/"]
    urls += [f"https://internshala.com/internships/work-from-home-{r}-internships/"
             for r in cfg.get("internshala_roles", [])]
    for u in urls:
        try:
            soup = BeautifulSoup(get(u).text, "html.parser")
        except Exception as e:
            print(f"  internshala {u}: {str(e)[:60]}")
            continue
        for c in soup.select(".individual_internship"):
            a = c.select_one(".job-title-href, .job-internship-name a, h3 a, a[href]")
            href = c.get("data-href") or (a.get("href") if a else "")
            if not a or not href:
                continue
            comp = c.select_one(".company-name, .company_name")
            pay = c.select_one(".stipend, .salary")
            loc = c.select_one(".locations, .location_link")
            out.append(job("Internshala", a.get_text(), comp.get_text() if comp else "",
                           "https://internshala.com" + href if href.startswith("/") else href,
                           loc.get_text() if loc else "Work from home", True,
                           pay.get_text() if pay else "", None, c.get_text(" ")))
    return out


def f_unstop(cfg):
    """Best-effort: uses Unstop's public search endpoint used by its own site."""
    out = []
    for kind in ("internships", "jobs"):
        d = get("https://unstop.com/api/public/opportunity/search-result",
                params={"opportunity": kind, "per_page": 30, "oppstatus": "open", "page": 1}).json()
        items = ((d.get("data") or {}).get("data")) or []
        for x in items:
            org = (x.get("organisation") or {}).get("name", "")
            url = x.get("public_url") or ""
            url = url if url.startswith("http") else "https://unstop.com/" + url.lstrip("/")
            locs = " ".join(str(l.get("city", "")) if isinstance(l, dict) else str(l)
                            for l in (x.get("jobDetail", {}) or {}).get("locations", []) or [])
            out.append(job("Unstop", x.get("title"), org, url, locs, None, "",
                           x.get("start_date") or x.get("created_at"), x.get("details") or x.get("seo_title") or ""))
    return out


def f_yc(cfg):
    """Best-effort: harvest job links from ycombinator.com/jobs pages."""
    out, seen = [], set()
    urls = ["https://www.ycombinator.com/jobs"] + [f"https://www.ycombinator.com/jobs/role/{r}" for r in cfg.get("yc_roles", [])]
    for u in urls:
        try:
            soup = BeautifulSoup(get(u).text, "html.parser")
        except Exception as e:
            print(f"  yc {u}: {str(e)[:60]}")
            continue
        for a in soup.select("a[href*='/companies/'][href*='/jobs/']"):
            href = a["href"]
            if href in seen:
                continue
            seen.add(href)
            m = re.search(r"/companies/([^/]+)/jobs", href)
            title = clean(a.get_text()) or href.rsplit("/", 1)[-1].replace("-", " ")
            out.append(job("YC", title, (m.group(1).replace("-", " ").title() if m else ""),
                           "https://www.ycombinator.com" + href if href.startswith("/") else href, "", None, "", None, title))
    return out


def f_jobspy(cfg):
    """LinkedIn / Indeed / Google Jobs via python-jobspy (pip install python-jobspy). Often rate-limited on CI IPs."""
    from jobspy import scrape_jobs
    c = cfg.get("jobspy", {})
    out = []

    def s(v):
        return "" if v is None or (isinstance(v, float) and v != v) else str(v)

    for t in c.get("terms", ["internship"]):
        try:
            df = scrape_jobs(site_name=c.get("sites", ["linkedin", "indeed", "naukri", "google"]), search_term=t,
                             google_search_term=f"{t} jobs in {c.get('location', 'India')} since last week",
                             location=c.get("location", "India"), results_wanted=c.get("results_wanted", 40),
                             hours_old=cfg["max_age_days"] * 24, country_indeed=c.get("country", "India"))
        except Exception as e:
            print(f"  jobspy '{t}': {type(e).__name__}: {str(e)[:70]}")
            continue
        for r in df.to_dict("records"):
            pay = f"{s(r.get('min_amount'))}-{s(r.get('max_amount'))} {s(r.get('currency'))}/{s(r.get('interval'))}" if s(r.get("min_amount")) else ""
            out.append(job(f"jobspy/{s(r.get('site'))}", s(r.get("title")), s(r.get("company")), s(r.get("job_url")),
                           s(r.get("location")), True if r.get("is_remote") else None, pay,
                           s(r.get("date_posted")) or None, s(r.get("description"))))
    return out


def f_rss(cfg):
    """Any extra RSS/Atom feeds from config (e.g. Google Alerts feeds for X / Instagram / Wellfound)."""
    out = []
    for item in cfg.get("rss_feeds", []):
        name, url = (item["name"], item["url"]) if isinstance(item, dict) else ("RSS", item)
        out += parse_feed(url, name)
    return out


# ------------------------------------------------ company career pages (ATS)
# Most companies host their careers page on a shared applicant-tracking system with a public JSON API.
# One adapter covers every company on that system; we only need the company's slug.
RX = {}  # compiled filters, set in run(); used to drop non-matching titles early (keeps big boards cheap)


def title_ok(title):
    if RX.get("include") and not RX["include"].search(title):
        return False
    if RX.get("role") and not RX["role"].search(title):
        return False
    if RX.get("exclude") and RX["exclude"].search(title):
        return False
    return True


def rel_date(s):
    s = (s or "").lower()
    if "today" in s:
        return NOW.isoformat()
    if "yesterday" in s:
        return (NOW - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*day", s)
    return (NOW - timedelta(days=int(m.group(1)))).isoformat() if m else None


def a_greenhouse(slug):  # verified field names
    d = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs").json()
    return [job(f"GH/{slug}", x["title"], x.get("company_name") or slug, x.get("absolute_url"),
                (x.get("location") or {}).get("name", ""), None, "",
                x.get("first_published") or x.get("updated_at"), "")
            for x in d.get("jobs", []) if title_ok(x.get("title", ""))]


def a_lever(slug):  # verified field names
    out = []
    for x in get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}).json():
        if not title_ok(x.get("text", "")):
            continue
        c = x.get("categories") or {}
        loc = ", ".join(c.get("allLocations") or [c.get("location", "")])
        if x.get("workplaceType"):
            loc += f" ({x['workplaceType']})"
        out.append(job(f"Lever/{slug}", x["text"], slug, x.get("hostedUrl"), loc, None, "", x.get("createdAt"),
                       (x.get("descriptionPlain") or "")[:400] + " " + (c.get("commitment") or "")))
    return out


def a_ashby(slug):  # verified field names
    out = []
    d = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"}).json()
    for x in d.get("jobs", []):
        if not x.get("title") or x.get("isListed") is False or not title_ok(x["title"]):
            continue
        locs = [x.get("location") or ""] + [s.get("location", "") if isinstance(s, dict) else str(s)
                                            for s in x.get("secondaryLocations") or []]
        loc = ", ".join(l for l in locs if l)
        if x.get("isRemote") or str(x.get("workplaceType")).lower() == "remote":
            loc += " (remote)"
        comp = (x.get("compensation") or {}).get("compensationTierSummary", "")
        out.append(job(f"Ashby/{slug}", x["title"], slug, x.get("jobUrl"), loc, None, comp, x.get("publishedAt"),
                       (x.get("descriptionPlain") or "")[:400]))
    return out


def a_smartrecruiters(slug):  # UNVERIFIED shape (from public docs)
    out, offset = [], 0
    for _ in range(5):
        d = get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", params={"limit": 100, "offset": offset}).json()
        items = d.get("content", [])
        if not items:
            break
        for x in items:
            if not title_ok(x.get("name", "")):
                continue
            l = x.get("location") or {}
            loc = ", ".join(v for v in (l.get("city"), l.get("country")) if v) + (" (remote)" if l.get("remote") else "")
            out.append(job(f"SR/{slug}", x["name"], (x.get("company") or {}).get("name", slug),
                           f"https://jobs.smartrecruiters.com/{slug}/{x.get('id')}", loc, None, "", x.get("releasedDate"), ""))
        offset += len(items)
        if offset >= d.get("totalFound", 0):
            break
    return out


def a_workable(slug):  # UNVERIFIED shape
    out = []
    d = get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"}).json()
    for x in d.get("jobs", []):
        if not title_ok(x.get("title", "")):
            continue
        loc = ", ".join(v for v in (x.get("city"), x.get("state"), x.get("country")) if v)
        if x.get("telecommuting"):
            loc += " (remote)"
        out.append(job(f"Workable/{slug}", x["title"], d.get("name") or slug,
                       x.get("url") or f"https://apply.workable.com/{slug}/j/{x.get('shortcode')}/", loc, None, "",
                       x.get("published_on") or x.get("created_at"), (x.get("description") or "")[:400]))
    return out


def a_recruitee(slug):  # UNVERIFIED shape
    out = []
    for x in get(f"https://{slug}.recruitee.com/api/offers/").json().get("offers", []):
        if not title_ok(x.get("title", "")):
            continue
        loc = x.get("location") or ", ".join(v for v in (x.get("city"), x.get("country")) if v)
        if x.get("remote"):
            loc += " (remote)"
        pay = f"{x['min_salary']}-{x.get('max_salary')}" if num(x.get("min_salary")) else ""
        out.append(job(f"Recruitee/{slug}", x["title"], x.get("company_name") or slug, x.get("careers_url"), loc, None,
                       pay, x.get("published_at") or x.get("created_at"), (x.get("description") or "")[:400]))
    return out


def a_workday(e):  # UNVERIFIED (public CXS endpoint every Workday career site uses)
    t, wd, site = e["tenant"], e.get("wd", 5), e["site"]
    base = f"https://{t}.wd{wd}.myworkdayjobs.com"
    out = []
    for term in e.get("terms") or ["intern", "graduate", "engineer"]:
        for off in range(0, 100, 20):
            r = S.post(f"{base}/wday/cxs/{t}/{site}/jobs", timeout=25,
                       json={"appliedFacets": {}, "limit": 20, "offset": off, "searchText": term})
            r.raise_for_status()
            posts = r.json().get("jobPostings", [])
            if not posts:
                break
            for x in posts:
                if title_ok(x.get("title", "")):
                    out.append(job(f"Workday/{t}", x["title"], e.get("name", t), f"{base}/{site}{x.get('externalPath', '')}",
                                   x.get("locationsText", ""), None, "", rel_date(x.get("postedOn")), ""))
    return out


ATS = {"greenhouse": a_greenhouse, "lever": a_lever, "ashby": a_ashby, "smartrecruiters": a_smartrecruiters,
       "workable": a_workable, "recruitee": a_recruitee}
ATS_PREFIX = ("GH/", "Lever/", "Ashby/", "SR/", "Workable/", "Recruitee/", "Workday/")

SLUG_RX = {
    "greenhouse": re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I),
    "lever": re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)", re.I),
    "workable": re.compile(r"apply\.workable\.com/([a-z0-9_-]+)", re.I),
    "smartrecruiters": re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([a-z0-9_-]+)", re.I),
    "recruitee": re.compile(r"(?:https?://)?([a-z0-9-]+)\.recruitee\.com", re.I),
}
NOT_SLUG = {"embed", "api", "v1", "www", "jobs", "j", "o", "careers", "apply"}


def harvest_slugs(jobs):
    """Every listing from any source may link to a company's ATS board -> learn the company for next time."""
    found = {k: set() for k in SLUG_RX}
    for j in jobs:
        blob = f"{j['url']} {j['text']}"
        for ats, rx in SLUG_RX.items():
            for m in rx.finditer(blob):
                s = m.group(1).lower()
                if s not in NOT_SLUG:
                    found[ats].add(s)
    return found


def crawl_ats(cfg, discovered):
    cp = ROOT / "companies.yaml"
    seeds = yaml.safe_load(cp.read_text()) if cp.exists() else {}
    sp = ROOT / "data" / "ats_companies.json"
    sp.parent.mkdir(exist_ok=True)
    known = json.loads(sp.read_text()) if sp.exists() else {}
    dead = known.get("_dead", {})
    retry_cut = (NOW - timedelta(days=30)).isoformat()
    tasks = []
    for ats in ATS:
        slugs = set(seeds.get(ats) or []) | set(known.get(ats, [])) | discovered.get(ats, set())
        known[ats] = sorted(slugs)
        tasks += [(ats, s) for s in sorted(slugs) if dead.get(f"{ats}:{s}", "") < retry_cut]

    def work(t):
        ats, slug = t
        try:
            return t, ATS[ats](slug), None
        except Exception as e:
            return t, [], e

    out, ok, newdead, err = [], 0, 0, 0
    with ThreadPoolExecutor(max_workers=cfg.get("ats_workers", 12)) as ex:
        for t, js, e in ex.map(work, tasks):
            if e is None:
                ok += 1
                out += js
            elif getattr(getattr(e, "response", None), "status_code", None) in (400, 404, 410):
                dead[f"{t[0]}:{t[1]}"] = NOW.isoformat()
                newdead += 1
            else:
                err += 1
    wd_ok = 0
    for e in seeds.get("workday") or []:
        try:
            out += a_workday(e)
            wd_ok += 1
        except Exception as ex_:
            print(f"  workday {e.get('name')}: {str(ex_)[:70]}")
    known["_dead"] = dead
    sp.write_text(json.dumps(known, indent=1))
    return out, (f"{ok}/{len(tasks)} boards read, {newdead} newly dead, {sum(1 for k in dead if k.split(':')[0] in ATS)} known-dead skipped, {err} errors, "
                 f"{wd_ok} workday sites, {len(out)} title-matched postings; {sum(len(known[a]) for a in ATS)} companies tracked")


def f_amazon(cfg):
    out = []
    for t in cfg["search_terms"][:3]:
        d = get("https://www.amazon.jobs/en/search.json",
                params={"base_query": t, "loc_query": "India", "country[]": "IND", "result_limit": 100}).json()
        for x in d.get("jobs", []):
            if title_ok(x.get("title", "")):
                out.append(job("Amazon", x["title"], "Amazon", "https://www.amazon.jobs" + x.get("job_path", ""),
                               x.get("normalized_location") or x.get("location", ""), None, "",
                               x.get("posted_date"), x.get("description_short")))
    return out


FETCHERS = {
    "amazon": f_amazon,
    "remotive": f_remotive, "remoteok": f_remoteok, "weworkremotely": f_weworkremotely,
    "himalayas": f_himalayas, "arbeitnow": f_arbeitnow, "jobicy": f_jobicy,
    "hn_whoishiring": f_hn_whoishiring, "reddit": f_reddit, "telegram": f_telegram,
    "internshala": f_internshala, "unstop": f_unstop, "yc": f_yc, "jobspy": f_jobspy, "rss": f_rss,
}


# ----------------------------------------------------------------- filters
def words(ws):
    ws = [w for w in ws if w]
    if not ws:
        return None
    return re.compile("|".join(r"(?<![a-z0-9])" + re.escape(w.lower()) + r"(?![a-z0-9])" for w in ws), re.I)


UNPAID = re.compile(r"\b(unpaid|volunteer|no stipend|without stipend|equity only|for exposure|pro bono)\b"
                    r"|stipend[:\s]*(?:₹|rs\.?|inr|\$)?\s*0\b", re.I)
PAID = re.compile(r"\b(paid|stipend|salary|compensation)\b|[$₹€£]\s*\d|\d\s*(usd|inr|eur|lpa)\b|\bper (month|hour)\b", re.I)
REMOTE = re.compile(r"remote|work[ -]from[ -]home|\bwfh\b|anywhere|distributed", re.I)
BARE_REMOTE = re.compile(r"\W*(fully |100% )?(remote|work from home|wfh)( only)?\W*", re.I)
INDIA_SRC = ("Telegram/", "Internshala", "Unstop")  # India-focused sources: empty location is fine
HIRING_WORDS = re.compile(r"hiring|opening|vacanc|looking for|internship|\bintern\b|recruit|\bapply\b|referral", re.I)
ALLOWED_LOC = re.compile(r"worldwide|anywhere|global|india|apac|asia|work from home|\bwfh\b", re.I)


def paid_status(j):
    blob = f"{j['pay']} {j['title']} {j['text']}"
    if UNPAID.search(blob):
        return False
    if (j["pay"] and re.search(r"[1-9]", j["pay"])) or PAID.search(blob):
        return True
    return None


def keep(j, cfg, rx):
    head = f"{j['title']} {j['text'][:300]}"
    if rx["include"] and not rx["include"].search(head):
        return False
    if rx["role"] and not rx["role"].search(head):
        return False
    if rx["exclude"] and rx["exclude"].search(j["title"]):
        return False
    if cfg.get("remote_only", True):
        if not (j["remote"] or REMOTE.search(f"{j['title']} {j['location']} {j['text'][:300]}")
                or (rx["onsite_ok"] and rx["onsite_ok"].search(j["location"]))):
            return False
    loc = j["location"]
    good = lambda s: bool(ALLOWED_LOC.search(s) or (rx["onsite_ok"] and rx["onsite_ok"].search(s)))
    if cfg.get("india_only", True):
        if loc:
            # India city/state, worldwide/anywhere/APAC, or plain "Remote" -> ok. "Remote - US", "Berlin" -> drop.
            if not (good(loc) or BARE_REMOTE.fullmatch(loc.strip())):
                return False
        elif not j["source"].startswith(INDIA_SRC) and not good(f"{j['title']} {j['text']}"):
            return False  # no location given by a global source: need an India/worldwide mention
    elif loc and not ALLOWED_LOC.search(loc) and rx["blocked_loc"] and rx["blocked_loc"].search(loc):
        return False
    p = paid_status(j)
    j["paid"] = {True: "yes", False: "no", None: "?"}[p]
    mode = cfg.get("paid", "loose")
    if mode == "strict" and p is not True:
        return False
    if mode == "loose" and p is False:
        return False
    d = to_dt(j["posted"])
    if d and d < NOW - timedelta(days=cfg.get("max_age_days", 14)):
        return False
    return True


# ------------------------------------------------------------------ output
def md_row(j):
    t = j["title"].replace("|", "/")
    return (f"| {t} | {j['company'].replace('|', '/')} | {j['location'].replace('|', '/') or '-'} "
            f"| {j['pay'] or '-'} ({j['paid']}) | {j['source']} | {(j['posted'] or j['first_seen'])[:10]} "
            f"| [Apply]({j['url']}) |")


def write_md(path, new, allj, report, total_raw):
    head = "| Role | Company | Location | Pay (paid?) | Source | Posted | Apply |\n|---|---|---|---|---|---|---|"
    lines = [f"# Fresh remote & paid internships / jobs\n",
             f"_Updated {NOW:%Y-%m-%d %H:%M} UTC · {len(allj)} active · {len(new)} new this run · {total_raw} raw listings scanned_\n"]
    if new:
        lines += ["## New this run\n", head] + [md_row(j) for j in new[:100]] + [""]
    lines += ["## All active (newest first)\n", head] + [md_row(j) for j in allj[:400]] + [""]
    lines += ["## Source health\n"] + [f"- **{k}**: {v}" for k, v in report.items()]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_csv(path, allj):
    cols = ["title", "company", "pay", "paid", "location", "source", "posted", "first_seen", "url"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(allj)


def notify(new):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat and new):
        return
    for i in range(0, min(len(new), 30), 10):
        chunk = new[i:i + 10]
        msg = "🆕 New listings\n\n" + "\n\n".join(
            f"{j['title']} — {j['company'] or j['source']}\n{j['pay'] or ''} [{j['source']}]\n{j['url']}" for j in chunk)
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          data={"chat_id": chat, "text": msg, "disable_web_page_preview": True}, timeout=20)
        except Exception as e:
            print("telegram notify failed:", e)


# -------------------------------------------------------------------- main
def run(cfg, raw=None):
    rx = {
        "include": words(cfg.get("include_any", [])) if cfg.get("entry_level_only", True) else None,
        "role": words(cfg.get("role_any", [])),
        "exclude": words(cfg.get("exclude_any", [])),
        "blocked_loc": words(cfg.get("blocked_locations", [])),
        "onsite_ok": words(cfg.get("onsite_ok_locations", [])),
    }
    RX.clear()
    RX.update(rx)
    report, allraw = {}, []
    if raw is None:
        for name, fn in FETCHERS.items():
            if not cfg.get("sources", {}).get(name):
                continue
            try:
                js = fn(cfg)
                report[name] = f"{len(js)} fetched"
                allraw += js
            except Exception as e:
                report[name] = f"FAILED ({type(e).__name__}: {str(e)[:80]})"
            print(f"{name}: {report[name]}")
        if cfg.get("sources", {}).get("ats"):  # last, so it can learn company boards from everything above
            try:
                js, info = crawl_ats(cfg, harvest_slugs(allraw))
                report["ats"] = info
                allraw += js
            except Exception as e:
                report["ats"] = f"FAILED ({type(e).__name__}: {str(e)[:80]})"
            print(f"ats: {report['ats']}")
    else:
        allraw = raw

    kept, seen = [], set()
    for j in allraw:
        if not j["url"] or not keep(j, cfg, rx):
            continue
        j["id"] = job_id(j)
        if j["id"] in seen:
            continue
        seen.add(j["id"])
        kept.append(j)
    for k in report:
        if k == "ats":
            n = sum(1 for j in kept if j["source"].startswith(ATS_PREFIX))
        else:
            n = sum(1 for j in kept if j["source"].split("/")[0] == k or j["source"].lower().startswith(k.lower()))
        report[k] += f" → {n} matched"

    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    sp = data_dir / "jobs.json"
    state = json.loads(sp.read_text()) if sp.exists() else {}
    new = []
    for j in kept:
        old = state.get(j["id"])
        j["first_seen"] = old["first_seen"] if old else NOW.isoformat()
        j["last_seen"] = NOW.isoformat()
        state[j["id"]] = j
        if not old:
            new.append(j)
    cutoff = (NOW - timedelta(days=30)).isoformat()
    state = {k: v for k, v in state.items() if v["last_seen"] >= cutoff}
    sp.write_text(json.dumps(state, indent=1, ensure_ascii=False))

    active_cut = (NOW - timedelta(days=cfg.get("max_age_days", 14))).isoformat()
    allj = sorted((v for v in state.values() if v["last_seen"] >= active_cut),
                  key=lambda v: v["posted"] or v["first_seen"], reverse=True)
    new.sort(key=lambda v: v["posted"] or v["first_seen"], reverse=True)
    write_md(ROOT / "JOBS.md", new, allj, report, len(allraw))
    write_csv(ROOT / "jobs.csv", allj)
    notify(new)
    print(f"\n{len(allraw)} raw → {len(kept)} matched → {len(new)} new. See JOBS.md")
    return new, allj


if __name__ == "__main__":
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    run(cfg)
