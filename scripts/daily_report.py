"""Case Desk daily reports and backup, run by .github/workflows/daily-report.yml.

  python daily_report.py morning   — the day's matters (High Court lists x our cases), drafts and tasks due
  python daily_report.py evening   — what was recorded today, what is still missing, data problems
  python daily_report.py backup    — every collection to backup/<date>/*.json (kept as a private build artifact)

Reads Firestore with a service account (GitHub secret FIREBASE_SERVICE_ACCOUNT, the JSON key). The report is
posted in Instant (the team's group chat, so it reaches the Desk and the app) and, when RESEND_API_KEY and
REPORT_EMAILS are set, e-mailed too. Nothing else is changed in the database."""
import datetime as dt
import json
import os
import re
import sys
import urllib.parse
import urllib.request

import google.auth.transport.requests
from google.oauth2 import service_account

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SITE = "https://sh-chhaba.pages.dev"
BOT = "casedesk-bot"


def creds():
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    if not raw:
        print("::notice::FIREBASE_SERVICE_ACCOUNT is not set yet — reports and backups are off until it is added.")
        sys.exit(0)
    info = json.loads(raw)
    c = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/datastore"])
    c.refresh(google.auth.transport.requests.Request())
    return info["project_id"], c.token


PROJECT, TOKEN = (None, None)


def api(path, body=None, method=None):
    url = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents{path}"
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 method=method or ("POST" if body is not None else "GET"),
                                 headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def val(v):
    if "stringValue" in v: return v["stringValue"]
    if "integerValue" in v: return int(v["integerValue"])
    if "doubleValue" in v: return v["doubleValue"]
    if "booleanValue" in v: return v["booleanValue"]
    if "nullValue" in v: return None
    if "timestampValue" in v: return v["timestampValue"]
    if "mapValue" in v: return {k: val(x) for k, x in v["mapValue"].get("fields", {}).items()}
    if "arrayValue" in v: return [val(x) for x in v["arrayValue"].get("values", [])]
    return None


def enc(x):
    if x is None: return {"nullValue": None}
    if isinstance(x, bool): return {"booleanValue": x}
    if isinstance(x, int): return {"integerValue": str(x)}
    if isinstance(x, float): return {"doubleValue": x}
    if isinstance(x, dict): return {"mapValue": {"fields": {k: enc(v) for k, v in x.items()}}}
    if isinstance(x, list): return {"arrayValue": {"values": [enc(v) for v in x]}}
    return {"stringValue": str(x)}


def collection(name):
    out, tok = [], ""
    while True:
        j = api(f"/{name}?pageSize=300" + (f"&pageToken={urllib.parse.quote(tok)}" if tok else ""))
        for d in j.get("documents", []):
            out.append({**{k: val(v) for k, v in d.get("fields", {}).items()}, "id": d["name"].rsplit("/", 1)[1]})
        tok = j.get("nextPageToken")
        if not tok:
            return out


def doc(path):
    try:
        d = api(path)
        return {k: val(v) for k, v in d.get("fields", {}).items()}
    except Exception:
        return None


def get_json(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "casedesk-report"}), timeout=60) as r:
            return json.loads(r.read())
    except Exception:
        return None


# ---- the same case-number matching as the Desk (keyOf / famOf / sameCase) ----
def key_of(s):
    m = re.search(r"(\d{1,6})\s*/\s*(\d{4})", str(s or ""))
    return f"{int(m.group(1))}/{m.group(2)}" if m else ""


def fam_of(s):
    t = re.sub(r"\d[\s\S]*$", "", str(s or "").upper())
    t = re.sub(r"[^A-Z]", "", t)
    t = re.sub(r"^(SB|DB)", "", t)
    t = re.sub(r"NO$", "", t)
    if not t: return ""
    for pat, f in ((r"REVIEW|^WRW|^CRW", "REV"), (r"CONTEMPT|^CCP", "CCP"), (r"SPECIALAPPEAL|^SAW", "SAW"),
                   (r"RESTOR|^CRES|^RESAPPL", "RES"), (r"FIRSTAPPEAL|^CFA|ISTAPPL", "CFA"),
                   (r"SECONDAPPEAL|^CSA|IINDAPPL", "CSA"), (r"MISCAPPEAL|^CMA", "CMA"), (r"WRIT|^CWP?$", "CW")):
        if re.search(pat, t): return f
    return t


def same_case(a, b):
    k = key_of(a)
    if not k or k != key_of(b): return False
    x, y = fam_of(a), fam_of(b)
    return not x or not y or x == y


