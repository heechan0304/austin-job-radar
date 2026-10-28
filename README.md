# Austin Job Radar

Watches about 210 employers' job boards every 3 hours for **new-grad, entry-level and internship** roles in
RevOps, GTM Ops, GTM Strategy, Sales/Marketing Ops, BizOps and Strategy & Ops **in Austin**.
Healthcare is excluded. Each match gets a fit score (0–100), and you get a GitHub alert when a new role scores 70 or higher.

```
GitHub Actions (every 3h)  ──►  data/jobs.json  ──►  Claude morning sync (6:36am CT)  ──►  Austin Ops Radar dashboard
  Greenhouse · Lever · Ashby                         + daily web sweep for roles
  Workday · SmartRecruiters                            outside the watch list
        │
        └─► GitHub issue alert for new roles scoring ≥ 70
```

## One-time setup (about 10 minutes, no coding)

1. **Create the repo.** On github.com, click **New repository**. Name it `austin-job-radar`, choose **Public**, and click **Create repository**.
   It must be public so the morning sync can read the results, and so Actions minutes are free. Only job listings live here, nothing personal.
2. **Upload the files.** On the new repo page, click **uploading an existing file**. Drag in everything from this folder: `monitor.py`, `config.json`,
   `companies.csv`, `README.md`, and the `tools`, `data` and `.github` folders. Then click **Commit changes**.
   - On a Mac, Finder hides `.github`. Press **Cmd + Shift + .** to show it.
   - If the `.github` folder doesn't upload, click **Add file → Create new file**, name it `.github/workflows/monitor.yml`,
     paste in the contents of that file, and commit.
3. **Run the first scan.** Open the **Actions** tab. If asked, click **I understand… enable them**. Then click **Job Radar → Run workflow**.
   The scan takes about 3–6 minutes. When it finishes, `data/jobs.json` and `data/health.json` appear in the repo.
4. **Connect the dashboard.** Open the *Austin Ops Radar* artifact in Claude and type `your-username/austin-job-radar` in the setup box.
   The next morning sync pulls the results in.
5. **Phone alerts (optional).** Install the GitHub mobile app and turn on notifications. Alerts come as issues titled “New roles · …”
   that @mention you. Close an issue once you've looked at it.

## Day to day

- Open the dashboard. **NEW** marks roles first seen since your last visit.
- Use **Save**, **Skip**, or the status menu (Applied → Interviewing → Offer / Rejected) to move roles between tabs.
- To run a scan right now: **Actions → Job Radar → Run workflow**.

## Tuning

| Change | Edit |
|---|---|
| Add or remove a company | `companies.csv`: `ats` is greenhouse / lever / ashby / workday / smartrecruiters; `board` is the token from the careers URL; set `active` to 0 to pause a company |
| Title patterns, excluded words, years cap, skills you want scored | `config.json` |
| Include remote-US roles in the data | `keep_remote_us` in `config.json` (the dashboard still defaults to Austin-only) |
| Alert threshold | `alert_min_score` in `config.json` |
| Scan frequency | `cron` in `.github/workflows/monitor.yml` (GitHub's minimum is every 5 minutes; every 3 hours is plenty) |

**Finding a board token:** open the company's careers page and click a job. The token is in the URL:
- `job-boards.greenhouse.io/`**`alertmedia`**`/jobs/…`
- `jobs.lever.co/`**`arrivelogistics`**`/…`
- `jobs.ashbyhq.com/`**`ramp`**`/…`
- Workday: copy `company.wd5.myworkdayjobs.com/SiteName`

**Board health:** `data/health.json` lists every company with `ok`, the number of postings it has, and the number that matched.
A board that fails every run has probably moved. Fix its token, or set `active` to 0.

## Limits

- Workday boards are searched by keyword ("operations", "strategy", "revenue", …), so an oddly titled Workday role can slip through. The morning web sweep is the backstop.
- Years of experience are read from the description text. A posting that never states a number can't be filtered on it.
- LinkedIn and Indeed can't be read directly. Roles posted only there come in through the web sweep.
