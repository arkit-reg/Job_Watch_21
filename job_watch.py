#!/usr/bin/env python3
"""
job_watch.py - watches company career sites for new postings that match your target
roles and sends you one digest (email and/or phone push) when something new shows up.

  python job_watch.py                               normal run: check everything, notify about new matches
  python job_watch.py --check                       test companies.csv: show what each site returned (sends and saves nothing)
  python job_watch.py --check --company "Regeneron" test a single company
  python job_watch.py --test-notify                 send a test message to check your email / push setup

Most pharma and biotech career pages are front ends for a handful of applicant tracking
systems (Workday, Greenhouse, Lever, Ashby, SmartRecruiters). Those systems serve clean job
data, so the script asks them directly instead of scraping HTML. For anything else it reads
the links on the page, and with render=yes it loads the page in a headless browser first.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
import threading
from urllib.parse import parse_qsl, quote, urljoin, urlparse, urlunparse

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/New_York")  # times in your messages are Eastern
except Exception:
    LOCAL_TZ = timezone.utc

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # older Python
    import tomli as tomllib

import warnings

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)  # some sites return XML; that's fine

HERE = Path(__file__).resolve().parent
COMPANIES_FILE = HERE / "companies.csv"
CONFIG_FILE = HERE / "config.toml"
STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "jobs_log.csv"
MANUAL_FILE = HERE / "manual_check.csv"
ENV_FILE = HERE / ".env"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 20                 # seconds per request
MANUAL_AFTER_FAILURES = 2    # a site that fails this many runs in a row goes on the check-by-hand list
WORKDAY_MAX_PER_TERM = 100   # Workday results read per search term (best matches come first)
WORKDAY_PARALLEL = 3         # requests at once per Workday server group, so Workday doesn't block us


# --------------------------------------------------------------------------- data

@dataclass
class Job:
    id: str
    title: str
    url: str
    location: str = ""
    posted: str = ""                       # the site's own wording, e.g. Workday's "Posted Yesterday"
    posted_at: datetime | None = None      # when it was posted, if the site says
    detail: str = ""                       # job system address for the full description, if there is one
    description: str = ""                  # full description text, when the listing already includes it
    sponsor: str = ""                      # "no" / "yes" / "" from the description
    sponsor_note: str = ""                 # the sentence it was based on
    years: str = ""                        # e.g. "3+ yrs" from the description
    context: str = ""                      # text around a link on a page (used to spot non-US jobs)
    country: str = ""                      # country the job system gives, if any
    text: str = ""                         # full description, once read

    def __post_init__(self):
        self.title = clean(self.title)
        self.location = clean(self.location)


def clean(value) -> str:
    return " ".join(html.unescape(str(value or "")).split())


NOW = datetime.now(timezone.utc)  # reset at the start of each run
DEADLINE: float | None = None     # time.time() after which no more slow work starts this run


class OutOfTime(BaseException):  # not an Exception, so "ignore errors" code never swallows it
    """The run's time limit passed: leave this company for the next run."""


def out_of_time() -> bool:
    return DEADLINE is not None and time.time() > DEADLINE


def parse_time(value) -> datetime | None:
    """ISO date/time text from a job system -> UTC datetime (None if missing or unreadable)."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):  # date only: assume midday
            return datetime.fromisoformat(text + "T12:00:00+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def workday_posted(text: str) -> datetime | None:
    """Workday only says "Posted Today", "Posted Yesterday", "Posted 3 Days Ago" or "30+ Days Ago".
    Each "N days ago" is taken as the middle of that day (N days and 12 hours)."""
    t = (text or "").lower()
    if "today" in t:
        return NOW
    if "yesterday" in t:
        return NOW - timedelta(hours=36)
    m = re.search(r"(\d+)\+?\s*days?", t)
    return NOW - timedelta(hours=int(m.group(1)) * 24 + 12) if m else None


def age_text(job: Job) -> str:
    if job.posted:
        return job.posted
    if job.posted_at:
        hours = max(0, (NOW - job.posted_at).total_seconds() / 3600)
        return f"Posted {hours:.0f}h ago" if hours < 48 else f"Posted {hours / 24:.0f} days ago"
    return ""


# --------------------------------------------------------------------------- http

def http(method: str, url: str, tries: int = 2, timeout: int = TIMEOUT, **kw) -> requests.Response:
    """Request with a browser-like user agent and one retry for flaky servers."""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9", **kw.pop("headers", {})}
    last_error: Exception = RuntimeError(f"no response from {url}")
    sender = kw.pop("session", None) or requests
    attempt, limit = 0, tries
    while attempt < limit:
        delay = 2
        try:
            r = sender.request(method, url, headers=headers, timeout=timeout, **kw)
        except requests.exceptions.SSLError as e:
            last_error = e
            if kw.get("verify", True):  # a certificate setup browsers accept but Python doesn't:
                kw["verify"] = False      # reading a public job list without it is harmless
                limit = max(limit, attempt + 2)
                delay = 0
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_error = e
        else:
            if r.status_code == 429:  # too many requests: wait as long as the site asks, up to 30s
                limit = max(limit, 4)
                after = r.headers.get("Retry-After", "")
                delay = min(int(after), 30) if after.isdigit() else 5 * (attempt + 1)
                last_error = RuntimeError(f"HTTP 429 (too many requests) from {url}")
            elif r.status_code >= 500:
                last_error = RuntimeError(f"HTTP {r.status_code} from {url}")
            elif r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code} from {url}")
            else:
                return r
        attempt += 1
        if attempt < limit:
            time.sleep(delay)
    raise last_error


_workday_slots: dict = {}
_workday_lock = threading.Lock()


def workday_slot(host: str) -> threading.Semaphore:
    """Many companies share each Workday server group (wd1, wd3, wd5...); limit requests to each."""
    group = host.split(".", 1)[1] if "." in host else host
    with _workday_lock:
        return _workday_slots.setdefault(group, threading.Semaphore(WORKDAY_PARALLEL))


# --------------------------------------------------------------------------- which system runs the site?

ATS_PATTERNS = [
    ("workday", re.compile(r"https?://([\w-]+\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?(?!wday\b)([\w-]+)")),
    ("workday_site", re.compile(r"https?://(wd\d+\.myworkdaysite\.com)/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([\w-]+)/([\w-]+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([\w-]+)")),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([\w-]+)")),
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?!embed\b)([\w-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([\w.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/(?!api\b)([\w.%-]+)")),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/(?!oneclick-ui\b|sr-jobs\b)([\w-]+)")),
    ("bamboohr", re.compile(r"https?://(?!www\.|api\.|help\.|marketplace\.)([\w-]+)\.bamboohr\.com")),
    ("workable", re.compile(r"apply\.workable\.com/(?!api\b|j\b)([\w-]+)")),
    ("jobvite", re.compile(r"jobs\.jobvite\.com/(?!api\b)([\w-]+)")),
    ("icims", re.compile(r"https?://((?!www\.)[\w-]+\.icims\.com)")),
    ("ultipro", re.compile(r"https?://(recruiting2?\.ultipro\.com)/([A-Za-z0-9]+)/JobBoard/([0-9a-fA-F-]{36})")),
    ("paylocity", re.compile(r"recruiting\.paylocity\.com/recruiting/jobs/(?:All|Details)/([0-9a-fA-F-]{36})", re.I)),
    ("adp", re.compile(r"workforcenow\.adp\.com/[^\"'\s<>]*?[?&]cid=([0-9a-fA-F-]{36})")),
    ("oracle", re.compile(r"https?://([\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/([a-z]{2}(?:-[A-Z]{2})?)/sites/([\w-]+)")),
    ("eightfold", re.compile(r"https?://([\w-]+\.eightfold\.ai)/careers(?:[^\"'\s<>]*?[?&]domain=([\w.-]+))?")),
]


def detect_ats(text: str):
    """Return (system, captured parts) for the first job-system URL found in text, else None."""
    text = html.unescape(text.replace("\\/", "/"))
    for name, pattern in ATS_PATTERNS:
        m = pattern.search(text)
        if m:
            return name, m.groups()
    return None


def fetch_from_ats(ats: str, parts: tuple, terms: list[str]) -> list[Job]:
    if ats == "workday":
        host, site = parts
        return fetch_workday(host, host.split(".")[0], site, terms, f"https://{host}/{site}")
    if ats == "workday_site":
        host, tenant, site = parts
        return fetch_workday(host, tenant, site, terms, f"https://{host}/recruiting/{tenant}/{site}")
    if ats == "greenhouse":
        return fetch_greenhouse(parts[0])
    if ats == "lever":
        return fetch_lever(parts[0])
    if ats == "ashby":
        return fetch_ashby(parts[0])
    if ats == "smartrecruiters":
        return fetch_smartrecruiters(parts[0])
    if ats == "bamboohr":
        return fetch_bamboohr(parts[0])
    if ats == "workable":
        return fetch_workable(parts[0])
    if ats == "jobvite":
        r = http("GET", f"https://jobs.jobvite.com/{parts[0]}/jobs")
        return parse_html_jobs([(r.text, r.url)])
    if ats == "icims":
        return fetch_search(f"https://{parts[0]}/jobs/search?ss=1&searchKeyword={{q}}&in_iframe=1", terms)
    if ats == "ultipro":
        return fetch_ultipro(parts[0], parts[1], parts[2], terms)
    if ats == "paylocity":
        r = http("GET", f"https://recruiting.paylocity.com/recruiting/jobs/All/{parts[0]}")
        return parse_html_jobs([(r.text, r.url)])
    if ats == "adp":
        return fetch_adp(parts[0])
    if ats == "oracle":
        return fetch_oracle(parts[0], parts[1], parts[2], terms)
    if ats == "eightfold":
        host, domain = parts[0], (parts[1] if len(parts) > 1 and parts[1] else None)
        return fetch_eightfold(host, domain or host.split(".")[0] + ".com", terms)
    raise ValueError(f"unknown system {ats}")


# --------------------------------------------------------------------------- one reader per system

def fetch_workday(host: str, tenant: str, site: str, terms: list[str], link_base: str) -> list[Job]:
    """Workday's career pages load their jobs from this JSON endpoint. It returns 20 at a time,
    so instead of paging through thousands of jobs we run one search per term."""
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    jobs: dict[str, Job] = {}
    errors = []
    browser = {"session": None, "headers": {}}

    def post(body):
        try:
            with workday_slot(host):
                return http("POST", api, json=body, session=browser["session"],
                            headers={"Accept": "application/json", **browser["headers"]}).json()
        except RuntimeError as e:
            if browser["session"] is not None or not re.search(r"HTTP (400|403|422)", str(e)):
                raise
            # some Workday sites want the security token a browser receives on its first visit
            session = requests.Session()
            session.get(link_base, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
            browser["session"] = session
            browser["headers"] = {"Origin": f"https://{host}", "Referer": link_base}
            token = session.cookies.get("CALYPSO_CSRF_TOKEN")
            if token:
                browser["headers"]["X-CALYPSO-CSRF-TOKEN"] = token
            with workday_slot(host):
                return http("POST", api, json=body, session=session,
                            headers={"Accept": "application/json", **browser["headers"]}).json()
    for term in (terms or [""]):
      if out_of_time():
          if jobs:
              break          # keep what the first searches found
          raise OutOfTime()
      try:
        offset, total = 0, None
        while offset < WORKDAY_MAX_PER_TERM:
            data = post({"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term})
            if total is None:  # Workday only reports the total on the first page
                total = data.get("total") or 0
            postings = data.get("jobPostings") or []
            for p in postings:
                path = p.get("externalPath") or ""
                if not path:
                    continue
                req = re.search(r"_([A-Za-z0-9]+(?:-\d+)?)$", path)
                jid = f"wd:{tenant}:{req.group(1) if req else path}"
                jobs[jid] = Job(jid, p.get("title", ""), link_base + path, p.get("locationsText", ""),
                                p.get("postedOn", ""), workday_posted(p.get("postedOn", "")),
                                detail=f"https://{host}/wday/cxs/{tenant}/{site}{path}")
            offset += len(postings)
            if not postings or offset >= total:
                break
            time.sleep(0.25)
      except Exception as e:  # one search word failing shouldn't lose the others
        errors.append(e)
    if not jobs and errors:
        raise errors[0]
    return list(jobs.values())


def fetch_greenhouse(board: str) -> list[Job]:
    data = http("GET", f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs").json()
    return [Job(f"gh:{j['id']}", j.get("title", ""), j.get("absolute_url", ""),
                (j.get("location") or {}).get("name", ""),
                posted_at=parse_time(j.get("first_published") or j.get("updated_at")),
                detail=f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{j['id']}")
            for j in data.get("jobs", [])]


def fetch_lever(company: str) -> list[Job]:
    data = http("GET", f"https://api.lever.co/v0/postings/{company}", params={"mode": "json"}).json()
    return [Job(f"lever:{j['id']}", j.get("text", ""), j.get("hostedUrl", ""),
                (j.get("categories") or {}).get("location", ""),
                posted_at=datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc)
                if isinstance(j.get("createdAt"), (int, float)) else None,
                description=" ".join(str(x) for x in walk_strings(
                    [j.get("descriptionPlain"), j.get("lists"), j.get("additionalPlain")])))
            for j in data]


def fetch_ashby(board: str) -> list[Job]:
    data = http("GET", f"https://api.ashbyhq.com/posting-api/job-board/{board}").json()
    return [Job(f"ashby:{j['id']}", j.get("title", ""), j.get("jobUrl", ""), j.get("location", ""),
                posted_at=parse_time(j.get("publishedAt") or j.get("publishedDate")),
                description=j.get("descriptionPlain") or j.get("descriptionHtml") or "")
            for j in data.get("jobs", [])]


def fetch_smartrecruiters(company: str) -> list[Job]:
    jobs, offset = [], 0
    while offset < 2000:
        data = http("GET", f"https://api.smartrecruiters.com/v1/companies/{company}/postings",
                    params={"limit": 100, "offset": offset}).json()
        content = data.get("content") or []
        for j in content:
            loc = j.get("location") or {}
            where = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
            if loc.get("remote"):
                where = f"{where} (remote)".strip()
            jobs.append(Job(f"sr:{j['id']}", j.get("name", ""),
                            f"https://jobs.smartrecruiters.com/{company}/{j['id']}", where,
                            posted_at=parse_time(j.get("releasedDate")),
                            detail=f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{j['id']}"))
        offset += len(content)
        if not content or offset >= (data.get("totalFound") or 0):
            break
    return jobs


def fetch_bamboohr(company: str) -> list[Job]:
    data = http("GET", f"https://{company}.bamboohr.com/careers/list", headers={"Accept": "application/json"}).json()
    jobs = []
    for j in data.get("result") or []:
        loc = j.get("location") or {}
        where = ", ".join(x for x in (loc.get("city"), loc.get("state")) if isinstance(x, str) and x)
        jobs.append(Job(f"bamboo:{company}:{j.get('id')}", j.get("jobOpeningName", ""),
                        f"https://{company}.bamboohr.com/careers/{j.get('id')}", where,
                        detail=f"https://{company}.bamboohr.com/careers/{j.get('id')}/detail"))
    return jobs


def fetch_workable(account: str) -> list[Job]:
    data = http("GET", f"https://apply.workable.com/api/v1/widget/accounts/{account}",
                headers={"Accept": "application/json"}).json()
    jobs = []
    for j in data.get("jobs") or []:
        where = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if isinstance(x, str) and x)
        link = j.get("url") or j.get("application_url") or f"https://apply.workable.com/{account}/j/{j.get('shortcode')}/"
        jobs.append(Job(f"workable:{account}:{j.get('shortcode') or link}", j.get("title", ""), link, where,
                        posted_at=parse_time(j.get("published_on") or j.get("created_at"))))
    return jobs


def fetch_ultipro(host: str, tenant: str, board: str, terms: list[str]) -> list[Job]:
    base = f"https://{host}/{tenant}/JobBoard/{board}"
    jobs = {}
    for term in (terms or [""]):
        body = {"opportunitySearch": {"Top": 50, "Skip": 0, "QueryString": term,
                                      "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate",
                                                   "Ascending": False}],
                                      "Filters": [{"t": "TermsSearchFilterDto", "fieldName": n, "extra": None,
                                                   "values": []} for n in (4, 5, 6)]},
                "matchCriteria": {"PreferredJobs": [], "Educations": [], "LicenseAndCertifications": [],
                                  "Skills": [], "hasNoLicenses": False, "SkippedSkills": []}}
        data = http("POST", f"{base}/JobBoardView/LoadSearchResults", json=body,
                    headers={"Accept": "application/json"}).json()
        for o in data.get("opportunities") or []:
            where = ""
            for place in o.get("Locations") or []:
                addr = (place or {}).get("Address") or {}
                state = addr.get("State")
                state = state.get("Code") if isinstance(state, dict) else state
                where = ", ".join(x for x in (addr.get("City"), state) if isinstance(x, str) and x)
                break
            oid = o.get("Id")
            jobs[oid] = Job(f"ukg:{tenant}:{oid}", o.get("Title", ""), f"{base}/OpportunityDetail?opportunityId={oid}",
                            where, posted_at=parse_time(o.get("PostedDate")))
    return list(jobs.values())


def fetch_adp(cid: str) -> list[Job]:
    url = ("https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
           f"?cid={cid}&timeStamp={int(time.time() * 1000)}&lang=en_US&locale=en_US&$top=200")
    data = http("GET", url, headers={"Accept": "application/json"}).json()
    jobs = []
    for r in data.get("jobRequisitions") or []:
        rid = r.get("itemID") or r.get("requisitionID") or r.get("reqId")
        where = ""
        places = r.get("requisitionLocations") or []
        if places:
            addr = (places[0] or {}).get("address") or {}
            region = addr.get("countrySubdivisionLevel1")
            region = region.get("codeValue") if isinstance(region, dict) else region
            where = ", ".join(x for x in (addr.get("cityName"), region) if isinstance(x, str) and x)
        link = f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}&jobId={rid}&lang=en_US"
        jobs.append(Job(f"adp:{cid}:{rid}", r.get("requisitionTitle", ""), link, where,
                        posted_at=parse_time(r.get("postDate"))))
    return jobs


def fetch_oracle(host: str, lang: str, site: str, terms: list[str]) -> list[Job]:
    jobs = {}
    for term in (terms or [""]):
        finder = f"findReqs;siteNumber={site},limit=50,sortBy=POSTING_DATES_DESC" + (f',keyword="{term}"' if term else "")
        url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
               f"&expand=requisitionList.secondaryLocations&finder={quote(finder, safe='=;,')}")
        data = http("GET", url, headers={"Accept": "application/json"}).json()
        for item in data.get("items") or []:
            for r in item.get("requisitionList") or []:
                rid = r.get("Id")
                jobs[rid] = Job(f"oracle:{host}:{rid}", r.get("Title", ""),
                                f"https://{host}/hcmUI/CandidateExperience/{lang}/sites/{site}/job/{rid}",
                                r.get("PrimaryLocation", ""), posted_at=parse_time(r.get("PostedDate")))
    return list(jobs.values())


def fetch_eightfold(host: str, domain: str, terms: list[str]) -> list[Job]:
    jobs = {}
    for term in (terms or [""]):
        data = http("GET", f"https://{host}/api/apply/v2/jobs", headers={"Accept": "application/json"},
                    params={"domain": domain, "start": 0, "num": 50, "query": term, "sort_by": "timestamp"}).json()
        for p in data.get("positions") or []:
            pid = p.get("id")
            created = p.get("t_create")
            jobs[pid] = Job(f"eightfold:{host}:{pid}", p.get("name", ""),
                            p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{pid}?domain={domain}",
                            p.get("location", ""),
                            posted_at=datetime.fromtimestamp(created, timezone.utc)
                            if isinstance(created, (int, float)) else None)
    return list(jobs.values())


def search_template(url: str) -> str | None:
    """A job-search address with {q} where the search word goes. You can also paste the address
    you get after searching a site for the word "quality" and it is converted automatically."""
    if "{q}" in url:
        return url
    p = urlparse(url)
    pairs = parse_qsl(p.query, keep_blank_values=True)
    if any(v.lower() == "quality" for _, v in pairs):
        query = "&".join(f"{quote(k, safe='')}=" + ("{q}" if v.lower() == "quality" else quote(v, safe=""))
                         for k, v in pairs)
        return urlunparse(p._replace(query=query))
    parts = p.path.split("/")
    if any(x.lower() == "quality" for x in parts):
        return urlunparse(p._replace(path="/".join("{q}" if x.lower() == "quality" else x for x in parts)))
    return None


def fetch_search(template: str, terms: list[str]) -> list[Job]:
    """Run the site's own job search once per search term and read the results pages."""
    pages, last_error = [], None
    for term in (terms or ["quality"]):
        try:
            r = http("GET", prepare_url(template.replace("{q}", quote(term))), tries=1)
        except Exception as e:
            last_error = e
            continue
        pages.append((r.text, r.url))
    if not pages:
        raise last_error or RuntimeError("search page couldn't be loaded")
    return parse_html_jobs(pages)


