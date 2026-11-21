"""Read one Rajasthan High Court causelist PDF (quick-download copy) into plain data.

The PDFs are made by the court's own software (TCPDF) and always look the same:
a preamble (court, judges, sitting time, VC details, roster, notes), then a table
S.No | Case Type/No | Title of The Case | Petitioner Advocate | Respondent Advocate
that runs over many pages, with section heads (PART - A, FOR ADMISSION ...) in between."""
import re
import pdfplumber

CASE_RE = re.compile(r"([A-Z][A-Z0-9.()&/\- ]{1,40}?\.?\s*\d{1,6}\s*/\s*\d{4})")
TIME_RE = re.compile(r"\d{1,2}:\d{2}\s*[AP]M\s*TO\s*\d{1,2}:\d{2}\s*[AP]M", re.I)
SKIP_RE = re.compile(r"^(Page \d+ of \d+|.*Bench Id\s*:|.*Causelist \(Filtered\)|.*Dated\s*:\s*\d|\d\d/\d\d/\d{4}\s+\d\d:\d\d)", re.I)


def _lines(page):
    ws = page.extract_words(keep_blank_chars=False, use_text_flow=False, extra_attrs=["fontname", "size"])
    rows = []
    for w in sorted(ws, key=lambda w: (round(w["top"]), w["x0"])):
        if rows and abs(rows[-1]["top"] - w["top"]) <= 2.5:
            rows[-1]["w"].append(w)
        else:
            rows.append({"top": w["top"], "w": [w]})
    for r in rows:
        r["w"].sort(key=lambda w: w["x0"])
        r["text"] = " ".join(w["text"] for w in r["w"])
        r["bold"] = all("Bold" in w["fontname"] for w in r["w"])
    return rows


def _cols(row, rows=()):
    """Column starts from the table header row, or None. Header words sit centred over their
    columns, so the true left edges are taken from the data itself where it shows them."""
    t = row["text"]
    if not (t.startswith("S.No") and "Petitioner" in t and "Respondent" in t):
        return None
    xs = {}
    for w in row["w"]:
        for k, key in (("Case", "case"), ("Title", "title"), ("Petitioner", "pet"), ("Respondent", "resp")):
            if w["text"].startswith(k) and key not in xs:
                xs[key] = w["x0"]
    if len(xs) != 4:
        return None
    hw = {w["text"]: w for w in row["w"]}
    tn = next((w for w in row["w"] if w["text"].startswith("Type")), None)
    case_end = tn["x1"] if tn else xs["case"] + 60
    tx = [w["x0"] for r in rows for w in r["w"] if (w["text"] in ("VS", "VS.") or w["text"].startswith("[")) and case_end - 10 < w["x0"] < xs["title"]]
    adv = [w for w in row["w"] if w["text"] == "Advocate"]
    lastcase = [w for w in row["w"] if w["text"] == "Case" and w["x0"] > xs["title"]]
    pet_b = ((lastcase[0]["x1"] if lastcase else xs["title"] + 80) + xs["pet"]) / 2
    resp_b = ((adv[0]["x1"] if adv else xs["pet"] + 90) + xs["resp"]) / 2
    return {"case": xs["case"] - 14, "title": (min(tx) if tx else case_end + 20), "pet": pet_b, "resp": resp_b}


def _join(parts):
    return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()


