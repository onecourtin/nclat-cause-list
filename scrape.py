#!/usr/bin/env python3
"""Fetch NCLAT daily cause lists and turn them into JSON.

NCLAT lists every day's PDFs on one paginated page,

    https://nclat.nic.in/daily-cause-list?page=N

as a table of (court, description, date, PDF link). File names are NOT
predictable (``Causelist_II_14.10.2026.pdf``, ``Supp_Causelist_IV_13.10.2026.pdf``,
``14.10.2026.pdf`` for Chennai, ``Registrar Court_13.10.2026.pdf``, ``… Suppl
(II).pdf`` …), so the scraper reads the listing page rather than guessing URLs.

Courts: Chairperson, Court-II, Court-IV (Principal Bench, New Delhi), the
Chennai Bench, and the Registrar Court. Each may have a main list and one or
more supplementary lists per date.

Output, all under data/:

    index.json          which dates exist, and per file its ETag/hash
    <YYYY-MM-DD>.json   the parsed lists for that date

The PDFs are tables (S.No | Case No | Parties | Counsel for Appellants |
Counsel for Respondents; the Registrar list has no respondents column). The
parser reads text by x-position with PyMuPDF, taking the column edges from the
header row of each PDF, because plain text extraction interleaves the columns.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import fitz  # PyMuPDF
import requests

SITE = "https://nclat.nic.in"
LISTING = SITE + "/daily-cause-list"
DATA = Path(__file__).resolve().parent / "data"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OneCourt cause-list reader; +https://onecourt.in)",
}

# listing "Court Name" cell -> (key, display name, sort order)
COURTS = {
    "chairperson court": ("ch", "Court of Chairperson", 0),
    "court - 2": ("II", "Court-II", 1),
    "court - 4": ("IV", "Court-IV", 2),
    "chennai bench": ("chennai", "Chennai Bench", 3),
    "registrar court": ("reg", "Registrar Court", 4),
}

# ── PDF parsing ──────────────────────────────────────────────────────────────

HEADER_ROW = re.compile(
    r"^(S\.?\s*No\.?|Case No\.?|Name of the [Pp]arties|Counsel for|Appellants|Respondents|"
    r"S\.?No\.? Case No\.?|Counsel for Appellants)$"
)
TIME_RE = re.compile(r"^\(?\s*(\d{1,2}[:.]\d{2}\s*[AP]\.?\s?M\.?)\s*\)?$", re.I)
SNO_RE = re.compile(r"^\d+(\.\d+)*\.?$")
VS_RE = re.compile(r"^(vs\.?|versus|v/s\.?)$", re.I)
FOOT_RE = re.compile(r"^(Copy to|By order|INSTRUCTIONS FOR)", re.I)
SNO_CASE_RE = re.compile(r"^(\d+(?:\.\d+)*\.)\s+(\S.*)$")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _lines(page):
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            text = "".join(s["text"] for s in ln["spans"])
            if not text.strip():
                continue
            span = ln["spans"][0]
            yield {
                "x0": ln["bbox"][0],
                "x1": ln["bbox"][2],
                "y0": ln["bbox"][1],
                "text": text.strip(),
                "bold": bool(span["flags"] & 16),
                "size": span["size"],
            }


def is_sno(text: str) -> bool:
    """'12.' or '5.1' — an item number. Bare digits ('2026') are a wrapped case number."""
    return bool(SNO_RE.match(text)) and "." in text


def _columns(doc):
    """Column left edges, found from the body itself: item numbers give the
    first, and the other columns are the sharp peaks of line start positions
    (every column is left-aligned). Header cells are centred in some courts'
    PDFs, so their positions can't be trusted."""
    from collections import Counter

    sno_x, starts = [], Counter()
    in_table = False
    for page in doc:
        for ln in sorted(_lines(page), key=lambda l: (round(l["y0"], 1), l["x0"])):
            if ln["y0"] > page.rect.height - 40:
                continue
            if FOOT_RE.match(ln["text"]):
                return _edges(sno_x, starts)
            if re.match(r"^S\.?\s*No", ln["text"]):
                in_table = True
                continue
            if not in_table or HEADER_ROW.match(ln["text"]):
                continue
            m = SNO_CASE_RE.match(ln["text"])
            if is_sno(ln["text"]) and ln["x0"] < 90:
                sno_x.append(ln["x0"])
            elif m and ln["x0"] < 90:
                sno_x.append(ln["x0"])  # "1.   Comp. App. (AT)": number and case on one line
            else:
                starts[round(ln["x0"] / 3)] += 1
    return _edges(sno_x, starts)


