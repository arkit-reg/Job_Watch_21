# Job watch

Checks a list of company career sites every 3 hours and sends you one message (phone push, email, or both) when a posting appears that matches your target roles and was posted in the last 72 hours. Each job is sent once and never again, including when a company re-posts the same title at the same location under a new job number.

| File | What it's for |
|---|---|
| `companies.csv` | Your company list. Edit it in Excel or any text editor. |
| `config.toml` | Which job titles count as a match, and optional location filters. |
| `job_watch.py` | The script. You shouldn't need to change it. |
| `.github/workflows/job-watch.yml` | Runs the script on GitHub's servers every 3 hours, for free. |
| `state.json` | Created automatically; remembers which jobs you've already been told about. |

## 1. Fill in companies.csv

Columns: `company`, `url`, `render` (leave blank unless step 3 tells you otherwise), `priority` (the batch: `high` or blank = checked every run, about every 3 hours; `medium` = every 6 hours; `normal` = every 9 hours; the 6 and 9 are set in `config.toml` and should be multiples of 3, since runs happen every 3 hours), and `group` (just a label for sorting in Excel; the watcher ignores it). To move a company to every-run checks, change its priority to `high`. The two rows already there are starting points to confirm with the test in step 3.

The address you paste matters. The best one is the job-search page itself, not the company's "Careers" landing page. Open the careers page, click "Search jobs" or "View openings", and copy the address from your browser. If it looks like one of these, the script reads the job system's data feed directly, which is fast and reliable:

| Job system | Address looks like |
|---|---|
| Workday (most large pharma) | `https://company.wd1.myworkdayjobs.com/Careers` (also `/en-US/Careers`) |
| Greenhouse | `https://job-boards.greenhouse.io/company` or `boards.greenhouse.io/company` |
| Lever | `https://jobs.lever.co/company` |
| Ashby | `https://jobs.ashbyhq.com/company` |
| SmartRecruiters | `https://careers.smartrecruiters.com/Company` |

If a company's own careers page embeds one of these systems, the script usually finds it on its own. For anything else (iCIMS, SuccessFactors, Taleo, Oracle, a recruiting firm's own site), it reads every link on the page and keeps the ones whose text matches your keywords. For SuccessFactors-style sites (`jobs.company.com/search/?q=`), paste a search-results address, for example with `?q=quality`.

Save as "CSV UTF-8" if you edit in Excel.

## 2. Adjust config.toml (optional)

It's already set up for entry-level Regulatory Affairs, QA/Quality Systems, and Validation/CSV titles, filters out Senior, Supervisor, Manager, Director, level III and up, internships, and non-US locations, and only sends jobs posted in the last 72 hours (`max_age_hours`). Add or remove words there; plain-English comments explain each list.

## 3. Test on your computer

You need Python 3.9 or newer.

```
pip install -r requirements.txt
python job_watch.py --check
```

This prints, for every company, which system it detected, how many postings it read, which ones match your roles, and which of those would be sent (marked `+`). The rest show why not: older than 72h, already sent, or no posting date on a first check. It sends and saves nothing, so run it as often as you like while fixing your list. To test one company: `python job_watch.py --check --company "Regeneron"`.

How to read the results:

