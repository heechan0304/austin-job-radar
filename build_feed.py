#!/usr/bin/env python3
"""Turn data/jobs.json into compact feed documents for the Claude dashboard.

Usage: python tools/build_feed.py <jobs.json path or URL> <out_dir>
Writes out_dir/feed_p0.json, feed_p1.json, ... (each under ~200 KB) and out_dir/meta.json.
"""
import json, sys, urllib.request, pathlib, datetime as dt

src, out = sys.argv[1], pathlib.Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
raw = urllib.request.urlopen(src).read() if src.startswith("http") else open(src, "rb").read()
d = json.loads(raw)
cut = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=21)).isoformat()
rows = []
for j in d["jobs"].values():
    if not j.get("active") and j.get("last_seen", "") < cut:
        continue
    rows.append({k: j.get(k) for k in ("key", "title", "company", "industry", "family", "type", "location",
                 "location_type", "url", "posted", "first_seen", "last_seen", "active", "score",
                 "years_required", "flags", "matched_skills", "source")})
    rows[-1]["origin"] = "radar"
rows.sort(key=lambda r: (-(r["score"] or 0), r["key"]))
parts, cur, size = [], [], 0
for r in rows:
    n = len(json.dumps(r))
    if cur and size + n > 200_000:
        parts.append(cur); cur, size = [], 0
    cur.append(r); size += n
parts.append(cur)
for i, p in enumerate(parts):
    (out / f"feed_p{i}.json").write_text(json.dumps({"part": i, "parts": len(parts), "jobs": p}))
meta = {k: d.get(k) for k in ("updated_at", "companies_checked", "companies_ok", "active_jobs", "new_this_run")}
meta["parts"] = len(parts)
(out / "meta.json").write_text(json.dumps(meta))
print(json.dumps(meta))