def phenom_locale(page_html: str, page_url: str) -> str:
    """Phenom sites live under a path like /us/en/ or /global/en/; read it from the site's own links."""
    m = re.search(r"https?://[^/]+/([a-z]{2,8}/[a-z]{2})(?:/|$)", page_url)
    if m:
        return m.group(1)
    host = re.escape(urlparse(page_url).netloc)
    found = (re.findall(rf"https?://{host}/([a-z]{{2,8}}/[a-z]{{2}})/", page_html)
             + re.findall(r'href="/([a-z]{2,8}/[a-z]{2})/', page_html))
    if found:
        return max(set(found), key=found.count)
    return "us/en"


def fetch_phenom(template: str, terms: list[str]) -> list[Job]:
    """Phenom's own job-search feed (what its pages call), falling back to reading the search pages."""
    m = re.match(r"(https?://[^/]+)/([a-z]{2,8})/([a-z]{2})/search-results", template)
    if not m:
        return fetch_search(template, terms)
    origin, country, lang = m.groups()
    found: dict[str, Job] = {}
    for term in (terms or [""]):
        if out_of_time():
            break
        body = {"lang": f"{lang}_{country}", "deviceType": "desktop", "country": country,
                "pageName": "search-results", "ddoKey": "refineSearch", "sortBy": "", "subsearch": "",
                "from": 0, "jobs": True, "counts": False, "all_fields": ["category", "country", "state", "city"],
                "size": 50, "clearAll": False, "jdsource": "facets", "isSliderEnable": False,
                "pageId": "page20", "siteType": "external", "keywords": term, "global": True,
                "selected_fields": {}, "locationData": {}}
        try:
            data = http("POST", f"{origin}/widgets", json=body, tries=1, headers={"Accept": "application/json"}).json()
        except Exception:
            continue
        found.update(jobs_from_hidden_data(data, f"{origin}/{country}/{lang}/"))
    return list(found.values()) or fetch_search(template, terms)


def run_template(template: str, terms: list[str]) -> list[Job]:
    return fetch_phenom(template, terms) if "/search-results?keywords=" in template else fetch_search(template, terms)


def platform_template(page_html: str, page_url: str) -> str | None:
    """Recognize common career-site platforms and return their job-search address."""
    origin = "{0.scheme}://{0.netloc}".format(urlparse(page_url))
    low = page_html.lower()
    if "rmkcdn.successfactors.com" in low or "jobs2web" in low:                       # SAP SuccessFactors
        return f"{origin}/search/?q={{q}}&sortColumn=referencedate&sortDirection=desc"
    if "tbcdn.talentbrew.com" in low or "talentbrew" in low:                          # Radancy (TalentBrew)
        m = (re.search(r"data-company-site-id=[\"'](\d+)", page_html)
             or re.search(r"/job/[^\"'\s<>]+?/(\d{3,6})/\d{5,}", page_html))
        if m:
            return f"{origin}/search-jobs/{{q}}/{m.group(1)}/1"
    if "phenompeople" in low or "phapp.ddo" in low:                                   # Phenom
        return f"{origin}/{phenom_locale(page_html, page_url)}/search-results?keywords={{q}}"
    if "attraxcdn" in low:                                                            # Attrax
        m = re.match(r"https?://[^/]+/([a-z]{2}(?:-[a-z]{2})?)(?:/|$)", page_url)
        return f"{origin}/{m.group(1) if m else 'en'}/jobs?q={{q}}&options=&page=1"
    return None