def fmt(d):
    try: return dt.date.fromisoformat(d).strftime("%d %b %Y")
    except Exception: return d or ""


def ms_day(ms):
    try: return dt.datetime.fromtimestamp(int(ms) / 1000, IST).date().isoformat()
    except Exception: return ""


def load():
    staff = {s["id"]: s for s in collection("staff")}
    cases = [c for c in collection("cases") if c.get("stage") != "disposed"]
    tasks = [t for t in collection("tasks") if not t.get("done")]
    return staff, cases, tasks


def name(staff, uid):
    return (staff.get(uid) or {}).get("name") or "—"


def listed_today(cases, day):
    """our cases on the day's High Court lists (both benches), plus the chamber's own uploaded causelist"""
    out = {}
    for b, bn in (("jp", "Jaipur"), ("jdp", "Jodhpur")):
        ks = get_json(f"{SITE}/api/hc/data/{b}/{day}/keys.json") or {}
        by = {}
        for c in cases:
            k = key_of(c.get("caseNo"))
            if k: by.setdefault(k, []).append(c)
        for cno, court, t, n, lid in ks.get("keys", []):
            for c in by.get(key_of(cno), []):
                if same_case(c.get("caseNo"), cno) and c["id"] not in out:
                    out[c["id"]] = dict(c=c, where=f"Court {court} ({bn}) · {t} · item {n}", court=court, n=n)
    cl = doc(f"/causelists/{day}") or {}
    for it in cl.get("items") or []:
        for c in cases:
            if c["id"] not in out and it.get("key") and it.get("key") == key_of(c.get("caseNo")) and same_case(it.get("caseNo") or c.get("caseNo"), c.get("caseNo")):
                out[c["id"]] = dict(c=c, where=f"{it.get('court', '')} · item {it.get('item', '')}", court=it.get("court", ""), n=it.get("item", ""))
    num = lambda x: int(re.sub(r"\D", "", str(x)) or 999)
    return sorted(out.values(), key=lambda x: (num(x["court"]), num(x["n"])))


def morning(staff, cases, tasks, day):
    L = listed_today(cases, day)
    got = {x["c"]["id"] for x in L}
    nl = [c for c in cases if c.get("nextDate") == day and c["id"] not in got and not c.get("mainId")]
    t2 = (dt.date.fromisoformat(day) + dt.timedelta(days=3)).isoformat()
    drafts = [c for c in cases if c.get("stage") in ("assigned", "drafting") and c.get("dueDate") and c["dueDate"] <= t2]
    overdue_t = [t for t in tasks if t.get("due") and t["due"] < day]
    due_t = [t for t in tasks if t.get("due") == day]
    days = get_json(f"{SITE}/api/hc/data/days.json") or {}
    read_ok = day in (days.get("days") or {}).get("jp", [])
    lines = [f"🌅 Morning report · {fmt(day)}",
             f"High Court lists: {'read ✓' if read_ok else 'NOT READ YET ⚠'} · our matters listed: {len(L)}", ""]
    by_c = {}
    for x in L: by_c.setdefault(x["c"].get("counsel") or "", []).append(x)
    for cid in sorted(by_c, key=lambda u: name(staff, u)):
        lines.append(f"👤 {name(staff, cid) if cid else 'No counsel assigned'} · {len(by_c[cid])}")
        for x in by_c[cid]:
            c = x["c"]
            lines.append(f"  • {c.get('caseNo', '')} — {(c.get('title') or '')[:60]}\n    {x['where']}" + (f" · {c['dept']}" if c.get("dept") else ""))
        lines.append("")
    if nl:
        lines.append(f"⚠ Dated today but not in the lists · {len(nl)}")
        lines += [f"  • {c.get('caseNo', '')} — {(c.get('title') or '')[:60]}" for c in nl[:15]]
        lines.append("")
    if drafts:
        lines.append(f"✍ Drafts due in 3 days or overdue · {len(drafts)}")
        lines += [f"  • {c.get('caseNo', '')} · {name(staff, c.get('counsel'))} · due {fmt(c.get('dueDate'))}" for c in drafts[:15]]
        lines.append("")
    if due_t or overdue_t:
        lines.append(f"☑ Tasks: {len(due_t)} due today, {len(overdue_t)} overdue")
        lines += [f"  • {t.get('title', '')[:70]} · {name(staff, t.get('assignee'))}" for t in (due_t + overdue_t)[:12]]
    return "\n".join(lines).strip()