def parse(path):
    pdf = pdfplumber.open(path)
    meta = {"list": "", "court": "", "judges": [], "time": [], "vc": "", "roster": "", "notes": [], "date": ""}
    items, cur, head = [], None, ""
    cols, in_pre, note_on, done = None, True, False, False
    for pno, page in enumerate(pdf.pages):
        if done:
            break
        H = page.height
        rows = _lines(page)
        for r in rows:
            t = r["text"].strip()
            if not t or r["top"] > H - 45 or SKIP_RE.match(t):
                continue
            c = _cols(r, rows)
            if c:
                cols, in_pre, note_on = c, False, False
                continue
            if in_pre:
                m = TIME_RE.findall(t)
                if m:
                    meta["time"] += [re.sub(r"\s+", " ", x.upper()) for x in m]
                    t = TIME_RE.sub("", t).strip()
                if not t:
                    continue
                if re.search(r"Causelist", t, re.I) and not meta["list"]:
                    meta["list"] = t.replace("*", "").strip(); continue
                if re.match(r"^\w+day Dated", t):
                    meta["date"] = t.split(":")[-1].strip(); continue
                mc = re.match(r"^Court No\s*:\s*(.+)$", t, re.I)
                if mc:
                    meta["court"] = mc.group(1).strip(); continue
                if t.startswith("HON'BLE") or t.startswith("HONBLE") or (r["bold"] and "JUSTICE" in t and "AUTHORITY" not in t):
                    meta["judges"].append(t); continue
                if t.startswith("[") and ("WEBEX" in t or "MEETING" in t or "VC" in t):
                    meta["vc"] = t.strip("[] "); continue
                if "THIS CAUSE LIST IS PUBLISHED" in t or t.startswith("Disclaimer") or t in ("the Judge.",) or "RAJASTHAN HIGH COURT" in t:
                    continue
                if t.startswith("ROSTER"):
                    meta["roster"] = t.split(":", 1)[-1].strip(" •"); note_on = "r"; continue
                if t.startswith("NOTE"):
                    meta["notes"].append(t.split(":", 1)[-1].strip()); note_on = "n"; continue
                if re.match(r"^PART\s*-", t):
                    head = t; continue
                if note_on == "n":
                    if re.match(r"^\d+\.", t): meta["notes"].append(t)
                    else: meta["notes"][-1] += " " + t
                    continue
                if note_on == "r":
                    meta["roster"] += " " + t.strip(" •"); continue
                continue
            if not cols:
                continue
            first = r["w"][0]
            if first["x0"] < cols["case"] and re.fullmatch(r"\d{1,4}", first["text"]):
                cur = {"n": first["text"], "h": head, "case": [], "title": [], "pet": [], "resp": []}
                items.append(cur)
                ws = r["w"][1:]
            else:
                ws = r["w"]
                if all(w["x0"] >= cols["case"] for w in ws) and r["bold"] and not any(w["x0"] < cols["title"] - 2 for w in ws if False):
                    # a centred bold line between items is a section head
                    x0 = ws[0]["x0"]; x1 = ws[-1]["x1"]
                    if x0 > cols["case"] + 30 and not t.startswith("[") and not t.startswith("("):
                        head = t; cur = None if (x1 - x0) < 500 else cur
                        continue
                if re.match(r"^PART\s*-", t):
                    head = t; cur = None; continue
            if cur is None:
                continue
            if "BY ORDER OF" in t or t.startswith("Sd/-"):
                cur = None; done = True
                continue
            buck = {"case": [], "title": [], "pet": [], "resp": []}
            prev, prevk = None, None
            for w in ws:
                x = w["x0"]
                k = "case" if x < cols["title"] - 2 else "title" if x < cols["pet"] else "pet" if x < cols["resp"] else "resp"
                # a long [tag] or (department) line runs on across the advocate columns: keep it whole
                if k in ("pet", "resp") and prevk == "title" and prev is not None and w["x0"] - prev["x1"] < 12:
                    k = "title"
                if k in ("pet", "resp") and prevk == "title" and cur.get("_open"):
                    k = "title"
                buck[k].append(w["text"])
                prev, prevk = w, k
                if k == "title":
                    o = cur.get("_open", 0) + w["text"].count("[") - w["text"].count("]")
                    cur["_open"] = max(0, o)
            for k, v in buck.items():
                if v:
                    cur[k].append(" ".join(v))
    out = []
    for it in items:
        case_s = _join(it["case"])
        urn = ""
        mu = re.search(r"URN:\s*(.+)$", case_s)
        if mu:
            urn = re.sub(r"\s*With\b.*$", "", mu.group(1)).strip()
            rest = mu.group(1)[len(urn):]
            case_s = (case_s[:mu.start()] + " " + rest).strip()
        cases = [re.sub(r"\s+", " ", x).strip() for x in CASE_RE.findall(case_s)] or [case_s]
        title, dept, tags = [], "", []
        buf = ""
        kind = ""
        for ln in it["title"]:
            if buf or ln.startswith("[") or (ln.startswith("(") and not dept):
                if not buf: kind = "t" if ln.startswith("[") else "d"
                buf = (buf + " " + ln).strip()
                o, c = ("[", "]") if kind == "t" else ("(", ")")
                if buf.count(o) <= buf.count(c):
                    if kind == "t": tags += [x.strip(" []") for x in re.split(r"\]\s*\[", buf) if x.strip(" []")]
                    else: dept = buf.strip()[1:-1].strip() if buf.endswith(")") else buf.strip("() ")
                    buf = ""
                continue
            title.append(ln)
        if buf:
            if kind == "t": tags.append(buf.strip("[] "))
            else: dept = buf.strip("() ")
        ttl = _join(title)
        mm = re.match(r"^(.*?)\s*VS\.?\s+(.*)$", ttl)
        out.append({
            "n": it["n"], "h": it["h"], "c": cases[0], "cs": cases[1:], "urn": urn,
            "p": (mm.group(1) if mm else ttl).strip(), "r": (mm.group(2) if mm else "").strip(),
            "d": dept, "pa": [a for a in it["pet"] if a], "ra": [a for a in it["resp"] if a], "x": tags,
        })
    return {"meta": meta, "items": out, "pages": len(pdf.pages)}


if __name__ == "__main__":
    import json, sys
    d = parse(sys.argv[1])
    print(json.dumps(d["meta"], indent=1, ensure_ascii=False))
    print(len(d["items"]), "items", d["pages"], "pages")
    for it in d["items"][:12] + d["items"][-4:]:
        print(json.dumps(it, ensure_ascii=False))
    print(len(json.dumps(d, ensure_ascii=False, separators=(",", ":"))), "bytes")