# --------------------------------------------------------------------------- any other page

def prepare_url(url: str) -> str:
    # iCIMS shows its job list inside a frame; this asks for the list itself
    if "icims.com" in url and "in_iframe=" not in url:
        url += ("&" if "?" in url else "?") + "in_iframe=1"
    return url


def render_pages(url: str) -> list[tuple[str, str]]:
    """Load the page in headless Chrome so JavaScript-built job lists exist. Returns (html, url) per frame."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("render=yes needs Playwright: pip install playwright && "
                           "python -m playwright install chromium")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=USER_AGENT, ignore_https_errors=True)
            page.goto(prepare_url(url), wait_until="domcontentloaded", timeout=30_000)
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            try:
                page.mouse.wheel(0, 4000)  # some job lists only load as you scroll
            except Exception:
                pass
            page.wait_for_timeout(1_500)
            pages = []
            for frame in page.frames:
                if frame.url.startswith("http"):
                    try:
                        pages.append((frame.content(), frame.url))
                    except Exception:
                        pass
            return pages
        finally:
            browser.close()


BROWSER_LOCK = threading.Semaphore(4)  # up to 4 browser pages at once
BROWSER_TERMS = ["quality", "regulatory", "validation"]  # searches run inside the browser


def browser_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


BROWSER_STATS = {"opened": 0, "failed": 0, "error": ""}
_stats_lock = threading.Lock()


def _render_and_count(url: str) -> list[tuple[str, str]]:
    try:
        pages = render_pages(url)
    except Exception as e:
        with _stats_lock:
            BROWSER_STATS["failed"] += 1
            first_line = (str(e).strip().splitlines() or [""])[0]
            BROWSER_STATS["error"] = BROWSER_STATS["error"] or f"{type(e).__name__}: {first_line}"[:200]
        raise
    with _stats_lock:
        BROWSER_STATS["opened"] += 1
    return pages


def browser_render(url: str) -> list[tuple[str, str]]:
    if DEADLINE is None:
        with BROWSER_LOCK:
            return _render_and_count(url)
    remaining = DEADLINE - time.time()
    if remaining <= 0 or not BROWSER_LOCK.acquire(timeout=remaining):
        raise OutOfTime()  # waited for a free browser until the time limit
    try:
        return _render_and_count(url)
    finally:
        BROWSER_LOCK.release()


def render_template(template: str, terms: list[str]) -> list[Job]:
    """Run a site's own job search inside the browser (for search pages built by JavaScript)."""
    pages = []
    for term in terms:
        if out_of_time():
            break
        try:
            pages += browser_render(template.replace("{q}", quote(term)))
        except OutOfTime:
            raise
        except Exception:
            continue
    return parse_html_jobs(pages) if pages else []


def browser_search(rendered: list[tuple[str, str]], from_url: str):
    """If a rendered page is a known career-site platform, search it for your roles in the browser."""
    for page_html, page_url in rendered:
        template = platform_template(page_html, page_url)
        if template:
            jobs = render_template(template, BROWSER_TERMS)
            if looks_like_job_list(jobs):
                return jobs, f"search {template} (browser)", {"from": from_url, "template": template, "render": True}
    return None


def via_browser(urls: list[str], terms: list[str], from_url: str):
    """Open the careers page in a real (headless) browser, like a person would, and look again."""
    tried = []
    for u in urls:
        if not u or u in tried or len(tried) >= 2:
            continue
        tried.append(u)
        try:
            rendered = browser_render(u)
        except OutOfTime:
            raise
        except Exception:
            continue
        if not rendered:
            continue
        result = try_pages(rendered, terms, from_url) or via_job_posting(rendered, terms, from_url)
        if result:
            return result
        jobs = parse_html_jobs(rendered)
        if looks_like_job_list(jobs):
            return jobs, f"page {u} (browser)", {"from": from_url, "page": u, "v": 2, "render": True}
        result = browser_search(rendered, from_url)
        if result:
            return result
        for link in career_links(rendered[0][0], rendered[0][1], limit=2):  # e.g. its "Search jobs" page
            found = detect_ats(link)
            if found:
                try:
                    jobs = fetch_from_ats(found[0], found[1], terms)
                    if jobs:
                        return jobs, f"{label(found[0])} (found via {link})", \
                            {"from": from_url, "ats": found[0], "parts": list(found[1])}
                except Exception:
                    pass
                continue
            try:
                sub = browser_render(link)
            except OutOfTime:
                raise
            except Exception:
                continue
            result = (try_pages(sub, terms, from_url) or via_job_posting(sub, terms, from_url)
                      or browser_search(sub, from_url))
            if result:
                return result
            jobs = parse_html_jobs(sub)
            if looks_like_job_list(jobs):
                return jobs, f"page {link} (browser)", {"from": from_url, "page": link, "v": 2, "render": True}
        found, via, crawled = follow_career_links(rendered[0], hops=1)
        if found:
            try:
                jobs = fetch_from_ats(found[0], found[1], terms)
                if jobs:
                    return jobs, f"{label(found[0])} (found via {via})", \
                        {"from": from_url, "ats": found[0], "parts": list(found[1])}
            except Exception:
                pass
        result = try_pages(crawled, terms, from_url) or via_job_posting(crawled, terms, from_url)
        if result:
            return result
    return None


def walk_strings(data):
    """Every piece of text inside a JSON answer, in order."""
    if isinstance(data, str):
        yield data
    elif isinstance(data, dict):
        for value in data.values():
            yield from walk_strings(value)
    elif isinstance(data, list):
        for value in data:
            yield from walk_strings(value)


def walk_json(data):
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from walk_json(value)
    elif isinstance(data, list):
        for value in data:
            yield from walk_json(value)


def ld_location(item: dict) -> str:
    places = item.get("jobLocation") or []
    if isinstance(places, dict):
        places = [places]
    out = []
    for place in places:
        addr = (place or {}).get("address") if isinstance(place, dict) else None
        if isinstance(addr, dict):
            bits = [addr.get("addressLocality"), addr.get("addressRegion")]
            out.append(", ".join(b for b in bits if isinstance(b, str) and b))
    return "; ".join(o for o in out if o)


JOB_ID_KEYS = ("jobSeqNo", "jobId", "JobId", "jobID", "reqId", "requisitionId", "RequisitionId", "jobReqId",
               "postingId", "jobPostingId", "itemID", "positionId", "jobCode")
JOB_TITLE_KEYS = ("title", "jobTitle", "JobTitle", "Title", "requisitionTitle", "postingTitle", "name")
JOB_URL_KEYS = ("applyUrl", "jobUrl", "url", "canonicalPositionUrl", "absolute_url", "hostedUrl", "jobDetailUrl",
                "externalUrl", "link", "href")
JOB_LOC_KEYS = ("location", "cityStateCountry", "locationsText", "LocationName", "Location", "primaryLocation",
                "PrimaryLocation", "city", "locations")
JOB_DATE_KEYS = ("postedDate", "PostedDate", "datePosted", "postingDate", "PublishedDate", "publishedDate",
                 "releasedDate", "createdDate", "dateCreated")
HIDDEN_DATA_STARTS = [re.compile(x) for x in (r"phApp\.ddo\s*=\s*", r"window\.pageData\s*=\s*",
                                               r"window\.__INITIAL_STATE__\s*=\s*", r"window\.__PRELOADED_STATE__\s*=\s*")]


def hidden_data(page_html: str, soup) -> list:
    """Job lists that sites embed in the page for their JavaScript to draw."""
    blobs = []
    for script in soup.find_all("script"):
        kind = (script.get("type") or "").lower()
        if script.get("id") == "__NEXT_DATA__" or kind == "application/json":
            try:
                blobs.append(json.loads(script.string or ""))
            except (json.JSONDecodeError, TypeError):
                pass
    decoder = json.JSONDecoder()
    for start in HIDDEN_DATA_STARTS:
        for m in start.finditer(page_html):
            if m.end() < len(page_html) and page_html[m.end()] in "{[":
                try:
                    blobs.append(decoder.raw_decode(page_html, m.end())[0])
                except ValueError:
                    pass
    return blobs


def place_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return ", ".join(value[k] for k in ("city", "City", "cityName", "state", "State", "stateCode", "country", "Country")
                         if isinstance(value.get(k), str) and value[k])
    if isinstance(value, list) and value:
        return place_text(value[0])
    return ""


def jobs_from_hidden_data(blob, base: str) -> dict:
    found = {}
    for d in walk_json(blob):
        id_key = next((k for k in JOB_ID_KEYS if d.get(k) not in (None, "", [], {})), None)
        title = next((d[k] for k in JOB_TITLE_KEYS if isinstance(d.get(k), str) and 2 < len(d[k]) <= 150), None)
        if not id_key or not title:
            continue
        jid = str(d[id_key])
        link = next((d[k] for k in JOB_URL_KEYS if isinstance(d.get(k), str) and d[k].strip()
                     and not d[k].strip().startswith(("#", "javascript:"))), "")
        if link:
            link = urljoin(base, link.strip())
        elif id_key == "jobSeqNo":  # Phenom job pages live at /<country>/<language>/job/<jobSeqNo>
            m = re.match(r"(https?://[^/]+/(?:[a-z]{2,8}/[a-z]{2}/)?)", base)
            link = f"{m.group(1)}job/{jid}" if m else base
        elif "paylocity.com" in base:
            link = f"https://recruiting.paylocity.com/Recruiting/Jobs/Details/{jid}"
        else:
            link = base
        when = next((d[k] for k in JOB_DATE_KEYS if d.get(k)), None)
        if isinstance(when, (int, float)):
            posted = datetime.fromtimestamp(when / 1000 if when > 1e11 else when, timezone.utc)
        else:
            posted = parse_time(when)
        where = next((place_text(d[k]) for k in JOB_LOC_KEYS if d.get(k)), "")
        key = "data:" + (link if link != base else f"{base}#{jid}")
        found[key] = Job(key, title, link, where, posted_at=posted)
    return found


def card_text(a, limit: int = 400) -> str:
    """The text of the 'job card' around a link: usually the title, location and date."""
    node, text, own = a, clean(a.get_text(" ")), a.get("href")
    for _ in range(4):
        parent = node.parent
        if parent is None or parent.name in ("[document]", "body", "html", "main", "ul", "ol", "table", "tbody"):
            break
        if any(x.get("href") != own for x in parent.find_all("a", href=True)):
            break  # the next level up holds other jobs too
        t = clean(parent.get_text(" "))
        if len(t) > limit:
            break
        node, text = parent, t
    return text


def parse_html_jobs(pages: list[tuple[str, str]]) -> list[Job]:
    """Turn a career page into candidate jobs: job data hidden in the page, structured job data,
    plus every link on the page. The title filter later keeps only the real matches."""
    jobs: dict[str, Job] = {}
    for page_html, base in pages:
        soup = BeautifulSoup(page_html, "html.parser")
        for blob in hidden_data(page_html, soup):
            jobs.update(jobs_from_hidden_data(blob, base))
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            for item in walk_json(data):
                kind = item.get("@type")
                if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                    link = urljoin(base, item.get("url") or "") if item.get("url") else base
                    jid = "web:" + (link if item.get("url") else clean(item.get("title")))
                    jobs[jid] = Job(jid, item.get("title", ""), link, ld_location(item),
                                    posted_at=parse_time(item.get("datePosted")),
                                    description=str(item.get("description") or ""))
        for a in soup.find_all("a", href=True):
            text = clean(a.get_text(" "))
            href = a["href"].strip()
            if not text or len(text) > 200 or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            link = urljoin(base, href)
            if "web:" + link not in jobs:
                jobs["web:" + link] = Job("web:" + link, text, link, context=f"{card_text(a)} {link}")
    return list(jobs.values())


