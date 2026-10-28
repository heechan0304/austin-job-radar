#!/usr/bin/env python3
"""Austin Job Radar — polls company job boards and keeps data/jobs.json current.

Sources: Greenhouse, Lever, Ashby, SmartRecruiters, Workday (public JSON endpoints).
Standard library only, so the GitHub Action needs no installs.

Usage:
  python monitor.py                 # full run, writes data/
  python monitor.py --only alertmedia,ramp   # just these board tokens (testing)
  python monitor.py --dry           # don't write files
"""
import argparse
import csv
import hashlib
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
CFG = json.loads((ROOT / "config.json").read_text())
UA = "Mozilla/5.0 (job-radar; personal job search)"
NOW = dt.datetime.now(dt.timezone.utc)
KEEP_INACTIVE_DAYS = 30


# ---------------------------------------------------------------- http
def http_json(url, body=None, timeout=25, tries=2):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (404, 410, 401, 403):
                break
        except Exception as e:  # timeouts, resets
            last = e
        time.sleep(1.5 * (i + 1))
    raise last


# ---------------------------------------------------------------- text helpers
TAG = re.compile(r"<[^>]+>")


def strip_html(s):
    if not s:
        return ""
    s = html.unescape(html.unescape(s))
    return re.sub(r"\s+", " ", TAG.sub(" ", s)).strip()


def rx_any(patterns, text):
    return any(re.search(p, text, re.I) for p in patterns)


def iso(ts):
    """Accept ISO strings or epoch ms; return ISO8601 UTC or None."""
    if ts is None or ts == "":
        return None
    try:
        if isinstance(ts, (int, float)):
            return dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).isoformat()
        return dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(dt.timezone.utc).isoformat()
    except Exception:
        return None


def workday_posted(s):
    """'Posted Today' / 'Posted Yesterday' / 'Posted 5 Days Ago' / 'Posted 30+ Days Ago'."""
    if not s:
        return None
    s = s.lower()
    days = 0 if "today" in s else 1 if "yesterday" in s else None
    m = re.search(r"(\d+)\+?\s*day", s)
    if m:
        days = int(m.group(1))
    if days is None:
        return None
    return (NOW - dt.timedelta(days=days)).isoformat()


# ---------------------------------------------------------------- adapters
# Each returns a list of dicts: id, title, locations[list], url, posted, description, workplace
def fetch_greenhouse(token):
    j = http_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    out = []
    for p in j.get("jobs", []):
        locs = [p.get("location", {}).get("name", "")]
        locs += [o.get("location") or o.get("name", "") for o in p.get("offices", []) or []]
        out.append(dict(id=str(p["id"]), title=p.get("title", ""), locations=[l for l in locs if l],
                        url=p.get("absolute_url"), posted=iso(p.get("first_published") or p.get("updated_at")),
                        description=strip_html(p.get("content", ""))))
    return out


def fetch_lever(token):
    j = http_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    out = []
    for p in j:
        c = p.get("categories", {}) or {}
        locs = [c.get("location", "")] + list(c.get("allLocations", []) or [])
        desc = " ".join([p.get("descriptionPlain", "") or ""] +
                        [strip_html(x.get("content", "")) + " " + x.get("text", "") for x in p.get("lists", []) or []] +
                        [p.get("additionalPlain", "") or ""])
        out.append(dict(id=p["id"], title=p.get("text", ""), locations=[l for l in locs if l],
                        url=p.get("hostedUrl"), posted=iso(p.get("createdAt")), description=desc,
                        workplace=p.get("workplaceType", "")))
    return out


def fetch_ashby(token):
    j = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false")
    out = []
    for p in j.get("jobs", []):
        if p.get("isListed") is False:
            continue
        locs = [p.get("location", "")] + [s.get("location", "") for s in p.get("secondaryLocations", []) or []]
        if p.get("isRemote"):
            locs.append("Remote")
        out.append(dict(id=p.get("id") or p.get("jobUrl"), title=p.get("title", ""), locations=[l for l in locs if l],
                        url=p.get("jobUrl"), posted=iso(p.get("publishedAt")),
                        description=p.get("descriptionPlain") or strip_html(p.get("descriptionHtml", "")),
                        workplace=p.get("workplaceType", "")))
    return out