def _edges(sno_x, starts):
    if not sno_x:
        return None
    sno = sorted(sno_x)[len(sno_x) // 2]
    total = sum(starts.values())
    peaks = []
    for b, n in sorted(starts.items()):
        x = b * 3
        if x < sno + 15 or n < (1 if total < 60 else max(2, total * 0.03)):
            continue
        if peaks and x - peaks[-1][0] < 45:
            if n > peaks[-1][1]:
                peaks[-1] = (x, n)
            continue
        peaks.append((x, n))
    xs = [x for x, _ in peaks]
    if len(xs) < 3:
        return None
    return {"sno": sno, "case": xs[0], "party": xs[1], "app": xs[2], "res": xs[3] if len(xs) > 3 else None}


def _assign(x0, cols):
    """Which column a line starting at x0 belongs to."""
    tol = 9
    if x0 < cols["case"] - tol:
        return "sno" if x0 >= cols["sno"] - tol else "left"
    if x0 < cols["party"] - tol:
        return "case"
    if x0 < cols["app"] - tol:
        return "party"
    if cols["res"] is None or x0 < cols["res"] - tol:
        return "app"
    return "res"


class Item:
    def __init__(self, sno):
        self.sno = sno.rstrip(".")
        self.case = []
        self.notes = []
        self.note_mode = False
        self.pet, self.res = [], []
        self.after_vs = False
        self.app, self.rsp = [], []  # lists of [text, x1]

    def add_case(self, ln):
        t = ln["text"]
        if not self.note_mode and self.case and (ln["bold"] or re.match(r"^(For\b|To\b|With\b|\(\s*(with|part|old|defects))", t, re.I)):
            self.note_mode = True
        (self.notes if self.note_mode else self.case).append(_clean(t))

    def add_party(self, text):
        if VS_RE.match(text.strip()):
            self.after_vs = True
        elif self.after_vs:
            self.res.append(text)
        else:
            self.pet.append(text)

    @staticmethod
    def _counsel(lines, right_edge):
        """Join counsel lines. Each advocate is normally on a line of their own;
        a line is only a continuation when the previous one is visibly cut
        short (ends with '-' or ','), the new one starts lower-case or with a
        respondent tag ('R1', 'for …'), or the previous one is a lone 'With'."""
        out = []
        for text, x1, size in lines:
            text = _clean(text)
            if not text:
                continue
            if out:
                prev, _ = out[-1]
                if (prev.endswith(("-", ",")) or prev.lower() == "with" or text[0].islower()
                        or re.match(r"^(R\d+|for\b)", text, re.I)
                        or (re.fullmatch(r"\S+ R\d+", text) and not re.search(r"R\d+$", prev))
                        or (re.fullmatch(r"[A-Z][a-z]+", text) and re.fullmatch(r"(Mr|Ms|Mrs|Dr|Shri|Smt)\.? \S+", prev))):
                    sep = "" if prev.endswith("-") else " "
                    out[-1] = (prev + sep + text, x1)
                    continue
            out.append((text, x1))
        return [t for t, _ in out]

    def out(self, right_app, right_res):
        d = {"n": self.sno, "c": _clean(" ".join(self.case)), "p": _clean(" ".join(self.pet)), "r": _clean(" ".join(self.res))}
        pa = self._counsel(self.app, right_app)
        ra = self._counsel(self.rsp, right_res)
        if pa:
            d["pa"] = "; ".join(pa)
        if ra:
            d["ra"] = "; ".join(ra)
        if self.notes:
            d["x"] = self.notes
        return d


def parse_pdf(path_or_bytes) -> dict:
    """Parse one cause-list PDF -> {time, judges, vc, notes, sections, published?}."""
    doc = fitz.open(stream=path_or_bytes, filetype="pdf") if isinstance(path_or_bytes, bytes) else fitz.open(path_or_bytes)
    cols = _columns(doc)
    bench = {"time": "", "judges": "", "vc": "", "notes": [], "sections": []}
    if cols is None:
        return bench
    page_w = doc[0].rect.width
    right_app = (cols["res"] - 4) if cols["res"] else page_w - 20
    right_res = page_w - 20
    right_app_f = right_app

    phase = "head"
    judges_open = False
    note_open = False
    section = None
    item = None

    def close_item():
        nonlocal item
        if item is not None:
            if section is None:
                new_section("")
            section["items"].append(item.out(right_app_f, right_res))
        item = None

    def new_section(title):
        nonlocal section
        section = {"t": _clean(title), "items": []}
        bench["sections"].append(section)

    for page in doc:
        # Reading order is top-to-bottom. An item's number sits a fraction of a
        # point lower than its first case-number line, so nudge number lines up
        # to make sure they open the item before its other columns arrive.
        lines = sorted(_lines(page), key=lambda l: (round(l["y0"] - (2 if (is_sno(l["text"]) or SNO_CASE_RE.match(l["text"])) and l["x0"] < cols["case"] - 8 else 0), 1), l["x0"]))
        for ln in lines:
            text = ln["text"]
            if ln["y0"] > page.rect.height - 40 and re.fullmatch(r"\d{1,3}", text):
                continue  # page number
            if HEADER_ROW.match(text):
                if phase == "head" and re.match(r"^S\.?\s*No", text):
                    phase = "body"
                    judges_open = note_open = False
                continue
            if FOOT_RE.match(text):
                close_item()
                phase = "foot"
            if phase == "foot":
                continue

            if phase == "head":
                m = TIME_RE.match(text)
                if re.match(r"^In the Court of", text, re.I):
                    bench["judges"] = _clean(text)
                    judges_open, note_open = True, False
                elif m and not bench["time"]:
                    bench["time"] = _clean(m.group(1))
                    judges_open = False
                elif text.startswith("http"):
                    bench["vc"] = text.split()[0]
                elif re.match(r"^For\b", text) and (not note_open or not bench["notes"] or bench["notes"][-1].rstrip().endswith(".")):
                    judges_open = False
                    new_section(text)
                elif re.match(r"^Note\b", text, re.I):
                    judges_open, note_open = False, True
                    rest = _clean(re.sub(r"^Note\s*:?-?", "", text, flags=re.I))
                    if rest:
                        bench["notes"].append(rest)
                elif judges_open:
                    bench["judges"] += " " + _clean(text)
                elif note_open:
                    if re.match(r"^\d+\.\s", text) or not bench["notes"]:
                        bench["notes"].append(_clean(text))
                    else:
                        bench["notes"][-1] += " " + _clean(text)
                # anything else: letterhead lines (tribunal name, address, court title)
                continue

            # ── body ──
            col = _assign(ln["x0"], cols)
            if col == "left":
                # Left of the table: a section heading ("For Admission (Fresh Cases)")
                close_item()
                if section is not None and not section["items"]:
                    section["t"] = _clean(section["t"] + " " + text)
                else:
                    new_section(text)
                continue
            if col == "sno":
                m = SNO_CASE_RE.match(text)
                if is_sno(text):
                    close_item()
                    item = Item(text)
                elif m:
                    close_item()
                    item = Item(m.group(1))
                    item.add_case({**ln, "text": m.group(2)})
                else:
                    close_item()
                    if section is not None and not section["items"]:
                        section["t"] = _clean(section["t"] + " " + text)
                    else:
                        new_section(text)
                continue
            if item is None:
                continue
            if col == "case":
                item.add_case(ln)
            elif col == "party":
                item.add_party(text)
            elif col == "app":
                item.app.append((text, ln["x1"], ln["size"]))
            else:
                item.rsp.append((text, ln["x1"], ln["size"]))

    close_item()
    bench["sections"] = [s for s in bench["sections"] if s["items"]]
    bench["judges"] = bench["judges"]
    if not bench["notes"]:
        del bench["notes"]
    if not bench["vc"]:
        del bench["vc"]
    return bench


# ── Listing page ─────────────────────────────────────────────────────────────

def parse_listing(html: str) -> list[dict]:
    """Rows of the daily-cause-list table -> [{court, desc, date, href}]."""
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        a = re.search(r'href="([^"]+\.pdf)"', row, re.I)
        if not a:
            continue
        cells = [_clean(re.sub(r"<[^>]+>", " ", c)) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 4:
            continue
        court, desc, d = cells[1], cells[2], cells[3]
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", d)
        if not m:
            continue
        out.append({
            "court": court,
            "desc": desc,
            "date": f"{m.group(3)}-{m.group(2)}-{m.group(1)}",
            "href": urljoin(SITE, a.group(1)),
        })
    return out


def classify(row: dict) -> dict:
    """Court key / kind / sequence from a listing row."""
    court = row["court"].strip().lower()
    key, name, order = COURTS.get(court, (re.sub(r"[^a-z0-9]+", "-", court).strip("-"), row["court"].strip(), 9))
    desc = row["desc"]
    supp = re.match(r"Supplementary", desc, re.I) is not None
    seq = 0
    if supp:
        seq = 1
        m = re.search(r"Supplementary Cause List\s*\(\s*([0-9IVX]+)\s*\)", desc, re.I)
        if m:
            v = m.group(1).upper()
            roman = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}
            seq = int(v) if v.isdigit() else roman.get(v, 1)
    return {"ckey": key, "court": name, "order": order, "kind": "supp" if supp else "main", "seq": seq}


# ── Fetching ─────────────────────────────────────────────────────────────────

STATS: dict[str, int] = {}


def fetch(session, url, prev=None, pdf=True):
    status, body, meta = _fetch(session, url, prev, pdf)
    STATS[status] = STATS.get(status, 0) + 1
    return status, body, meta


def _fetch(session, url, prev, pdf):
    headers = {}
    if prev and prev.get("etag"):
        headers["If-None-Match"] = prev["etag"]
    for attempt in range(3):
        try:
            r = session.get(url, headers=headers, timeout=60)
            break
        except requests.RequestException as e:
            if attempt == 2:
                print(f"  ! {url}: {e}", file=sys.stderr)
                return "error", None, None
            time.sleep(3 * (attempt + 1))
    if r.status_code == 304:
        return "same", None, prev
    if r.status_code == 404 or (r.status_code == 200 and not r.content):
        return "missing", None, None
    if r.status_code != 200 or (pdf and not r.content.startswith(b"%PDF")):
        print(f"  ! {url}: HTTP {r.status_code}, {len(r.content)} bytes, {r.headers.get('Content-Type', '')}", file=sys.stderr)
        return "error", None, None
    meta = {
        "etag": r.headers.get("ETag", ""),
        "modified": r.headers.get("Last-Modified", ""),
        "sha": hashlib.sha256(r.content).hexdigest()[:16],
        "bytes": len(r.content),
    }
    if prev and prev.get("sha") == meta["sha"]:
        return "same", None, meta
    return "new", r.content, meta


def read_listing(session, oldest: str, max_pages=12):
    """Walk the listing pages until they reach dates older than `oldest`."""
    rows = []
    for page in range(max_pages):
        status, body, _ = fetch(session, f"{LISTING}?page={page}", None, pdf=False)
        if status != "new":
            break
        got = parse_listing(body.decode("utf-8", "replace"))
        if not got:
            break
        rows.extend(got)
        if min(r["date"] for r in got) < oldest:
            break
    return rows


def label_for(info: dict) -> str:
    if info["kind"] == "main":
        return "Cause List"
    return "Supplementary" + ("" if info["seq"] <= 1 else f" ({info['seq']})")


def list_name(info: dict) -> str:
    return f"{info['ckey']}-{info['kind']}" + (f"-{info['seq']}" if info["seq"] > 1 else "")


def build_date(session, date, rows, index, force) -> bool:
    entry = index["dates"].get(date, {"files": {}})
    files = entry["files"]
    changed = False
    fresh = {}
    present = set()

    for row in rows:
        info = classify(row)
        name = list_name(info)
        # Two files can map to one name (re-uploads); keep the later row.
        if name in present:
            continue
        present.add(name)
        prev = None if force else files.get(name)
        if prev and prev.get("url") != row["href"]:
            prev = None  # file replaced by a differently named upload
        status, body, meta = fetch(session, row["href"], prev)
        if status in ("missing", "error"):
            continue
        meta = {**(meta or {}), "url": row["href"], "info": info, "desc": row["desc"]}
        if status == "new":
            fresh[name] = body
            files[name] = meta
            changed = True
            print(f"  + {date} {name} ({meta['bytes'] // 1024} KB)")
        else:
            files[name] = {**files.get(name, {}), **meta}

    # drop lists that NCLAT withdrew from the listing
    for name in list(files):
        if name not in present:
            del files[name]
            changed = True

    if not files:
        return False
    if not changed and (DATA / f"{date}.json").exists():
        return False

    lists, benches = [], []
    for name, meta in sorted(files.items(), key=lambda kv: (kv[1]["info"]["order"], kv[1]["info"]["seq"] if kv[1]["info"]["kind"] == "supp" else 99)):
        body = fresh.get(name)
        if body is None:
            status, body, m2 = fetch(session, meta["url"], None)
            if status != "new":
                continue
        info = meta["info"]
        try:
            b = parse_pdf(body)
        except Exception as e:
            print(f"  ! parse failed for {date} {name}: {e}", file=sys.stderr)
            b = {"time": "", "judges": "", "sections": []}
        b = {"list": name, "ckey": info["ckey"], "court": info["court"], **b}
        meta["published"] = meta.get("modified")
        lists.append({
            "name": name,
            "ckey": info["ckey"],
            "court": info["court"],
            "order": info["order"],
            "kind": info["kind"],
            "seq": info["seq"],
            "label": label_for(info),
            "url": meta["url"],
            "published": meta.get("modified"),
            "items": sum(len(s["items"]) for s in b["sections"]),
        })
        benches.append(b)

    out = {"date": date, "fetched": dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"), "lists": lists, "benches": benches}
    (DATA / f"{date}.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    entry["files"] = files
    entry["items"] = sum(l["items"] for l in lists)
    entry["fetched"] = out["fetched"]
    index["dates"][date] = entry
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days-back", type=int, default=2)
    ap.add_argument("--days-ahead", type=int, default=7)
    ap.add_argument("--keep-days", type=int, default=60, help="delete parsed dates older than this")
    ap.add_argument("--date", action="append", help="scrape only this date (YYYY-MM-DD); repeatable")
    ap.add_argument("--force", action="store_true", help="re-download and re-parse even if unchanged")
    ap.add_argument("--parse", metavar="PDF", help="just parse a local PDF and print JSON")
    args = ap.parse_args()

    if args.parse:
        print(json.dumps(parse_pdf(args.parse), ensure_ascii=False, indent=1))
        return

    DATA.mkdir(exist_ok=True)
    idx_path = DATA / "index.json"
    index = json.loads(idx_path.read_text()) if idx_path.exists() else {"dates": {}}

    today = dt.datetime.now(IST).date()
    if args.date:
        dates = sorted(args.date)
    else:
        dates = [(today + dt.timedelta(days=d)).isoformat() for d in range(-args.days_back, args.days_ahead + 1)]

    session = requests.Session()
    session.headers.update(HEADERS)
    rows = read_listing(session, min(dates))
    print(f"Listing: {len(rows)} rows")
    by_date: dict[str, list] = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)

    changed = False
    for date in dates:
        if date not in by_date:
            continue
        print(date)
        changed |= build_date(session, date, by_date[date], index, args.force)

    print("Requests: " + ", ".join(f"{k} {v}" for k, v in sorted(STATS.items())))
    if STATS.get("error") and not (STATS.get("new") or STATS.get("same")):
        print("::error::Every request to NCLAT failed — the site may be blocking this runner.")
        sys.exit(1)
    if not rows:
        print("::error::The listing page returned no rows — NCLAT may have changed its layout or be blocking this runner.")
        sys.exit(1)

    cutoff = (today - dt.timedelta(days=args.keep_days)).isoformat()
    for date in list(index["dates"]):
        if date < cutoff:
            del index["dates"][date]
            (DATA / f"{date}.json").unlink(missing_ok=True)
            changed = True

    if not changed and idx_path.exists():
        print("No changes.")
        return
    index["dates"] = dict(sorted(index["dates"].items()))
    index["updated"] = dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    idx_path.write_text(json.dumps(index, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