STRONG_CAREER = re.compile(r"career|\bjobs?\b|job[\s-]+search|openings|open[\s-]+(?:positions|roles)"
                           r"|search[\s-]+(?:all[\s-]+)?jobs|view[\s-]+(?:all[\s-]+)?(?:jobs|positions|roles)"
                           r"|vacanc|hiring|join[\s-]+our[\s-]+team", re.I)
WEAK_CAREER = re.compile(r"join[\s-]+(?:us|the[\s-]+team)|work[\s-]+(?:with|at|for)[\s-]+us"
                         r"|(?:career|job|employment)[\s-]+opportunit|\bpositions\b", re.I)
NOT_CAREERS = ("linkedin.", "facebook.", "twitter.", "//x.com", "instagram.", "youtube.", "glassdoor.",
               "indeed.", "tiktok.", "ziprecruiter.", ".pdf")


def career_links(page_html: str, base: str, limit: int = 4) -> list[str]:
    """Links that look like they lead to careers / job openings, best guesses first."""
    soup = BeautifulSoup(page_html, "html.parser")
    here = base.split("#")[0].rstrip("/")
    scores: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"].strip()).split("#")[0]
        if not href.startswith("http") or href.rstrip("/") == here or any(x in href.lower() for x in NOT_CAREERS):
            continue
        text, path = clean(a.get_text(" ")), urlparse(href).netloc + urlparse(href).path
        score = (4 if STRONG_CAREER.search(text) else 3 if STRONG_CAREER.search(path)
                 else 2 if WEAK_CAREER.search(text) else 1 if WEAK_CAREER.search(path) else 0)
        if score > scores.get(href, 0):
            scores[href] = score
    ranked = sorted((h for h in scores if scores[h]), key=lambda h: -scores[h])  # stable: page order within a score
    return ranked[:limit]


def follow_career_links(start: tuple[str, str], hops: int, max_pages: int = 6):
    """From a homepage or careers page, follow careers-looking links until a known job system turns up.
    Returns (system found or None, the link it was found through, pages fetched on the way)."""
    frontier, pages, visited = [start], [], {start[1].split("#")[0].rstrip("/")}
    for _ in range(hops):
        next_frontier = []
        for page_html, base in frontier:
            for link in career_links(page_html, base):
                if link.rstrip("/") in visited:
                    continue
                visited.add(link.rstrip("/"))
                found = detect_ats(link)
                if found:
                    return found, link, pages
                if len(pages) >= max_pages:
                    return None, None, pages
                try:
                    r = http("GET", prepare_url(link), tries=1, timeout=10)
                except Exception:
                    continue
                page = (r.text, r.url)
                pages.append(page)
                next_frontier.append(page)
                found = detect_ats(r.url) or detect_ats(r.text)
                if found:
                    return found, link, pages
        frontier = next_frontier
        if not frontier:
            break
    return None, None, pages


def label(ats: str) -> str:
    return ats.replace("_site", "")


def try_pages(pages: list[tuple[str, str]], terms: list[str], from_url: str):
    """Look for a known job system or career-site platform on these pages and read its jobs."""
    for page_html, page_url in pages:
        found = detect_ats(page_url) or detect_ats(page_html)
        if found:
            try:
                jobs = fetch_from_ats(found[0], found[1], terms)
                if jobs:
                    return jobs, f"{label(found[0])} (found via {page_url})", \
                        {"from": from_url, "ats": found[0], "parts": list(found[1])}
            except Exception:
                pass
        template = platform_template(page_html, page_url)
        if template:
            try:
                jobs = run_template(template, terms)
                if jobs and looks_like_job_list(jobs):
                    return jobs, f"search {template}", {"from": from_url, "template": template}
            except Exception:
                pass
    return None


JOB_LINK_RE = re.compile(r"/(?:jobs?|job-details|opportunit(?:y|ies)|requisitions?|positions?)/[^?#\s]*?"
                         r"(?:\d{3,}|r-\d+)|/apply/[A-Za-z0-9]{8,}/", re.I)  # second part: JazzHR (applytojob.com)


def job_links(page_html: str, base: str, limit: int = 2) -> list[str]:
    """Links that look like individual job postings."""
    out = []
    for a in BeautifulSoup(page_html, "html.parser").find_all("a", href=True):
        href = urljoin(base, a["href"].strip()).split("#")[0]
        if not href.startswith("http"):
            continue
        if JOB_LINK_RE.search(urlparse(href).path) or re.search(r"[?&](?:jobId|job_id|reqId|requisitionId|jid)=", href, re.I):
            if href not in out:
                out.append(href)
        if len(out) >= limit:
            break
    return out


def via_job_posting(pages: list[tuple[str, str]], terms: list[str], from_url: str):
    """Open a job posting or two and see which job system its Apply button leads to."""
    for page_html, page_url in pages:
        for link in job_links(page_html, page_url):
            found = detect_ats(link)
            if not found:
                try:
                    r = http("GET", link, tries=1, timeout=10)
                except Exception:
                    continue
                found = detect_ats(r.url) or detect_ats(r.text)
            if found:
                try:
                    jobs = fetch_from_ats(found[0], found[1], terms)
                    if jobs:
                        return jobs, f"{label(found[0])} (found through a job posting: {link})", \
                            {"from": from_url, "ats": found[0], "parts": list(found[1])}
                except Exception:
                    pass
    return None


def open_start_page(start: str, render: bool, allow_alternatives: bool):
    """Load the starting page. If a homepage blocks us, try the company's careers addresses instead."""
    try:
        if render:
            return render_pages(start)
        r = http("GET", prepare_url(start))
        return [(r.text, r.url)]
    except Exception as first_error:
        if not allow_alternatives:
            raise
        domain = urlparse(start).netloc.lower()
        domain = domain[4:] if domain.startswith("www.") else domain
        for alt in (f"https://careers.{domain}", f"https://career.{domain}", f"https://jobs.{domain}",
                    f"https://www.{domain}/careers", f"https://{domain}/careers"):
            try:
                r = http("GET", alt, tries=1, timeout=10)
                return [(r.text, r.url)]
            except Exception:
                continue
        raise first_error


def fetch_company(c: dict, cfg: dict, saved: dict | None = None):
    """Returns (jobs, where they came from, the address to remember for next time or None)."""
    terms, url = cfg["search_terms"], c["url"]
    found = detect_ats(url)
    if found:
        return fetch_from_ats(found[0], found[1], terms), label(found[0]), None
    template = search_template(url)
    if template:
        jobs = run_template(template, terms)
        if (not looks_like_job_list(jobs) and not c["render"] and cfg.get("browser_fallback", True)
                and browser_available()):
            if out_of_time():
                raise OutOfTime()
            rendered_jobs = render_template(template, BROWSER_TERMS)
            if looks_like_job_list(rendered_jobs):
                return rendered_jobs, f"search {template} (browser)", None
        return jobs, f"search {template}", None
    saved = saved if saved and saved.get("from") == url else None
    if saved and saved.get("page") and saved.get("v") != 2:
        saved = None  # careers page picked by an older version: look again with the better link ranking
    if saved:  # skip the homepage search: go straight to the job board found last time
        try:
            if saved.get("ats"):
                jobs = fetch_from_ats(saved["ats"], tuple(saved["parts"]), terms)
                if jobs:
                    return jobs, f"{label(saved['ats'])} (saved address)", saved
            elif saved.get("template"):
                jobs = (render_template(saved["template"], BROWSER_TERMS)
                        if saved.get("render") and browser_available() else run_template(saved["template"], terms))
                if jobs and looks_like_job_list(jobs):
                    return jobs, f"search {saved['template']} (saved address)", saved
        except Exception:
            pass  # the saved address stopped working, so look for it again
    start = saved["page"] if saved and saved.get("page") else url
    homepage = start == url and urlparse(url).path.strip("/") == ""
    render = c["render"] or bool(saved and saved.get("render") and browser_available())
    first = open_start_page(start, render, allow_alternatives=homepage)
    result = try_pages(first, terms, url)
    if result:
        return result
    found, via, crawled = follow_career_links(first[0], hops=2 if homepage else 1)
    if found:
        try:
            jobs = fetch_from_ats(found[0], found[1], terms)
            if jobs:
                return jobs, f"{label(found[0])} (found via {via})", \
                    {"from": url, "ats": found[0], "parts": list(found[1])}
        except Exception:
            pass
    result = try_pages(crawled, terms, url) or via_job_posting(first + crawled, terms, url)
    if result:
        return result
    if homepage and crawled:  # read the most job-like careers page we reached, not the homepage
        best = max(crawled, key=lambda page: len(job_links(page[0], page[1], limit=50)))
        jobs, source, careers_page = parse_html_jobs([best]), f"page {best[1]}", best[1]
        remember = None if c["render"] else {"from": url, "page": best[1], "v": 2}
    else:
        jobs, source, careers_page = parse_html_jobs(first), f"page {first[0][1]}", first[0][1]
        remember = saved if saved and saved.get("page") else None
        if render:
            source += " (browser)"
    if (not render and not looks_like_job_list(jobs) and cfg.get("browser_fallback", True)
            and browser_available()):
        if out_of_time():
            raise OutOfTime()
        result = via_browser([careers_page, first[0][1], start], terms, url)
        if result:
            return result
    return jobs, source, remember


def safe_fetch(c: dict, cfg: dict, saved: dict | None = None):
    try:
        jobs, source, remember = fetch_company(c, cfg, saved)
        return jobs, source, None, remember
    except OutOfTime:
        return [], POSTPONED, None, saved
    except Exception as e:
        return [], "", f"{type(e).__name__}: {e}"[:300], saved


POSTPONED = "not checked (run time limit; next run)"


def fetch_all(companies: list[dict], cfg: dict, saved_addresses: dict, deadline: float | None = None) -> dict:
    results, total = {}, len(companies)

    def timed(c):
        if deadline and time.time() > deadline:  # out of time: leave it for the next run
            return c["name"], ([], POSTPONED, None, None), 0.0
        started = time.time()
        return c["name"], safe_fetch(c, cfg, saved_addresses.get(c["name"])), time.time() - started

    def report(name, result, secs):
        results[name] = result
        status = ("postponed to the next run" if result[1] == POSTPONED else
                  "error" if result[2] else f"{len(result[0])} postings/links")
        print(f"  [{len(results)}/{total}] {name}: {status} ({secs:.0f}s)", flush=True)

    plain = [c for c in companies if not c["render"]]
    with ThreadPoolExecutor(max_workers=max(1, int(cfg["workers"]))) as pool:
        for future in as_completed([pool.submit(timed, c) for c in plain]):
            report(*future.result())
    for c in companies:  # headless-browser pages one at a time
        if c["render"]:
            report(*timed(c))
    return results


# --------------------------------------------------------------------------- reading job descriptions

NUMBER_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15}
_NUM = r"(\d{1,2}|" + "|".join(NUMBER_WORDS) + r")"
YEARS_RE = re.compile(  # inside a lookahead, so "5 years or Master's + 3 years of experience" yields both 5 and 3
    rf"(?=\b{_NUM}\s*(?:\+|plus|or\s+more)?\s*(?:(?:-|–|—|to)\s*{_NUM}\s*)?\+?\s*(?:years?|yrs?)\b"
    r"(?:\s*\([^)]*\))?[^.;]{0,60}?\b(?:experience|work|working|industry|environment|role|setting))", re.I)
NO_SPONSOR_RE = [
    re.compile(r"\b(?:no|not|unable|cannot|can't|won't|will\s+not|does\s+not|doesn't|do\s+not|don't|is\s+not|are\s+not)"
               r"\b[^.;]{0,60}\bsponsor", re.I),
    re.compile(r"\bsponsor\w*\b[^.;]{0,50}\b(?:not\s+(?:be\s+)?(?:available|offered|provided|considered|eligible|possible)"
               r"|unavailable|will\s+not)\b", re.I),
    re.compile(r"\bwithout\s+(?:the\s+)?(?:need\s+(?:for\s+)?|requiring\s+)?(?:current\s+or\s+future\s+|present\s+or\s+future\s+)?"
               r"(?:employer\s+|company\s+|visa\s+|immigration\s+|work\s+)?sponsor", re.I),
    re.compile(r"\b(?:must\s+be\s+(?:an?\s+)?|only\s+)(?:US|United\s+States)\s+citizen|\b(?:US|United\s+States)\s+"
               r"citizen(?:s|ship)?\s+(?:only|is\s+required|required)|\bsecurity\s+clearance\b|\bITAR\b", re.I),
]
YES_SPONSOR_RE = re.compile(r"\b(?:visa|H-?1B|immigration)\s+sponsorship\s+(?:is\s+|may\s+be\s+|will\s+be\s+)?"
                            r"(?:available|offered|provided|considered)|\b(?:will|can|able\s+to)\s+sponsor\b"
                            r"|\bsponsorship\s+(?:is\s+)?available\b", re.I)