def fetch_smartrecruiters(token):
    out, offset = [], 0
    while True:
        j = http_json(f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset={offset}")
        for p in j.get("content", []):
            loc = p.get("location", {}) or {}
            locs = [", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)]
            if loc.get("remote"):
                locs.append("Remote")
            out.append(dict(id=p["id"], title=p.get("name", ""), locations=locs,
                            url=f"https://jobs.smartrecruiters.com/{token}/{p['id']}",
                            posted=iso(p.get("releasedDate")), description="", needs_detail=True,
                            detail_url=p.get("ref")))
        offset += 100
        if offset >= j.get("totalFound", 0) or offset > 2000:
            break
    return out


WD_SEARCHES = ["operations", "strategy", "revenue", "go to market", "business analyst", "intern"]


def fetch_workday(board):
    # board looks like "dell.wd1.myworkdayjobs.com/External"
    # or "wd1.myworkdaysite.com/recruiting/<tenant>/<site>"
    host, site = board.split("/", 1)
    site = re.sub(r"^[a-z]{2}-[A-Z]{2}/", "", site).strip("/")
    if site.startswith("recruiting/"):
        _, tenant, site = site.split("/", 2)
        page_base = f"https://{host}/recruiting/{tenant}/{site}"
    else:
        tenant = host.split(".")[0]
        page_base = f"https://{host}/en-US/{site}"
    api = f"https://{host}/wday/cxs/{tenant}/{site}"
    seen, out = set(), []
    for q in WD_SEARCHES:
        offset = 0
        while offset < 200:
            j = http_json(f"{api}/jobs", body={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q})
            posts = j.get("jobPostings", []) or []
            for p in posts:
                path = p.get("externalPath")
                if not path or path in seen:
                    continue
                seen.add(path)
                out.append(dict(id=path, title=p.get("title", ""), locations=[p.get("locationsText", "")],
                                url=f"{page_base}{path}", posted=workday_posted(p.get("postedOn")),
                                description="", needs_detail=True, detail_url=f"{api}{path}"))
            offset += 20
            if len(posts) < 20 or offset >= (j.get("total") or 0):
                break
    return out


def workday_detail(job):
    j = http_json(job["detail_url"]).get("jobPostingInfo", {})
    locs = [j.get("location", "")] + list(j.get("additionalLocations", []) or [])
    job["locations"] = [l for l in locs if l] or job["locations"]
    job["description"] = strip_html(j.get("jobDescription", ""))
    if j.get("startDate"):
        job["posted"] = iso(j["startDate"] + "T12:00:00+00:00") or job["posted"]


def smartrecruiters_detail(job):
    j = http_json(job["detail_url"])
    secs = (j.get("jobAd", {}) or {}).get("sections", {}) or {}
    job["description"] = " ".join(strip_html(v.get("text", "")) for v in secs.values() if isinstance(v, dict))
    if j.get("postingUrl"):
        job["url"] = j["postingUrl"]


ADAPTERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "smartrecruiters": fetch_smartrecruiters, "workday": fetch_workday}
DETAIL = {"workday": workday_detail, "smartrecruiters": smartrecruiters_detail}


# ---------------------------------------------------------------- classification
def role_family(title):
    for fam, pats in CFG["role_families"].items():
        if rx_any(pats, title):
            return fam
    return None


def location_type(locs, description, workplace=""):
    blob = " | ".join(locs).lower()
    if rx_any(CFG["location_include"], blob):
        if "remote" in blob or str(workplace).lower() == "remote":
            return "Austin / Remote"
        if "hybrid" in blob or str(workplace).lower() == "hybrid":
            return "Austin (Hybrid)"
        return "Austin"
    if re.search(r"\b\d+ locations\b", blob) and re.search(r"\baustin\b", description, re.I):
        return "Austin"
    foreign = re.compile(r"\b(canada|uk|united kingdom|london|emea|apac|latam|india|germany|ireland|mexico|brazil|europe|australia|japan|singapore|poland|spain|france|netherlands|philippines|israel|argentina|colombia)\b", re.I)
    remote_wp = str(workplace).lower() == "remote"
    us_remote = any(("remote" in l.lower() or remote_wp) and not foreign.search(l) for l in locs)
    if us_remote and CFG.get("keep_remote_us"):
        return "Remote (US)"
    return None


YEARS = re.compile(r"(\d{1,2})\s*(?:\+|-|–|to)?\s*(\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b(?:[^.]{0,40})(?:experience|exp\b)", re.I)


def min_years(desc):
    vals = []
    for m in YEARS.finditer(desc or ""):
        a = int(m.group(1))
        if a <= 15:
            vals.append(a)
    return min(vals) if vals else None


def score_job(job, fam, is_intern, years, entry_hit, desc_l):
    s = 45
    if entry_hit:
        s += 15
    if years is not None:
        s += {0: 12, 1: 12, 2: 3}.get(years, -25)
    if re.search(r"\b(associate|analyst|coordinator|specialist)\b", job["title"], re.I):
        s += 5
    if is_intern:
        s += 3
    kw = 0
    hits = []
    for k, w in CFG["resume_keywords"].items():
        if re.search(r"\b" + k + r"\b", desc_l):
            kw += w
            hits.append(k.replace("?", "").replace("\\", ""))
    s += min(kw, 25)
    if job["location_type"] and job["location_type"].startswith("Austin"):
        s += 5
    return max(0, min(100, s)), hits[:8]


def classify(company, raw):
    title = re.sub(r"\s+", " ", raw["title"]).strip()
    fam = role_family(title)
    if not fam or rx_any(CFG["title_exclude"], title):
        return None
    return fam


def finalize(company, raw, fam):
    desc = raw.get("description", "") or ""
    desc_l = desc.lower()
    loc_type = location_type(raw["locations"], desc, raw.get("workplace", ""))
    if not loc_type:
        return None
    hc = sum(1 for p in CFG["healthcare_terms"] if re.search(p, desc_l)) + (3 if company.get("industry", "").lower().startswith("health") else 0)
    if hc >= CFG["healthcare_threshold"]:
        return None
    years = min_years(desc)
    if years is not None and years > CFG["max_years_required"]:
        return None
    title = raw["title"].strip()
    is_intern = rx_any(CFG["intern_patterns"], title)
    entry_hit = rx_any(CFG["entry_signals"], title + " " + desc_l)
    flags = []
    if is_intern:
        if re.search(r"summer", title + " " + desc_l[:1500], re.I):
            flags.append("Summer internship: check post-grad eligibility")
        if rx_any(CFG["intern_postgrad_risk"], desc_l):
            flags.append("Requires returning to school")
    if years == 2:
        flags.append("Asks for 2 yrs")
    job = dict(title=title, company=company["name"], industry=company.get("industry", ""),
               family=fam, type="Internship" if is_intern else "Full-time",
               location=", ".join(dict.fromkeys(l for l in raw["locations"] if l))[:160],
               location_type=loc_type, url=raw["url"], posted=raw.get("posted"),
               years_required=years, entry_level_signal=bool(entry_hit), flags=flags,
               source=company["ats"])
    job["score"], job["matched_skills"] = score_job(job, fam, is_intern, years, entry_hit, desc_l)
    if "Requires returning to school" in flags:
        job["score"] = max(0, job["score"] - 30)
    job["snippet"] = desc[:400]
    return job


# ---------------------------------------------------------------- run
def load_companies():
    rows = []
    with open(ROOT / "companies.csv", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("active", "1").strip() in ("0", "no", "false"):
                continue
            r["ats"] = r["ats"].strip().lower()
            r["board"] = r["board"].strip()
            if r["ats"] in ADAPTERS and r["board"]:
                rows.append(r)
    return rows


def run_company(c):
    t0 = time.time()
    raws = ADAPTERS[c["ats"]](c["board"])
    jobs = []
    for raw in raws:
        fam = classify(c, raw)
        if not fam:
            continue
        if raw.get("needs_detail"):
            try:
                DETAIL[c["ats"]](raw)
            except Exception:
                pass
        j = finalize(c, raw, fam)
        if j:
            j["id"] = f"{c['ats']}:{c['board']}:{raw['id']}"
            j["key"] = hashlib.sha1(j["id"].encode()).hexdigest()[:16]
            jobs.append(j)
    return dict(total=len(raws), matched=len(jobs), seconds=round(time.time() - t0, 1)), jobs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated board values to run")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    companies = load_companies()
    if args.only:
        want = {x.strip().lower() for x in args.only.split(",")}
        companies = [c for c in companies if c["board"].lower() in want or c["name"].lower() in want]

    jobs_path = DATA / "jobs.json"
    prev = json.loads(jobs_path.read_text()) if jobs_path.exists() else {"jobs": {}}
    first_run = not prev["jobs"]
    store = prev["jobs"]
    health, found, ok_boards = {}, {}, set()

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(run_company, c): c for c in companies}
        for f in as_completed(futs):
            c = futs[f]
            try:
                stats, jobs = f.result()
                health[c["name"]] = dict(ats=c["ats"], board=c["board"], ok=True, **stats)
                ok_boards.add(c["board"])
                for j in jobs:
                    found[j["id"]] = j
            except Exception as e:
                health[c["name"]] = dict(ats=c["ats"], board=c["board"], ok=False, error=str(e)[:200])

    now = NOW.isoformat()
    new_ids = []
    for jid, j in found.items():
        old = store.get(jid)
        if old:
            j["first_seen"] = old.get("first_seen", now)
        else:
            j["first_seen"] = now
            new_ids.append(jid)
        j["last_seen"] = now
        j["active"] = True
        store[jid] = j
    cutoff = (NOW - dt.timedelta(days=KEEP_INACTIVE_DAYS)).isoformat()
    for jid in list(store):
        j = store[jid]
        board = jid.split(":", 2)[1] if jid.count(":") >= 2 else ""
        if jid not in found and board in ok_boards:
            j["active"] = False
        if j.get("last_seen", now) < cutoff:
            del store[jid]

    active = [j for j in store.values() if j.get("active")]
    summary = dict(updated_at=now, companies_checked=len(companies),
                   companies_ok=sum(1 for h in health.values() if h["ok"]),
                   active_jobs=len(active), new_this_run=len(new_ids))
    print(json.dumps(summary, indent=2))
    for jid in sorted(new_ids, key=lambda k: -found[k]["score"]):
        j = found[jid]
        print(f"  NEW [{j['score']:>3}] {j['company']} — {j['title']} ({j['location_type']})")
    bad = [f"{n} ({h['ats']}:{h['board']}): {h.get('error')}" for n, h in health.items() if not h["ok"]]
    if bad:
        print(f"\n{len(bad)} boards failed:")
        for b in sorted(bad):
            print("  " + b)

    if args.dry:
        return
    DATA.mkdir(exist_ok=True)
    jobs_path.write_text(json.dumps(dict(summary, jobs=store), indent=1, sort_keys=True))
    (DATA / "health.json").write_text(json.dumps(dict(updated_at=now, boards=health), indent=1, sort_keys=True))

    # Alert file for the workflow (skipped on the very first run so you aren't flooded)
    hot = [found[k] for k in new_ids if found[k]["score"] >= CFG["alert_min_score"]]
    alert = DATA / "alert.md"
    if hot and not first_run:
        lines = [f"**{len(hot)} new high-fit role(s)**\n"]
        for j in sorted(hot, key=lambda x: -x["score"]):
            fl = f" — ⚠ {'; '.join(j['flags'])}" if j["flags"] else ""
            lines.append(f"- **{j['score']}** · [{j['company']} — {j['title']}]({j['url']}) · {j['location_type']}{fl}")
        alert.write_text("\n".join(lines) + "\n")
    elif alert.exists():
        alert.unlink()


if __name__ == "__main__":
    sys.exit(main())