def evening(staff, cases, tasks, day):
    L = listed_today(cases, day)
    rec = lambda c: any(h.get("date") == day for h in (c.get("hearings") or []))
    done = [x for x in L if rec(x["c"])]
    miss = [x for x in L if not rec(x["c"])]
    passed = [c for c in cases if c.get("nextDate") and c["nextDate"] < day and not c.get("mainId")]
    touched = [c for c in cases if ms_day(c.get("updatedAt") or 0) == day]
    stale = [c for c in cases if c.get("updatedAt") and (dt.datetime.now(IST).timestamp() * 1000 - int(c["updatedAt"])) > 30 * 864e5 and not c.get("mainId")]
    nonext = [c for c in cases if c.get("stage") == "listed" and not c.get("nextDate") and not c.get("mainId")]
    tom = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
    tomorrow = listed_today(cases, tom) if dt.date.fromisoformat(tom).weekday() != 6 else []
    lines = [f"🌇 Evening report · {fmt(day)}",
             f"Listed today: {len(L)} · results recorded: {len(done)} · still to record: {len(miss)}",
             f"Cases updated today: {len(touched)}", ""]
    if miss:
        lines.append("⚠ Result not recorded yet")
        lines += [f"  • {x['c'].get('caseNo', '')} · {x['where']} · {name(staff, x['c'].get('counsel'))}" for x in miss[:20]]
        lines.append("")
    if tomorrow:
        lines.append(f"📅 Tomorrow ({fmt(tom)}) · {len(tomorrow)} listed so far")
        lines += [f"  • {x['c'].get('caseNo', '')} · {x['where']}" for x in tomorrow[:15]]
        lines.append("")
    lines.append("🩺 Needs attention")
    lines.append(f"  • Next date passed, not updated: {len(passed)}")
    lines.append(f"  • In court with no next date: {len(nonext)}")
    lines.append(f"  • Not touched for 30+ days: {len(stale)}")
    lines.append(f"  • Open tasks overdue: {len([t for t in tasks if t.get('due') and t['due'] < day])}")
    return "\n".join(lines).strip()


def post(text):
    """into Instant (the team chat); written by the report bot"""
    now = int(dt.datetime.now().timestamp() * 1000)
    api("/instant", {"fields": {"by": enc(BOT), "text": enc(text[:9000]), "t": enc(now), "report": enc(True)}})


def mail(subject, text):
    key, to = os.environ.get("RESEND_API_KEY", "").strip(), [x.strip() for x in os.environ.get("REPORT_EMAILS", "").split(",") if x.strip()]
    if not key or not to:
        return
    body = json.dumps({"from": (os.environ.get("REPORT_FROM") or "Case Desk <onboarding@resend.dev>"), "to": to, "subject": subject, "text": text}).encode()
    req = urllib.request.Request("https://api.resend.com/emails", data=body, method="POST",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        print("mail", r.status)


def backup(day):
    os.makedirs(f"backup/{day}", exist_ok=True)
    for name_ in ("cases", "staff", "tasks", "causelists", "contacts", "bills", "payouts", "settings", "activity", "instant"):
        try:
            rows = collection(name_)
        except Exception as e:
            print("skip", name_, e); continue
        json.dump(rows, open(f"backup/{day}/{name_}.json", "w"), ensure_ascii=False)
        print(name_, len(rows))


def main():
    global PROJECT, TOKEN
    mode = sys.argv[1] if len(sys.argv) > 1 else "morning"
    PROJECT, TOKEN = creds()
    day = dt.datetime.now(IST).date().isoformat()
    if mode == "backup":
        if os.environ.get("PUBLIC_REPO"):
            sys.exit("Backups are made in the private repository only.")
        return backup(day)
    if dt.date.fromisoformat(day).weekday() == 6:
        print("Sunday: no report"); return
    staff, cases, tasks = load()
    text = morning(staff, cases, tasks, day) if mode == "morning" else evening(staff, cases, tasks, day)
    print(f"{mode} report: {len(text.splitlines())} lines")  # the text itself stays out of the build log
    if os.environ.get("DRY_RUN"):
        print(text); return
    post(text)
    try:
        mail(("Morning" if mode == "morning" else "Evening") + " report · " + fmt(day), text)
    except Exception as e:  # the report is already in Instant; a mail problem is only noted
        print("::warning::The e-mail copy was not sent:", e)


if __name__ == "__main__":
    main()