def html_to_text(value: str) -> str:
    text = BeautifulSoup(html.unescape(value or ""), "html.parser").get_text(" ")
    return " ".join(text.split())


def job_text(job: Job) -> str:
    """Full description: from the listing if it came with it, else the job system, else the job page."""
    if job.description:
        return html_to_text(job.description)
    if job.detail:
        try:
            data = http("GET", job.detail, tries=1, timeout=15, headers={"Accept": "application/json"}).json()
            info = data.get("jobPostingInfo") if isinstance(data, dict) else None  # Workday
            if isinstance(info, dict):
                country = info.get("country")
                job.country = clean(country.get("descriptor") if isinstance(country, dict) else country or "")
                if not job.location or re.fullmatch(r"\d+ Locations?", job.location):
                    places = [info.get("location")] + list(info.get("additionalLocations") or [])
                    job.location = clean("; ".join(x for x in places if isinstance(x, str) and x)) or job.location
            return html_to_text(" ".join(walk_strings(data)))
        except Exception:
            pass
    if job.url:
        try:
            return html_to_text(http("GET", job.url, tries=1, timeout=15).text)
        except Exception:
            pass
    return ""


def read_description(job: Job):
    """Fill in job.sponsor, job.sponsor_note and job.years from the description."""
    text = job_text(job)
    if not text:
        return
    text = re.sub(r"\bU\.S\.A?\.?", "US", text)  # so "U.S." doesn't end a sentence
    job.text = text[:30000]
    for pattern in NO_SPONSOR_RE:
        m = pattern.search(text)
        if m:
            job.sponsor, job.sponsor_note = "no", sentence_around(text, m.start(), m.end())
            break
    else:
        m = YES_SPONSOR_RE.search(text)
        if m:
            job.sponsor, job.sponsor_note = "yes", sentence_around(text, m.start(), m.end())
    lows = []
    for m in YEARS_RE.finditer(text):
        word = m.group(1).lower()
        low = int(word) if word.isdigit() else NUMBER_WORDS.get(word)
        if low is not None and low <= 20:
            lows.append(low)
    if lows:
        job.years = f"{min(lows)}+ yrs"  # lowest asked, e.g. "Bachelor's + 5 yrs or Master's + 3 yrs" -> 3+


# --------------------------------------------------------------------------- final checks on new jobs

US_STATE_NAMES = ("Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|"
                  "Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|"
                  "Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|"
                  "New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|"
                  "South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|"
                  "Wisconsin|Wyoming|District of Columbia|Puerto Rico")
US_IN_LOCATION = re.compile(
    rf"\b(?:{US_STATE_NAMES})\b|\bUnited States\b|\bUSA\b|(?<![A-Za-z])US(?![A-Za-z])"
    r"|(?:,|\s-|\(|\s)\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|"
    r"NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|PR)(?![A-Za-z])")
US_IN_DESCRIPTION = re.compile(rf"\b(?:{US_STATE_NAMES})\b|\bUnited States\b|E-Verify|protected veteran|"
                               r"Equal (?:Employment )?Opportunity Employer|\bEEO\b|"
                               r"(?:base )?(?:salary|pay) range[^.]{0,40}\$|\$\s?\d{2,3},\d{3}", re.I)
SOFTWARE_SIGNS = re.compile(r"selenium|cypress|playwright|appium|postman|jmeter|test automation|automation framework|"
                            r"regression testing|unit test|api testing|\bSDET\b|javascript|typescript|\bjava\b|"
                            r"\bc#|\.net\b|react|agile|scrum|ci/cd|jenkins|mobile app|web app", re.I)
GMP_SIGNS = re.compile(r"\bc?GMPs?\b|\bGxP\b|batch record|\bCAPA\b|deviations?\b|change control|21 CFR|"
                       r"Part 11|\bSOPs?\b|\bIQ\b|\bOQ\b|\bPQ\b|quality management system|\bQMS\b", re.I)


US_CITIES = re.compile(r"\b(?:Boston|Cambridge|Waltham|Lexington|Woburn|Bedford|Burlington|Framingham|Marlborough|"
    r"Norwood|Andover|Devens|Beverly|Worcester|Watertown|Somerville|Rensselaer|Tarrytown|Indianapolis|Thousand Oaks|"
    r"South San Francisco|San Francisco|San Diego|Carlsbad|Oceanside|Princeton|Rahway|Raritan|Titusville|"
    r"New Brunswick|Plainsboro|Bridgewater|Parsippany|Basking Ridge|Kalamazoo|Durham|Research Triangle|Raleigh|"
    r"Morrisville|Holly Springs|Sanford|Rocky Mount|Philadelphia|King of Prussia|Malvern|Exton|West Point|"
    r"Pittsburgh|Rockville|Gaithersburg|Frederick|Baltimore|Bothell|Seattle|Houston|Sugar Land|College Station|"
    r"North Chicago|Chicago|Libertyville|Round Lake|Minneapolis|St\.? Louis|Columbus|Cincinnati|Denver|Boulder|"
    r"Longmont|Salt Lake City|Irvine|Los Angeles|Vacaville|Juncos|New York|Brooklyn|Rochester)\b", re.I)


def us_status(job: Job, f: dict) -> str:
    """'us', 'foreign' or 'unknown', from the country, then the location, then the description."""
    if job.country:
        return "us" if re.search(r"United States|\bUSA?\b", job.country) else "foreign"
    loc = "" if re.fullmatch(r"\d+ Locations?", job.location or "") else job.location
    if loc:
        if any(p.search(loc) for p in f["location_exclude"]):  # first, so "Bengaluru, IN" isn't read as Indiana
            return "foreign"
        if US_IN_LOCATION.search(loc) or US_CITIES.search(loc):
            return "us"
    if job.text:
        us_signs = US_IN_DESCRIPTION.search(job.text)
        foreign_signs = any(p.search(job.text[:4000]) for p in f["location_exclude"])
        if us_signs:
            return "us"
        if foreign_signs:
            return "foreign"
    return "unknown"


def skip_reason(job: Job, f: dict, cfg: dict) -> str:
    """Why a new job shouldn't be sent after reading its description ('' = send it)."""
    if cfg.get("us_only", True):
        status = us_status(job, f)
        if status == "foreign":
            return "outside the US"
        if status == "unknown" and job.text:  # read the description, and nothing in it says US
            return "location not confirmed as US"
    max_years = cfg.get("max_years_experience")
    if max_years is not None and job.years and int(job.years.split("+")[0]) > int(max_years):
        return f"asks {job.years}"
    if re.search(r"\bQA\b|quality assurance|quality engineer|validation", job.title, re.I) and job.text:
        software = len(set(m.group(0).lower() for m in SOFTWARE_SIGNS.finditer(job.text)))
        if software >= 3 and not GMP_SIGNS.search(job.text):
            return "software testing role"
    return ""


def sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind(";", 0, start)) + 1
    right_candidates = [i for i in (text.find(".", end), text.find(";", end)) if i != -1]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    sentence = text[left:right].strip()
    return sentence if len(sentence) <= 220 else sentence[:217] + "..."


def read_descriptions(jobs: list[Job], workers: int = 8):
    if not jobs:
        return
    print(f"Reading {len(jobs)} job description(s)...", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(read_description, jobs))


def flags(job: Job) -> str:
    out = []
    if job.sponsor == "no":
        out.append("⚠ says no visa sponsorship")
    elif job.sponsor == "yes":
        out.append("mentions visa sponsorship")
    if job.years:
        out.append(f"asks {job.years}")
    return " | ".join(out)


# --------------------------------------------------------------------------- Adzuna (second source)

NAME_FILLER = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "the", "and", "of", "plc", "ag", "sa",
               "nv", "gmbh", "us", "usa", "america", "americas", "north", "group", "holdings", "pharmaceuticals",
               "pharmaceutical", "pharma", "therapeutics", "biosciences", "bioscience", "biotherapeutics", "biotech",
               "biopharma", "biopharmaceuticals", "biologics", "laboratories", "labs", "sciences", "life", "medicines",
               "global", "international", "services"}


def name_tokens(name: str) -> set[str]:
    name = re.sub(r"\([^)]*\)", " ", name.lower().replace("&", " "))
    return {w for w in re.findall(r"[a-z0-9]+", name) if w not in NAME_FILLER} or set(re.findall(r"[a-z0-9]+", name))


def adzuna_company_index(companies: list[dict]) -> list[tuple[set[str], str]]:
    return sorted(((name_tokens(c["name"]), c["name"]) for c in companies), key=lambda x: -len(x[0]))


def match_company(employer: str, index) -> str | None:
    words = name_tokens(employer) | set(re.findall(r"[a-z0-9]+", employer.lower()))
    for tokens, name in index:  # most specific names first
        if tokens and tokens <= words:
            return name
    return None


def fetch_adzuna(cfg: dict, companies: list[dict]) -> list[tuple[str, Job]]:
    """Search Adzuna (which collects postings from company career sites and job boards) by job title, US-wide."""
    app_id, app_key = os.environ.get("ADZUNA_APP_ID", "").strip(), os.environ.get("ADZUNA_APP_KEY", "").strip()
    if not app_id or not app_key:
        return []
    index = adzuna_company_index(companies)
    any_company = bool(cfg.get("adzuna_any_company", False))
    queries = list(cfg.get("adzuna_queries") or [])[: int(cfg.get("adzuna_max_calls_per_run", 8))]
    found: dict[str, tuple[str, Job]] = {}
    calls, problems = 0, []
    for q in queries:
        try:
            data = http("GET", "https://api.adzuna.com/v1/api/jobs/us/search/1", tries=2, params={
                "app_id": app_id, "app_key": app_key, "what": q, "results_per_page": 50, "sort_by": "date",
                "max_days_old": int(cfg.get("adzuna_days", 1)), "content-type": "application/json"}).json()
            calls += 1
        except Exception as e:
            problems.append(str(e)[:120])
            continue
        for r in data.get("results") or []:
            employer = clean(html_to_text((r.get("company") or {}).get("display_name", "")))
            name = match_company(employer, index) or (employer if any_company and employer else None)
            if not name:
                continue
            area = (r.get("location") or {}).get("area") or []
            job = Job(f"adzuna:{r.get('id')}", html_to_text(r.get("title", "")), r.get("redirect_url", ""),
                      clean((r.get("location") or {}).get("display_name", "")),
                      posted_at=parse_time(r.get("created")), description=r.get("description") or "")
            if area and area[0] == "US":
                job.country = "United States"
                if len(area) > 1 and area[1] not in job.location:
                    job.location = f"{job.location}, {area[1]}".strip(", ")
            found[job.id] = (name, job)
    print(f"Adzuna: {calls} search(es), {len(found)} posting(s) from " +
          ("any employer" if any_company else "companies on your list") +
          (f"; problems: {problems[0]}" if problems else ""))
    return list(found.values())


# --------------------------------------------------------------------------- matching

def term_regex(term: str) -> re.Pattern:
    # whole words only, any case, plural ok; a space also matches "-" or "/"
    # ("quality system" ~ "Quality-Systems", "complaint" ~ "Complaints")
    body = r"[\s\-/]+".join(re.escape(w) for w in term.split())
    return re.compile(rf"(?<![A-Za-z0-9]){body}s?(?![A-Za-z0-9])", re.I)


def build_filters(cfg: dict) -> dict:
    return {key: [term_regex(t) for t in cfg[key] if t.strip()]
            for key in ("include", "exclude", "location_include", "location_exclude")}