- **[ERROR] HTTP 404**: the address is wrong. Find the job-search page again.
- **via page, 0 match, but the site clearly has matching jobs**: the job list is built by JavaScript after the page loads. Either find the underlying job-system address (click into one job and look at the address bar; it often reveals myworkdayjobs.com, greenhouse.io, and so on), or set `render` to `yes` for that row and run `pip install playwright` and `python -m playwright install chromium`.
- **via page, matches include junk like "Quality Policy"**: harmless (you're told about each link once), but a more specific search-results address cuts it down.

## 4. Set up notifications

Use either or both.

**Phone push (easiest):** install the free ntfy app (iOS or Android), tap "+", and subscribe to a topic name nobody would guess, like `arkit-jobs-7q2x9` (anyone who knows the name can read it). That name is your `NTFY_TOPIC`.

**Email (recommended: Resend):** sign up free at resend.com with the email address you want alerts at, create an API key, and add two secrets: `RESEND_API_KEY` (the key, starting with `re_`) and `EMAIL_TO` (that same email address). Resend sends over a normal web connection, which works reliably from GitHub, and its free plan only delivers to your own sign-up address, which is all you need.

**Email (Gmail alternative):** with a Gmail account, turn on 2-Step Verification, then search your Google Account settings for "App passwords" and create one. Use your Gmail address as `SMTP_USER`, the 16-character app password as `SMTP_PASSWORD`, and wherever you want the alerts as `EMAIL_TO`. For other providers, also set `SMTP_HOST` and `SMTP_PORT`.

The email is a digest of job cards grouped by role type (QA, RA, Validation/CSV), each with badges for sponsorship and experience and a View job button. It's sent only when there are new jobs; set `email_every_run = true` in `config.toml` to also get "no new jobs" emails. The phone push still comes after every run.

To test on your computer: copy `.env.example` to `.env`, fill it in, and run `python job_watch.py --test-notify`. The test email contains three sample jobs, so you can see the layout.

## 5. Run it every 3 hours on GitHub (free, works with your laptop off)

1. Create a free GitHub account and a new **private** repository.
2. Upload all the files. Folders whose names start with a dot can be hidden on Mac and Windows, so if `.github/workflows/job-watch.yml` doesn't come along, use "Add file → Create new file", type `.github/workflows/job-watch.yml` as the name, and paste the file's contents. Don't upload `.env`.
3. In the repository, go to Settings → Secrets and variables → Actions, and add a repository secret for each setting you use: `NTFY_TOPIC`, `EMAIL_TO`, `SMTP_USER`, `SMTP_PASSWORD`.
4. Open the Actions tab, choose "Job watch", and click "Run workflow". After a minute or two, the run's log shows the same output as on your computer, and you'll get a message if any matching job was posted in the last 72 hours.

From then on it runs on its own every 3 hours. A private repository gets 2,000 free minutes a month, and each run takes about 1–3 minutes, so 8 runs a day fits comfortably. To change your list later, edit `companies.csv` or `config.toml` right on GitHub.

If a site works with `--check` on your computer but fails on GitHub, that site blocks cloud servers. Run the script on your own computer for that one instead, or drop it.

## Running on your own computer instead

Mac or Linux: run `crontab -e` and add a line such as
`17 */3 * * * cd /path/to/job-watch && /usr/bin/python3 job_watch.py >> log.txt 2>&1`
Windows: use Task Scheduler to run `python job_watch.py` in this folder every 3 hours. Either way, settings come from `.env`, and nothing runs while the computer is asleep.

## Public repository mode

This setup is meant for a public repository, where GitHub minutes are unlimited: every company is checked every 3 hours (`medium_interval_hours` and `normal_interval_hours` = 3), runs may take up to 25 minutes, and the browser searches JavaScript career sites more deeply. Because anyone can read a public repository and its run logs, the logs show only counts (`quiet_logs = true`), and `jobs_log.csv` leaves out the sponsorship columns (`log_sponsorship = false`). The sponsorship notes are still in your emails. Never upload a `jobs_log.csv` that has sponsorship columns to a public repository.

## Second source: Adzuna

Some career sites can't be read automatically. To cover them, each run also searches Adzuna, a large job search engine that collects postings from company career sites and job boards, for your job titles across the US (postings from the last day). Results from companies in `companies.csv` go through the same filters and duplicate checks as everything else, so a job found both ways is sent once. Get a free key at developer.adzuna.com and add two repository secrets, `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`. The free plan allows 2,500 searches a month; 8 searches a run (`adzuna_max_calls_per_run`) is about 1,900. Set `adzuna_any_company = true` to see matching jobs from any US employer, not just your list.

## Final checks before a job is sent

After finding a new matching job, the watcher reads its full description and skips it if:
- **it's outside the US** (`us_only = true`): judged from the country the job system gives (Workday provides one), then the location, then the description itself (US postings usually carry EEO/E-Verify wording or a $ pay range);
- **it asks for more experience than you want** (`max_years_experience = 4`): it uses the lowest requirement, so "Bachelor's + 5 years or Master's + 3 years" counts as 3 and is kept;
- **it's a software-testing job**: a QA or validation title whose description is full of software-testing terms (Selenium, test automation, JavaScript, Agile...) with no GMP/quality-system terms.

Skipped jobs are never sent and are counted in the run log. Research, lab and scientist titles, software titles, and non-pharma "compliance" and "regulatory" jobs (legal, finance, trade, environmental) are filtered out by title in `config.toml`.

## Sites that need a real browser

Some career pages only show their jobs after JavaScript runs in a browser. When a page shows no job list, the watcher opens it in a headless browser (Chromium, installed by the GitHub workflow) and looks again, including inside frames where many job boards are embedded. When that turns up a job board, its address is saved, so later runs read it directly without the browser. Sites whose job list only exists in the browser are opened in the browser on every run, which adds a few seconds each. To switch this off, set `browser_fallback = false` in `config.toml`. On your Mac it's skipped unless you install it: `python3 -m pip install playwright` then `python3 -m playwright install chromium`.

## Getting a company off the check-by-hand list

The watcher recognizes the common job systems and career-site platforms on its own (Workday, Greenhouse, Lever, Ashby, SmartRecruiters, BambooHR, Workable, Jobvite, iCIMS, UKG, ADP, Paylocity, Oracle, Eightfold, SuccessFactors, Radancy, Phenom, Attrax), including job lists hidden inside the page. If a company is still on the list, give it the site's own search page:

1. Open the company's careers site and use its job search box to search for the word **quality**.
2. Copy the address from your browser's address bar. It should contain the word `quality`, for example `https://careers.abbvie.com/en/jobs?q=quality&options=&page=1`.
3. Paste that address into the company's `url` cell in `companies.csv`.

The watcher swaps "quality" for each of your search words on every run. If the address doesn't change when you search (it never shows "quality"), this method won't work for that site; keep checking it by hand.

## A message every run

You get a message after every run (about every 3 hours), even when nothing is new. "No new jobs" messages are sent as low priority, so they arrive quietly; new-job alerts are high priority. Runs happen through the night too (times are Eastern), so use your phone's Do Not Disturb or Sleep focus if you don't want to see them overnight.

## Sites to check by hand (manual_check.csv)

Some career sites can't be read automatically: they won't load, or the job list only appears through JavaScript. These go on a check-by-hand list. Messages include the full list, with a link for each company, whenever the list changes and in the first message after 8 AM each day; other messages just say how many there are. The same list is always in `manual_check.csv` in your repository. A site that fails to load goes on the list after failing 2 runs in a row (one failure is usually a hiccup) and comes off by itself once it works again.

## Your job log (jobs_log.csv)

Every job the watcher sends you is also added to `jobs_log.csv` in your repository, with the date found, company, title, location, posting age, what the description says about visa sponsorship (and the exact sentence), the experience it asks for, and the link. Empty "Applied" and "Notes" columns are there for your own use. To open it in Excel, click the file on GitHub, then the download button. Keep your edits in your downloaded copy rather than editing the file on GitHub, so they never clash with the watcher adding new rows.

## Sponsorship and experience flags

For each new job, the watcher reads the full description and adds a note to the alert:
- **⚠ says no visa sponsorship**: the description says it can't sponsor, requires work authorization "without sponsorship", or requires US citizenship or a security clearance.
- **mentions visa sponsorship**: the description says sponsorship is available.
- **asks N+ yrs**: the lowest years of experience it mentions (for "Bachelor's + 5 years or Master's + 3 years" it shows 3+).

No note means the description doesn't say. That's common and doesn't mean either way, so ask the recruiter. The exact sentence is saved in `jobs_log.csv`; glance at it before ruling a job out, since wording can be unusual.

## Good to know

- **Run time limit:** after `max_run_minutes` (5, which keeps 8 runs a day inside GitHub's 2,000 free minutes a month for a private repository even if every run is full; raise it to about 8 with the Student Pack's 3,000), the watcher stops new work, including further Workday searches and pages waiting for the browser, and those companies are checked first on the next run and checks the rest on the next run, so a long run (like the first one with many new companies) never gets cut off. Nothing is lost.

- **Saved job-board addresses:** when the watcher finds a company's job board starting from its homepage, it remembers that address (in `state.json`) and goes straight there on later runs, which is faster and fails less. If the saved address stops working, it searches from the homepage again by itself. If you change a company's address in `companies.csv`, it starts fresh for that company.

- If a site fails 3 runs in a row (about 9 hours), you get one message about it so a broken address doesn't silently hide jobs.
- **72-hour rule:** Workday sites only show whole days ("Posted 2 Days Ago"), so 72 hours keeps Today, Yesterday and 2 Days Ago. Some sites show no posting date at all (Krystal's is one). For those, a job counts as new when it first appears, which is always within about 3 hours because the watcher checks that often. Undated jobs already listed when you first add a company are recorded quietly, not sent.
- **Never repeated:** every job sent is remembered permanently in `state.json`. Re-posts with the same title and location are skipped too. Deleting `state.json` makes it forget everything.
- If you change your keywords, postings that now match are sent only if they're within 72 hours and haven't been sent before.
- Checking every few hours is a very light load, similar to visiting each careers page yourself. Don't set it to run every few minutes.
