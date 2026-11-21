"""Every morning: read the Rajasthan High Court quick causelists (Jaipur and Jodhpur) into data.

For each bench and each day from today to three days ahead it asks the court's "Quick Download"
page which lists are out (court, judges, list types), downloads each PDF, reads it with
hc_parse.py and writes, under OUT/hc/<bench>/<date>/:
  index.json   the courts: judges, sitting time, VC details, roster, notes, list types, item counts
  keys.json    every case on the day's lists: [case no, court, list type, item, list id]
  c/<id>.json  the full rows of one list (title, advocates, department, remarks)
A list already read (same PDF bytes) is not read again. Older days are dropped.
Usage: python hc_fetch.py OUT_DIR [days_ahead]"""
import concurrent.futures as cf
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hc_parse  # noqa: E402

BENCH = {"jp": "https://hcraj.nic.in/quick-causelist-jp/", "jdp": "https://hcraj.nic.in/quick-causelist-jdp/"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
      "Accept-Language": "en-IN,en;q=0.9"}
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NAMES = {"D-A": "Daily · Part A", "D-B": "Daily · Part B", "S-A": "Supplementary · Part A", "S-B": "Supplementary · Part B",
         "W-A": "Weekly · Part A", "W-B": "Weekly · Part B", "T1SA": "Supplementary (urgent)"}


def get(url, data=None, tries=4):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=dict(UA, Referer=url.split("?")[0]))
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read()
        except Exception as e:  # the court's server is slow in the mornings
            if k == tries - 1:
                raise
            print("  retry", url[:90], e, flush=True)
            time.sleep(5 * (k + 1))  # the court refuses bursts; back off and try again


def strip(h):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h or ""))).strip()


def index_rows(bench, day):
    body = urllib.parse.urlencode({"day": day.day, "month": day.month, "year": day.year}).encode()
    h = get(BENCH[bench], body).decode("utf-8", "replace")
    tb = re.search(r"<table[^>]*id=[\"']causelist-table[\"'][\s\S]*?</table>", h, re.I)
    rows = []
    for tr in re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", tb.group(0) if tb else "", re.I):
        if "<th" in tr.lower():
            continue
        c = re.findall(r"<td[^>]*>([\s\S]*?)</td>", tr, re.I)
        if len(c) < 3:
            continue
        ls = [(strip(t), p) for p, t in re.findall(r"data-pdfpath=[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)</a>", c[2], re.I)]
        rows.append({"court": strip(c[0]), "judges": strip(c[1]), "lists": ls})
    return rows


def read_pdf(job):
    bench, path64, old, deadline = job
    # a list already read this day is kept as it is (a revised or new list comes out under a new name)
    if old and old.get("_items"):
        return path64, old.get("sha", ""), old["_meta"], old["_items"], True
    if time.time() > deadline:  # out of time for this run: the next run picks it up
        return path64, "", {}, [], False
    try:
        raw = get(BENCH[bench] + "?path=" + urllib.parse.quote(path64))
    except Exception as e:  # keep the earlier copy, or leave this one list empty; never fail the day for it
        print("  gave up on one list:", repr(e)[:120], flush=True)
        if old and old.get("_items") is not None:
            return path64, old.get("sha", ""), old["_meta"], old["_items"], True
        return path64, "", {}, [], False
    sha = hashlib.sha1(raw).hexdigest()[:16]
    if old and old.get("sha") == sha and old.get("_items") is not None:
        return path64, sha, old["_meta"], old["_items"], True
    if raw[:4] != b"%PDF":
        return path64, sha, {}, [], False
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(raw)
        fn = f.name
    try:
        d = hc_parse.parse(fn)
    finally:
        os.unlink(fn)
    return path64, sha, d["meta"], d["items"], False


def load_old(dd):
    """What an earlier run wrote for this day, so unchanged lists are not read again."""
    old = {}
    try:
        idx = json.load(open(os.path.join(dd, "index.json")))
    except Exception:
        return old
    for row in idx.get("courts", []):
        for l in row.get("lists", []):
            fn = os.path.join(dd, "c", l["id"] + ".json")
            try:
                items = json.load(open(fn))
            except Exception:
                continue
            meta = {k: l.get(k) for k in ("list", "time", "vc", "roster", "notes", "judges")}
            old[l["p"]] = {"sha": l.get("sha"), "_meta": meta, "_items": items}
    return old


def slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:20] or "x"


def run_day(out, bench, day, pool, deadline):
    iso = day.isoformat()
    dd = os.path.join(out, "hc", bench, iso)
    rows = index_rows(bench, day)
    if not rows:
        print(bench, iso, "no lists yet", flush=True)
        return 0
    old = load_old(dd)
    jobs = [(bench, p, old.get(p), deadline) for r in rows for (_, p) in r["lists"]]
    got = {}
    for res in pool.map(read_pdf, jobs):
        got[res[0]] = res
    tmp = dd + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.join(tmp, "c"))
    courts, keys, reused = [], [], 0
    for ri, r in enumerate(rows):
        row = {"court": r["court"], "judges": r["judges"], "lists": []}
        for t, p in r["lists"]:
            _, sha, meta, items, was = got.get(p, (p, "", {}, [], False))
            reused += 1 if was else 0
            lid = slug(r["court"]) + "_" + slug(t) + "_" + str(ri)
            json.dump(items, open(os.path.join(tmp, "c", lid + ".json"), "w"), ensure_ascii=False, separators=(",", ":"))
            row["lists"].append({
                "id": lid, "t": t, "name": NAMES.get(t, t), "p": p, "sha": sha, "n": len(items),
                "list": meta.get("list", ""), "time": meta.get("time", []), "vc": meta.get("vc", ""),
                "roster": meta.get("roster", ""), "notes": meta.get("notes", []), "judges": meta.get("judges", []),
            })
            for it in items:
                for cno in [it["c"]] + it.get("cs", []):
                    keys.append([cno, r["court"], t, it["n"], lid])
        courts.append(row)
    now = dt.datetime.now(IST).isoformat(timespec="seconds")
    json.dump({"bench": bench, "date": iso, "at": now, "courts": courts}, open(os.path.join(tmp, "index.json"), "w"),
              ensure_ascii=False, separators=(",", ":"))
    json.dump({"bench": bench, "date": iso, "at": now, "keys": keys}, open(os.path.join(tmp, "keys.json"), "w"),
              ensure_ascii=False, separators=(",", ":"))
    shutil.rmtree(dd, ignore_errors=True)
    os.rename(tmp, dd)
    print(bench, iso, len(rows), "court rows,", len(jobs), "lists (", reused, "unchanged ),", len(keys), "cases", flush=True)
    return len(keys)


def main():
    out = sys.argv[1]
    ahead = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    today = dt.datetime.now(IST).date()
    days = [today + dt.timedelta(days=k) for k in range(0, ahead + 1)]
    ok = True
    deadline = time.time() + 32 * 60  # the job is stopped at 50 minutes; leave time to publish
    # two at a time: the court's server refuses more parallel downloads than that
    with cf.ProcessPoolExecutor(max_workers=2) as pool:
        for bench in BENCH:
            for day in days:
                if day.weekday() == 6:  # Sunday
                    continue
                try:
                    run_day(out, bench, day, pool, deadline)
                except Exception as e:
                    ok = False
                    print("FAILED", bench, day, repr(e), flush=True)
            # keep yesterday for late updates; drop anything older
            bd = os.path.join(out, "hc", bench)
            for name in os.listdir(bd) if os.path.isdir(bd) else []:
                try:
                    if dt.date.fromisoformat(name) < today - dt.timedelta(days=1):
                        shutil.rmtree(os.path.join(bd, name))
                except ValueError:
                    pass
    manifest = {}
    for bench in BENCH:
        bd = os.path.join(out, "hc", bench)
        manifest[bench] = sorted(n for n in (os.listdir(bd) if os.path.isdir(bd) else []) if re.match(r"\d{4}-\d\d-\d\d$", n))
    json.dump({"at": dt.datetime.now(IST).isoformat(timespec="seconds"), "days": manifest},
              open(os.path.join(out, "hc", "days.json"), "w"))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