def plain_title(job: Job) -> str:
    return re.sub(r"[^a-z0-9]+", " ", job.title.lower()).strip()


def url_key(url: str) -> str:
    """The job's address without tracking bits, so the same posting matches under any company name."""
    p = urlparse(url or "")
    keep = sorted((k, v) for k, v in parse_qsl(p.query) if re.search(r"id|job|req", k, re.I))
    return (p.netloc.lower().removeprefix("www.") + p.path.rstrip("/").lower()
            + ("?" + "&".join(f"{k}={v}" for k, v in keep) if keep else ""))


def title_key(job: Job) -> str:
    """Same title at the same location counts as the same job, even under a new job ID."""
    return re.sub(r"[^a-z0-9]+", " ", f"{job.title} | {job.location}".lower()).strip()


def matches(job: Job, f: dict) -> bool:
    if not any(p.search(job.title) for p in f["include"]):
        return False
    if any(p.search(job.title) for p in f["exclude"]):
        return False
    loc = job.location
    if not loc or re.fullmatch(r"\d+ Locations?", loc):  # unknown location: check the job card, else keep it
        return not (job.context and any(p.search(job.context) for p in f["location_exclude"]))
    if any(p.search(loc) for p in f["location_exclude"]):
        return False
    if f["location_include"] and not any(p.search(loc) for p in f["location_include"]):
        return False
    return True


# --------------------------------------------------------------------------- files

def load_env_file():
    """For running on your own computer: read KEY=value lines from .env (never committed)."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit("config.toml not found next to job_watch.py")
    with open(CONFIG_FILE, "rb") as f:
        data = tomllib.load(f)
    cfg = {"include": [], "exclude": [], "search_terms": [], "location_include": [],
           "location_exclude": [], "workers": 8, **data}
    if not cfg["include"]:
        sys.exit("config.toml: 'include' needs at least one keyword")
    return cfg


def load_companies(only: str | None = None) -> list[dict]:
    if not COMPANIES_FILE.exists():
        sys.exit("companies.csv not found next to job_watch.py")
    with open(COMPANIES_FILE, newline="", encoding="utf-8-sig", errors="replace") as f:
        rows = list(csv.DictReader(f))
    companies, names = [], set()
    for line_no, row in enumerate(rows, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        name, url = row.get("company", ""), row.get("url", "")
        if not name or name.startswith("#"):
            continue
        if not url.lower().startswith("http"):
            print(f"companies.csv line {line_no}: skipping {name!r} because its url doesn't start with http")
            continue
        if name.lower() in names:
            print(f"companies.csv line {line_no}: skipping duplicate company name {name!r}")
            continue
        names.add(name.lower())
        priority = row.get("priority", "").lower()
        companies.append({"name": name, "url": url, "group": row.get("group", ""),
                          "render": row.get("render", "").lower() in ("yes", "y", "true", "1"),
                          "priority": ("medium" if priority in ("medium", "mid", "2")
                                       else "normal" if priority in ("normal", "low", "3", "twice a day", "2x")
                                       else "high")})
    if only:
        companies = [c for c in companies if c["name"].lower() == only.lower()]
        if not companies:
            sys.exit(f"No company named {only!r} in companies.csv")
    return companies


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


LOG_COLUMNS = ["Date found", "Company", "Title", "Location", "Posted", "Visa sponsorship",
               "Sponsorship wording", "Experience asked", "Link", "Applied", "Notes"]
PUBLIC_LOG_COLUMNS = ["Date found", "Company", "Title", "Location", "Posted", "Experience asked", "Link",
                      "Applied", "Notes"]  # no sponsorship columns: those stay in your emails


def append_log(items: list[tuple[str, Job]], stamp: str, with_sponsorship: bool = True):
    """Add every sent job to jobs_log.csv (opens in Excel). Creates the file with headers if needed."""
    new_file = not LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="", encoding="utf-8-sig" if new_file else "utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(LOG_COLUMNS if with_sponsorship else PUBLIC_LOG_COLUMNS)
        for name, j in items:
            sponsor = {"no": "Says no", "yes": "Mentions yes"}.get(j.sponsor, "Not mentioned")
            if with_sponsorship:
                writer.writerow([stamp[:10], name, j.title, j.location, age_text(j), sponsor,
                                 j.sponsor_note, j.years, j.url, "", ""])
            else:
                writer.writerow([stamp[:10], name, j.title, j.location, age_text(j), j.years, j.url, "", ""])


def save_state(state: dict):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


# --------------------------------------------------------------------------- notifications

def friendly_error(error: str) -> str:
    m = re.search(r"HTTP (\d{3})", error or "")
    if m:
        return {"401": "the site requires a login (HTTP 401)",
                "403": "the site blocks automated visits (HTTP 403)",
                "404": "address not found (HTTP 404)",
                "410": "page no longer exists (HTTP 410)",
                "429": "the site said too many requests (HTTP 429)"}.get(m.group(1), f"the site returned an error (HTTP {m.group(1)})")
    if re.search(r"Timeout|timed out", error or ""):
        return "the site didn't respond in time"
    if re.search(r"SSL", error or ""):
        return "secure connection to the site failed"
    if re.search(r"ConnectionError|NameResolution|Max retries|getaddrinfo", error or ""):
        return "couldn't connect to the site"
    return "site couldn't be loaded"


def looks_like_job_list(jobs: list[Job]) -> bool:
    """Did we actually read a list of job postings (even if none match your roles)?"""
    for j in jobs:
        if j.id.startswith("data:") or j.posted_at:
            return True
        if (JOB_LINK_RE.search(urlparse(j.url).path) or detect_ats(j.url)
                or re.search(r"[?&](?:jobId|job_id|reqId|requisitionId|jid)=", j.url, re.I)):
            return True
    return False


FAMILIES = {  # tag text, text color, background
    "QA": ("Quality Assurance", "#1d4ed8", "#e0eaff"),
    "RA": ("Regulatory Affairs", "#7e22ce", "#f1e6ff"),
    "Validation": ("Validation / CSV", "#0f766e", "#d6f5ee"),
}
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def role_family(title: str) -> str:
    if re.search(r"regulatory|submission|\beCTD\b|labell?ing|\bRA\b", title, re.I):
        return "RA"
    if re.search(r"validation|\bCSV\b|qualification|commissioning|C&Q|\bCQV\b|software assurance", title, re.I):
        return "Validation"
    return "QA"


def badge(text: str, color: str, background: str) -> str:
    return (f'<span style="display:inline-block;margin:0 6px 6px 0;padding:3px 9px;border-radius:999px;'
            f'background:{background};color:{color};font-size:12px;font-weight:600;font-family:{FONT}">'
            f'{html.escape(text)}</span>')


def job_card(name: str, j: Job) -> str:
    esc = html.escape
    label, color, background = FAMILIES[role_family(j.title)]
    badges = [badge(label, color, background)]
    if j.sponsor == "no":
        badges.append(badge("⚠ Says no visa sponsorship", "#b42318", "#fde7e5"))
    elif j.sponsor == "yes":
        badges.append(badge("✓ Mentions visa sponsorship", "#067647", "#dcfae6"))
    if j.years:
        badges.append(badge(f"Asks {j.years}", "#344054", "#eaecf0"))
    where = " · ".join(esc(x) for x in (j.location, age_text(j)) if x)
    note = ""
    if j.sponsor == "no" and j.sponsor_note:
        note = (f'<div style="margin:2px 0 8px;font-size:12px;line-height:1.4;color:#667085;font-style:italic">'
                f'“{esc(j.sponsor_note)}”</div>')
    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:#ffffff;border:1px solid #e4e7ec;border-radius:12px;margin:0 0 12px">
  <tr><td style="padding:16px 18px;font-family:{FONT}">
    <div>{''.join(badges)}</div>
    <div style="margin:4px 0 2px;font-size:17px;line-height:1.3;font-weight:700;color:#101828">{esc(j.title)}</div>
    <div style="font-size:14px;color:#344054;font-weight:600">{esc(name)}</div>
    <div style="margin:2px 0 10px;font-size:13px;color:#667085">{where}</div>
    {note}
    <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
      <td style="background:#101828;border-radius:8px">
        <a href="{esc(j.url)}" style="display:inline-block;padding:9px 18px;font-family:{FONT};font-size:14px;
           font-weight:700;color:#ffffff;text-decoration:none">View job &rarr;</a>
      </td></tr></table>
  </td></tr>
</table>"""


def build_email(new, manual, full_manual_list, checked, ok, when, max_age,
                recruiters=frozenset(), per_recruiter=3) -> str:
    """The email version: a header, then one card per job, grouped by role type.
    Jobs from recruiters and consulting firms come last, a few per firm."""
    esc = html.escape
    agency = [(n, j) for n, j in new if n in recruiters]
    new = [(n, j) for n, j in new if n not in recruiters]
    counts = {}
    for _, j in new:
        counts[role_family(j.title)] = counts.get(role_family(j.title), 0) + 1
    if new or agency:
        headline = f"{len(new)} new job{'s' if len(new) != 1 else ''} for you"
        summary = "".join(badge(f"{n} {FAMILIES[f][0]}", FAMILIES[f][1], FAMILIES[f][2])
                          for f, n in counts.items())
        if agency:
            summary += badge(f"{len(agency)} from recruiters & consultancies", "#475467", "#eaecf0")
    else:
        headline, summary = "No new jobs this check", ""
    body = []
    for family, (label, color, _) in FAMILIES.items():
        jobs = [(n, j) for n, j in new if role_family(j.title) == family]
        if not jobs:
            continue
        body.append(f'<div style="margin:22px 0 10px;font-family:{FONT};font-size:13px;font-weight:700;'
                    f'letter-spacing:.05em;text-transform:uppercase;color:{color}">{esc(label)} ({len(jobs)})</div>')
        body.extend(job_card(n, j) for n, j in jobs)
    if agency:
        body.append(f'<div style="margin:26px 0 10px;font-family:{FONT};font-size:13px;font-weight:700;'
                    f'letter-spacing:.05em;text-transform:uppercase;color:#475467">'
                    f'From recruiters & consultancies ({len(agency)})</div>')
        firms: dict[str, list[Job]] = {}
        for n, j in agency:
            firms.setdefault(n, []).append(j)
        for n, jobs in firms.items():
            body.extend(job_card(n, j) for j in jobs[:per_recruiter])
            if len(jobs) > per_recruiter:
                body.append(f'<div style="margin:-4px 0 12px;font-family:{FONT};font-size:13px;color:#667085">'
                            f'+{len(jobs) - per_recruiter} more matching jobs at {esc(n)} (all in jobs_log.csv)</div>')
    if not new and not agency:
        body.append(f'<div style="margin:16px 0;font-family:{FONT};font-size:15px;color:#344054">'
                    f'Nothing new matching your roles was posted in the last {max_age:g} hours.</div>')
    if manual and full_manual_list:
        rows = "".join(f'<tr><td style="padding:6px 0;border-top:1px solid #eaecf0;font-family:{FONT};font-size:13px">'
                       f'<a href="{esc(link)}" style="color:#1d4ed8;font-weight:600;text-decoration:none">{esc(name)}</a>'
                       f'<span style="color:#667085"> · {esc(reason)}</span></td></tr>' for name, link, reason in manual)
        body.append(f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:#ffffff;border:1px solid #e4e7ec;border-radius:12px;margin:22px 0 12px">
  <tr><td style="padding:16px 18px">
    <div style="font-family:{FONT};font-size:15px;font-weight:700;color:#101828">Check these by hand ({len(manual)})</div>
    <div style="margin:2px 0 8px;font-family:{FONT};font-size:13px;color:#667085">The watcher can't read their job lists.</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>
  </td></tr>
</table>""")
    elif manual:
        body.append(f'<div style="margin:18px 0 0;font-family:{FONT};font-size:13px;color:#667085">'
                    f'{len(manual)} site{"s" if len(manual) != 1 else ""} still need checking by hand '
                    f'(full list in manual_check.csv and in the first email after 8 AM).</div>')
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light"></head>
<body style="margin:0;padding:0;background:#f2f4f7">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f2f4f7">
  <tr><td align="center" style="padding:24px 12px">
    <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px">
      <tr><td>
        <div style="font-family:{FONT};font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#667085">Job Watch · {esc(when)}</div>
        <div style="margin:4px 0 10px;font-family:{FONT};font-size:24px;line-height:1.25;font-weight:800;color:#101828">{esc(headline)}</div>
        <div>{summary}</div>
        {''.join(body)}
        <div style="margin:22px 0 0;font-family:{FONT};font-size:12px;line-height:1.5;color:#98a2b3">
          Checked {ok} of {checked} companies. Only US jobs posted in the last {max_age:g} hours that match your roles
          are included; every job sent is also saved in jobs_log.csv.</div>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""


def build_message(new, manual, full_manual_list, companies, max_age, checked, ok, when,
                  recruiters=frozenset(), per_recruiter=3):
    """The message sent after every run: new jobs (or "no new jobs"), then the check-by-hand list.
    Direct employers first; recruiters and consulting firms last, a few jobs each."""
    order = {c["name"]: i + (100000 if c["name"] in recruiters else 0) for i, c in enumerate(companies)}
    text, rich = [], ['<div style="font-family:Calibri,Arial,sans-serif;font-size:15px">']
    esc = html.escape

    if new:
        groups: dict[str, list[Job]] = {}
        for name, job in sorted(new, key=lambda x: order.get(x[0], 0)):
            groups.setdefault(name, []).append(job)
        heading = f"New jobs ({len(new)})" + (f", posted in the last {max_age:g} hours" if max_age else "")
        text.append(heading.upper())
        rich.append(f"<h3>{esc(heading)}</h3>")
        for name, jobs in groups.items():
            hidden = 0
            if name in recruiters and len(jobs) > per_recruiter:
                jobs, hidden = jobs[:per_recruiter], len(jobs) - per_recruiter
            text.append(f"\n{name}" + (" (recruiter / consultancy)" if name in recruiters else ""))
            rich.append(f"<p style='margin:12px 0 4px'><b>{esc(name)}</b></p><ul style='margin-top:0'>")
            for j in jobs:
                extra = " - ".join(x for x in (j.location, age_text(j)) if x)
                note = flags(j)
                text.append(f"  - {j.title}" + (f" ({extra})" if extra else "")
                            + (f"\n    {note}" if note else "") + f"\n    {j.url}")
                color = "#b00020" if j.sponsor == "no" else "#666"
                rich.append(f'<li><a href="{esc(j.url)}">{esc(j.title)}</a>'
                            + (f" <span style='color:#666'>- {esc(extra)}</span>" if extra else "")
                            + (f"<br><span style='color:{color}'>{esc(note)}</span>" if note else "") + "</li>")
            if hidden:
                text.append(f"  + {hidden} more at {name} (all in jobs_log.csv)")
            rich.append("</ul>")
    else:
        line = (f"No new openings. Checked {ok} of {checked} companies at {when}: nothing new matching "
                f"your roles" + (f" was posted in the last {max_age:g} hours." if max_age else "."))
        text.append(line)
        rich.append(f"<p>{esc(line)}</p>")

    if manual:
        if full_manual_list:
            text.append(f"\nCHECK THESE BY HAND ({len(manual)}) - the watcher can't read their job lists")
            rich.append(f"<h3>Check these by hand ({len(manual)})</h3><p style='color:#666'>"
                        "The watcher can't read their job lists, so look at these yourself.</p><ul>")
            for name, link, reason in manual:
                text.append(f"  - {name}: {link}")
                rich.append(f'<li><a href="{esc(link)}">{esc(name)}</a> <span style="color:#666">- {esc(reason)}</span></li>')
            rich.append("</ul>")
        else:
            line = ((f"{len(manual)} sites still need" if len(manual) != 1 else "1 site still needs") + " checking by hand. Full list in manual_check.csv, "
                    "and in the first message after 8 AM.")
            text.append("\n" + line)
            rich.append(f"<p style='color:#666'>{esc(line)}</p>")
    rich.append("</div>")

    if new:
        direct = [n for n, _ in new if n not in recruiters]
        names = list(dict.fromkeys(direct or [n for n, _ in new]))
        who = ", ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "")
        agency_count = len(new) - len(direct)
        subject = (f"{len(direct)} new job match{'es' if len(direct) != 1 else ''}: {who}" if direct
                   else f"{agency_count} new recruiter job{'s' if agency_count != 1 else ''}: {who}")
        if direct and agency_count:
            subject += f" (+{agency_count} from recruiters)"
    else:
        subject = f"No new jobs ({when} check)"
    click = new[0][1].url if len(new) == 1 else None
    rich = build_email(new, manual, full_manual_list, checked, ok, when, max_age, recruiters, per_recruiter)
    return subject, "\n".join(text).strip(), rich, click


def write_manual_file(manual, since):
    with open(MANUAL_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["Company", "Check here", "Why", "On this list since"])
        for name, link, reason in manual:
            writer.writerow([name, link, reason, since.get(name, "")])


def send_resend(subject: str, text: str, rich: str, to: str, key: str):
    """Email through Resend's web API (works from GitHub, where Gmail's mail server often hangs up)."""
    key = key.strip().strip('"').strip("'")
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    try:
        r = requests.post("https://api.resend.com/emails", timeout=30,
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                   "User-Agent": USER_AGENT},
                          json={"from": "Job Watch <onboarding@resend.dev>", "to": [to], "subject": subject,
                                "html": rich, "text": text})
    except requests.RequestException as e:
        raise RuntimeError(f"couldn't reach Resend ({type(e).__name__})")
    if r.status_code < 300:
        return
    try:
        detail = r.json().get("message") or r.text
    except ValueError:
        detail = r.text
    detail = clean(str(detail))[:200]
    hint = ""
    if not key.startswith("re_"):
        hint = " The RESEND_API_KEY secret should start with re_: paste the full key, nothing else."
    elif r.status_code in (401, 403) and "own email" not in detail.lower() and "testing emails" not in detail.lower():
        hint = " Create a new API key in Resend (sending access, all domains) and replace the RESEND_API_KEY secret."
    elif "own email" in detail.lower() or "testing emails" in detail.lower():
        hint = " Set the EMAIL_TO secret to exactly the email address you signed up to Resend with."
    raise RuntimeError(f"Resend said: {detail} (HTTP {r.status_code}).{hint}")


def send_email(subject: str, text: str, rich: str, to: str):
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").replace(" ", "").strip()  # Google shows it in groups of 4
    if not user or not password:
        raise RuntimeError("EMAIL_TO is set but the SMTP_USER or SMTP_PASSWORD secret is missing")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(text)
    msg.add_alternative(rich, subtype="html")
    host = os.environ.get("SMTP_HOST", "").strip() or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT", "").strip() or 587)
    # Gmail accepts two connection methods; if one gets cut off, try the other (and retry once)
    methods = [(port, "ssl" if port == 465 else "starttls")]
    other = (587, "starttls") if port == 465 else (465, "ssl")
    methods += [other, methods[0]]
    last_error = None
    for attempt, (use_port, kind) in enumerate(methods):
        try:
            if kind == "ssl":
                smtp = smtplib.SMTP_SSL(host, use_port, timeout=30, context=ssl.create_default_context())
            else:
                smtp = smtplib.SMTP(host, use_port, timeout=30)
            with smtp:
                smtp.ehlo()
                if kind == "starttls":
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                smtp.login(user, password)
                smtp.send_message(msg)
            return
        except smtplib.SMTPAuthenticationError:
            raise RuntimeError("Gmail didn't accept the login. Check that SMTP_USER is the Gmail address the app "
                               "password was made in, and that SMTP_PASSWORD is the 16-letter app password "
                               "(not your normal password)")
        except (smtplib.SMTPException, OSError) as e:
            last_error = f"{type(e).__name__}: {e} (port {use_port})"
            if attempt < len(methods) - 1:
                time.sleep(5)
    raise RuntimeError(f"couldn't reach the mail server. Last error: {last_error}")


def notify(subject: str, text: str, rich: str, click: str | None = None, quiet: bool = False,
           email: bool = True) -> bool:
    """Send the email and the phone push separately, so a problem with one doesn't stop the other.
    Raises only if every configured channel failed."""
    delivered, problems = 0, []
    to = os.environ.get("EMAIL_TO", "").strip()
    resend_key = os.environ.get("RESEND_API_KEY", "").strip()
    has_login = bool(resend_key or os.environ.get("SMTP_USER", "").strip()
                     or os.environ.get("SMTP_PASSWORD", "").strip())
    if to and email:
        try:
            if resend_key:
                send_resend(subject, text, rich, to, resend_key)
            else:
                send_email(subject, text, rich, to)
            delivered += 1
            name, _, domain = to.partition("@")
            print("Email: sent")
        except Exception as e:
            problems.append(f"Email didn't send: {e}")
    elif to:
        print("Email: not sent this run (no new jobs, and email_every_run is false)")
    elif has_login:
        problems.append("Email didn't send: the EMAIL_TO secret is missing. Its name must be exactly EMAIL_TO")
    else:
        print("Email: not set up (no EMAIL_TO, SMTP_USER or SMTP_PASSWORD secrets)")
    for i in range(2, 6):  # more people: EMAIL_TO_2 with RESEND_API_KEY_2 (their own Resend key), up to _5
        extra_to = os.environ.get(f"EMAIL_TO_{i}", "").strip()
        if not extra_to or not email:
            continue
        extra_key = os.environ.get(f"RESEND_API_KEY_{i}", "").strip() or resend_key
        if not extra_key:
            problems.append(f"Email to person {i} didn't send: add the RESEND_API_KEY_{i} secret "
                            "(their own Resend key)")
            continue
        try:
            send_resend(subject, text, rich, extra_to, extra_key)
            delivered += 1
            print(f"Email: sent to person {i}")
        except Exception as e:
            problems.append(f"Email to person {i} didn't send: " + str(e)
                            .replace("the EMAIL_TO secret", f"the EMAIL_TO_{i} secret")
                            .replace("the RESEND_API_KEY secret", f"the RESEND_API_KEY_{i} secret"))
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        body = text if len(text) <= 3500 else text[:3400] + "\n...(list cut short; the email has all of it)"
        if problems:
            body += "\n\n⚠ " + " ".join(problems)[:300]
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/")
        headers = {"Title": subject.encode("ascii", "ignore").decode(), "Tags": "briefcase",
                   "Priority": "2" if quiet and not problems else "4"}  # "no new jobs" messages arrive quietly
        if click:
            headers["Click"] = click
        try:
            http("POST", f"{server}/{topic}", data=body.encode("utf-8"), headers=headers)
            delivered += 1
        except Exception as e:
            problems.append(f"Phone push didn't send: {e}")
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems and not delivered:
        raise RuntimeError("; ".join(problems))
    return delivered > 0


# --------------------------------------------------------------------------- main

def print_check(name, source, jobs, results):
    sending = sum(1 for _, status in results if status == "NEW")
    print(f"\n[OK] {name}  via {source}: {len(jobs)} postings/links read, "
          f"{len(results)} match your roles, {sending} would be sent")
    for j, status in results:
        extra = " - ".join(x for x in (j.location, age_text(j)) if x)
        mark = "+" if status == "NEW" else " "
        print(f"   {mark} {j.title}" + (f"  ({extra})" if extra else "") + ("" if status == "NEW" else f"  [{status}]"))
        if status == "NEW":
            if flags(j):
                print(f"       {flags(j)}")
            if j.sponsor_note:
                print(f"       \"{j.sponsor_note}\"")
            print(f"       {j.url}")
    hits = results
    if source.startswith("page") and not hits:
        print("     (Reading a plain page. If this company does list matching jobs, paste a job-search "
              "results URL instead, or set render=yes.)")


def run(args) -> int:
    cfg = load_config()
    filters = build_filters(cfg)
    companies = load_companies(args.company)
    state = load_state()
    legacy = state.get("version") != 2 and bool(state.get("seen"))  # state saved by the first version
    seen_all = state.setdefault("seen", {})    # every matching job ID ever noticed
    sent_all = state.setdefault("sent", {})    # title + location of every job ever sent to you
    failures = state.setdefault("failures", {})
    counts = state.setdefault("counts", {})
    addresses = state.setdefault("addresses", {})  # job boards found from homepages, reused next run
    sent_urls = state.setdefault("sent_urls", {})  # links of every job sent, across all companies
    never_repeat_title = bool(cfg.get("never_repeat_title", True))
    global NOW
    NOW = datetime.now(timezone.utc)
    stamp = NOW.isoformat(timespec="seconds")
    gap = float(cfg.get("min_hours_between_scheduled_runs", 2.5))
    if (not args.check and os.environ.get("GITHUB_EVENT_NAME") == "schedule" and state.get("last_run")
            and (NOW - datetime.fromisoformat(state["last_run"])).total_seconds() < gap * 3600):
        print(f"Backup timer: a run already happened at {state['last_run']}, so this one stands down.")
        return 0
    max_age = float(cfg.get("max_age_hours") or 0)
    started = time.time()

    # every-run companies always; twice-a-day ("normal" priority) ones when their time comes
    last_checked = state.setdefault("last_checked", {})
    intervals = {"medium": float(cfg.get("medium_interval_hours", 6)),
                 "normal": float(cfg.get("normal_interval_hours", 9))}
    order = {"high": 0, "medium": 1, "normal": 2}
    all_companies = companies

    def due(c):
        if args.check or args.company or c["priority"] == "high" or not last_checked.get(c["name"]):
            return True
        hours = intervals[c["priority"]]
        return (NOW - datetime.fromisoformat(last_checked[c["name"]])).total_seconds() >= (hours - 0.5) * 3600
    companies = sorted((c for c in all_companies if due(c)),
                       key=lambda c: (order[c["priority"]], last_checked.get(c["name"], "")))
    deadline = None if args.check else started + float(cfg.get("max_run_minutes", 20)) * 60
    global DEADLINE
    DEADLINE = deadline
    print(f"Checking {len(companies)} of {len(all_companies)} companies this run...")
    results = fetch_all(companies, cfg, addresses, deadline)

    manual_store = state.setdefault("manual", {})  # check-by-hand list, kept across runs
    new, manual, to_fix, check_blocks = [], [], [], []
    for c in companies:
        name = c["name"]
        jobs, source, error, remember = results[name]
        if source == POSTPONED:
            continue
        last_checked[name] = stamp
        manual_store.pop(name, None)
        if not error and not jobs and counts.get(name, 0) >= 5:
            error = f"returned nothing this time (it had {counts[name]} last time); the site may have changed"
        if error:
            failures[name] = failures.get(name, 0) + 1
            if failures[name] >= MANUAL_AFTER_FAILURES or args.check:
                manual_store[name] = [c["url"], friendly_error(error)]
            if args.check:
                print(f"\n[ERROR] {name}\n     {error}")
                to_fix.append(f"{name}: {error}")
            continue
        failures.pop(name, None)
        counts[name] = len(jobs)
        if remember:
            addresses[name] = remember
        else:
            addresses.pop(name, None)
        hits = [j for j in jobs if matches(j, filters)]
        if source.startswith(("page", "search")) and not hits and not looks_like_job_list(jobs):
            page = re.match(r"(?:page|search) (https?://\S+)", source)
            link = page.group(1).replace("{q}", "quality") if page else c["url"]
            manual_store[name] = [link, "couldn't find a job list on the page"]
        first_time = name not in seen_all
        seen = seen_all.setdefault(name, {})
        sent = sent_all.setdefault(name, {})
        statuses = []
        for j in hits:
            key, tkey, ukey = title_key(j), "t:" + plain_title(j), url_key(j.url)
            if legacy and j.id in seen:
                sent.setdefault(key, seen[j.id])  # the first version sent everything it recorded
            earlier = sent.get(key) or (sent.get(tkey) if never_repeat_title else None) or sent_urls.get(ukey)
            if earlier:  # same job: same title+location, same title at this company, or the same link anywhere
                status = "same as one above" if earlier == stamp else "already sent"
            elif j.id in seen:
                status = "seen before"
            elif j.posted_at and max_age and (NOW - j.posted_at).total_seconds() > max_age * 3600:
                status = f"older than {max_age:g}h"
            elif not j.posted_at and first_time:
                status = "no posting date; first check of this site"
            else:
                status = "NEW"
                new.append((name, j))
                sent[key] = sent[tkey] = sent_urls[ukey] = stamp
            seen.setdefault(j.id, stamp)
            statuses.append((j, status))
        if args.check:
            check_blocks.append((name, source, jobs, statuses))
            if not jobs:
                to_fix.append(f"{name}: {source} returned 0 postings")
            elif source.startswith(("page", "search")) and not hits and not looks_like_job_list(jobs):
                to_fix.append(f"{name}: read {source} but couldn't find a job list there")

    if not args.company:  # second source: Adzuna, by job title across the US (skipped when testing one company)
        for name, j in fetch_adzuna(cfg, all_companies):
            if not matches(j, filters):
                continue
            seen = seen_all.setdefault(name, {})
            sent = sent_all.setdefault(name, {})
            key = title_key(j)
            bare = plain_title(j)
            ukey, tkey = url_key(j.url), "t:" + bare
            already = (key in sent or ukey in sent_urls or (never_repeat_title and tkey in sent)
                       or any(k.startswith(bare) for k in sent))  # found on the career site too
            if already or j.id in seen:
                seen.setdefault(j.id, stamp)
                continue
            seen.setdefault(j.id, stamp)
            if j.posted_at and max_age and (NOW - j.posted_at).total_seconds() > max_age * 3600:
                continue
            new.append((name, j))
            sent[key] = sent[tkey] = sent_urls[ukey] = stamp
            if args.check:
                print(f"   + [Adzuna] {name}: {j.title} ({j.location})\n       {j.url}")
    read_descriptions([j for _, j in new])
    skipped = {}
    for name, j in new:
        reason = skip_reason(j, filters, cfg)
        if reason:
            skipped[j.id] = reason
            for k in (title_key(j), "t:" + plain_title(j)):  # never sent, so don't block a later re-post
                sent_all.get(name, {}).pop(k, None)
            sent_urls.pop(url_key(j.url), None)
    if skipped:
        counts_by_reason = {}
        for reason in skipped.values():
            key = "asks 5+ years" if reason.startswith("asks") else reason
            counts_by_reason[key] = counts_by_reason.get(key, 0) + 1
        print("Skipped after reading descriptions: " + ", ".join(f"{n} {r}" for r, n in counts_by_reason.items()))
    new = [(name, j) for name, j in new if j.id not in skipped]
    for name, source, jobs, statuses in check_blocks:
        statuses = [(j, f"skipped: {skipped[j.id]}" if status == "NEW" and j.id in skipped else status)
                    for j, status in statuses]
        print_check(name, source, jobs, statuses)

    listed = {c["name"] for c in all_companies}
    for name in [n for n in manual_store if n not in listed]:
        del manual_store[name]  # removed from companies.csv
    manual = [(c["name"], *manual_store[c["name"]]) for c in all_companies if c["name"] in manual_store]
    attempted = [c for c in companies if results[c["name"]][1] != POSTPONED]
    postponed = len(companies) - len(attempted)
    ok = sum(1 for c in attempted if not results[c["name"]][2])
    summary = (f"Checked {len(attempted)} of {len(all_companies)} companies ({ok} OK, {len(attempted) - ok} failed) "
               f"in {time.time() - started:.0f}s." + (f" {postponed} postponed to the next run." if postponed else ""))
    if args.check:
        print("\n" + summary + "\n(Check mode: nothing was sent or saved.)")
        if to_fix:
            print(f"\n----- {len(to_fix)} to fix or double-check (copy from here down) -----")
            print("\n".join(to_fix))
        return 0

    # check-by-hand list: in full when it changes and once a day after 8 AM, otherwise one line
    local = NOW.astimezone(LOCAL_TZ)
    when = local.strftime("%-I:%M %p") if os.name != "nt" else local.strftime("%I:%M %p").lstrip("0")
    today = local.date().isoformat()
    since = state.setdefault("manual_since", {})
    names = sorted(n for n, _, _ in manual)
    for n in names:
        since.setdefault(n, today)
    for n in [k for k in since if k not in names]:
        del since[n]
    full_list = bool(manual) and (names != state.get("manual_last_list")
                                  or (local.hour >= 8 and state.get("manual_last_date") != today))

    recruiters = {c["name"] for c in all_companies
                  if re.search(r"recruit|staffing|consult", c.get("group", ""), re.I)}
    recruiters |= set(cfg.get("recruiter_companies", []))
    if not cfg.get("include_recruiters", True):
        new = [(n, j) for n, j in new if n not in recruiters]
    subject, text, rich, click = build_message(new, manual, full_list, all_companies, max_age,
                                               len(attempted), ok, when, recruiters,
                                               int(cfg.get("max_jobs_per_recruiter", 3)))
    if cfg.get("browser_fallback", True):
        b = BROWSER_STATS
        if not browser_available():
            browser_note = "Headless browser: not installed, so JavaScript-only career sites can't be read."
        else:
            browser_note = f"Headless browser: opened {b['opened']} page(s), {b['failed']} failed."
            if b["failed"]:
                browser_note += f" First error: {b['error']}"
        print(browser_note)
        if not browser_available() or (b["failed"] and not b["opened"]):
            text += "\n\n⚠ " + browser_note  # it isn't working at all: say so in the message
    if cfg.get("quiet_logs", True):  # public repository: logs show counts only, never job details
        print(f"\nMessage: {len(new)} new job(s), {len(manual)} site(s) to check by hand")
    else:
        print("\n" + subject + "\n\n" + text + "\n")
    try:
        sent = notify(subject, text, rich, click, quiet=not new,
                      email=bool(new) or bool(cfg.get("email_every_run", False)))
    except Exception as e:
        print(f"Could not send the notification: {e}", file=sys.stderr)
        return 1  # state not saved, so these jobs are reported again next run
    if not sent:
        print("(No NTFY_TOPIC or EMAIL_TO set, so the message was only printed here.)")
    if full_list:
        state["manual_last_list"], state["manual_last_date"] = names, today
    write_manual_file(manual, since)
    append_log(new, stamp, with_sponsorship=bool(cfg.get("log_sponsorship", False)))  # creates it on the first run
    state["last_run"] = stamp
    state["version"] = 2
    save_state(state)
    print(summary)
    return 0


def sample_message():
    """Made-up jobs, so a test shows exactly what real emails will look like."""
    global NOW
    NOW = datetime.now(timezone.utc)
    examples = [
        ("Example Pharma", Job("s1", "QA Specialist I", "https://example.com/job/1", "Boston, MA", "Posted Today",
                               years="0+ yrs")),
        ("Sample Bio", Job("s2", "Regulatory Affairs Associate", "https://example.com/job/2", "Princeton, NJ",
                           "Posted Yesterday", sponsor="yes", years="2+ yrs")),
        ("Demo Therapeutics", Job("s3", "CSV Analyst I", "https://example.com/job/3", "Cambridge, MA", "Posted Today",
                                  sponsor="no", years="1+ yrs",
                                  sponsor_note="Applicants must be authorized to work in the US without sponsorship.")),
    ]
    when = NOW.astimezone(LOCAL_TZ).strftime("%I:%M %p").lstrip("0")
    rich = build_email(examples, [], False, 139, 131, when, 72)
    text = ("Job watch test: if you can read this, notifications work. "
            "The email version shows 3 sample jobs so you can see the layout.")
    return "Job watch test: this is how your job emails will look", text, rich


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_env_file()
    parser = argparse.ArgumentParser(description="Watch career sites for new matching jobs.")
    parser.add_argument("--check", action="store_true", help="show what each site returned; send and save nothing")
    parser.add_argument("--company", help="only this company (name as written in companies.csv)")
    parser.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    args = parser.parse_args()
    if args.test_notify:
        subject, text, rich = sample_message()
        if notify(subject, text, rich):
            print("Test notification sent (the email shows sample jobs, so you can see the layout).")
            return 0
        print("Nothing configured. Set NTFY_TOPIC and/or EMAIL_TO, SMTP_USER, SMTP_PASSWORD.")
        return 1
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
