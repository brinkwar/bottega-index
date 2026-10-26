#!/usr/bin/env python3
"""
BOTTEGA_HARVEST_V29

harvest.py — pull course records out of the catalogs discover.py located.

Two adapters ship here, CourseLeaf and Acalog, because between them they cover
roughly two thirds of every catalog found. Coursedog and SmartCatalog come next;
their hooks are stubbed and registered so adding one is a single function.

WHAT THIS MEASURES: course code, title, description, credits. Nothing is scored
here. Scoring is score.py's job, and keeping the two apart means the rubric can
change without refetching a single page.

Reuses discover.py's Fetcher, so robots.txt, the one-second-per-domain budget,
the named user agent and the on-disk cache all carry over unchanged.

USAGE
  python3 harvest.py --limit 5 --refresh        # a few schools, watch the output
  python3 harvest.py --platforms courseleaf     # one adapter across the tier
  python3 harvest.py                            # everything supported

OUTPUTS
  data/courses/<unitid>.json   course records for one school
  data/harvest_run.log         audit trail and the yield table
"""

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

import fcntl

import discover as D

MARKER = "BOTTEGA_HARVEST_V29"

# A description shorter than this is a stub, not a description. The 70% coverage
# floor is meaningless if "see department" and "TBA" count as covered.
MIN_DESC_CHARS = 40

LOG = []


def log(msg):
    LOG.append(str(msg))
    print(msg, file=sys.stderr)


def hst_today():
    return datetime.now(timezone(timedelta(hours=-10))).strftime("%Y-%m-%d")


def subject_of(code, source_url=""):
    """Department prefix from the course code, e.g. 'ARTF 1010' -> 'ARTF'.

    Derivable from data already harvested, so adding it costs a recompute rather
    than a re-fetch of half a million records.
    """
    m = re.match(r"^([A-Za-z]{2,6})", (code or "").strip())
    return m.group(1).upper() if m else ""


def section_of(source_url):
    """The catalog path segment a course was read from, e.g. 'acce'."""
    try:
        path = urllib.parse.urlparse(source_url or "").path.strip("/")
    except Exception:
        return ""
    parts = [p for p in path.split("/") if p]
    return parts[-1] if parts else ""


# "Themes in American History Credits 3" and "Vertebrate Paleontology. Units: 4"
# both leave the credit text welded to the title and the credits field empty.
# The heading patterns expect "Title. 3 Credits." or "Title (3 credits)"; this
# is the label-first order, which they never matched.
TRAILING_CREDITS = re.compile(
    r"[\s.,;:\-\u2013]*\(?\s*(?:credit|unit|hour)s?\s*:?\s*"
    r"(\d+(?:\.\d+)?(?:\s*[-\u2013]\s*\d+(?:\.\d+)?)?)\s*\)?[\s.]*$", re.I)


def split_trailing_credits(title):
    """-> (title without the credit tail, credits or "").

    Runs in enrich(), so it repairs the whole corpus through --recompute
    rather than needing a re-fetch of 370,000 records.
    """
    t = (title or "").strip()
    m = TRAILING_CREDITS.search(t)
    if not m:
        return t, ""
    cleaned = t[:m.start()].strip(" .,;:-\u2013")
    if not cleaned:
        return t, ""
    return cleaned, re.sub(r"\s+", "", m.group(1))


def described(desc):
    return len((desc or "").strip()) >= MIN_DESC_CHARS


def write_json_atomic(path, obj):
    """Write beside the target, then rename over it. A killed or overlapping
    process can no longer leave a torn school file for score.py to read."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


_LOCK = None


def acquire_lock(path):
    """-> (True, "") or (False, holder). One harvest at a time, recompute
    included. flock releases when the process dies, so there is no stale lock.

    On 2026-09-10 two acalog --refresh runs went for two hours side by side, and
    a --recompute ran over the same files while they did."""
    global _LOCK
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        fh.seek(0)
        holder = fh.read().strip()
        fh.close()
        # HARVEST_OVERRIDE_V1: say whether the holder is alive. flock cannot be
        # held by a dead process, so a refusal means a live one; but the run
        # on 2026-09-16 left pid 58787 hung on a browser challenge for a day
        # and the message read as a stale file.
        m = re.match(r"pid (\d+)", holder or "")
        if m:
            pid = int(m.group(1))
            try:
                os.kill(pid, 0)
                holder += f"  [pid {pid} is ALIVE — kill it or wait: kill {pid}]"
            except ProcessLookupError:
                holder += f"  [pid {pid} is dead; another process holds the flock]"
            except PermissionError:
                holder += f"  [pid {pid} is alive under another user]"
        return False, holder
    fh.seek(0)
    fh.truncate()
    fh.write(f"pid {os.getpid()} since {hst_today()}: {' '.join(sys.argv)}")
    fh.flush()
    _LOCK = fh
    return True, ""


def enrich(courses):
    """Attach subject/section, strip credit tails, set tier by the real rule."""
    for c in courses:
        title, credits = split_trailing_credits(c.get("title", ""))
        c["title"] = title
        if credits and not (c.get("credits") or "").strip():
            c["credits"] = credits
        # V26: a subject set deliberately (Carnegie Mellon's numeric departments,
        # 60-101 is Art) survives every recompute. No harvested file carries the
        # lock, so nothing already harvested changes.
        if not c.get("subject_locked"):
            c["subject"] = subject_of(c.get("code", ""))
        c["section"] = section_of(c.get("source_url", ""))
        c["tier"] = 1 if described(c.get("description")) else 2
    return courses


# Must hold the same value as score.ARTICLE_STRIP. One rule, two files;
# test_harvest and test_score both assert the keys agree.
ARTICLE_STRIP = False

_ARTICLE = __import__("re").compile(r"^(the|a|an)\s+", __import__("re").I)


def dedupe_key(c):
    t = (c.get("title", "") or "").strip().lower()
    if ARTICLE_STRIP:
        t = _ARTICLE.sub("", t).strip()
    return (t, ((c.get("description", "") or "")[:200]).strip().lower())


def n_deduped(courses):
    """Distinct title+description records.

    CalArts lists "Independent Study" 41 times — AART 199/299/.../699, APHM
    199/.../699, AGRA 199/.../699 — one per department per level. Real codes,
    real entries, but 38% of that catalog is administrative shells replicated by
    registrar convention rather than curriculum. A school that lists Independent
    Study once is not comparable to one that lists it 41 times.
    """
    keys = {dedupe_key(c) for c in courses if (c.get("title") or "").strip()}
    blank = sum(1 for c in courses if not (c.get("title") or "").strip())
    return len(keys) + blank


def duplicate_rate(courses):
    """Share of records sharing a title+description with another record.

    Mostly per-department replication of shells, not cross-listing of one course
    under two prefixes. Both inflate the denominator; only the first is a
    registrar convention the index has no reason to reward or punish.
    """
    if not courses:
        return 0.0
    seen = Counter()
    for c in courses:
        key = (c.get("title", "").strip().lower(),
               (c.get("description", "") or "")[:200].strip().lower())
        if key[0]:
            seen[key] += 1
    dupes = sum(n - 1 for n in seen.values() if n > 1)
    return round(dupes / len(courses), 4)


def clean(s):
    """HTML fragment to readable text."""
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.I | re.S)
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"[\s\u00a0]+", " ", s).strip()


# ---------------------------------------------------------------- parsing

# "ARTG 1010.  Drawing I.  3 Credits."  /  "ART-101 Drawing I (3 credits)"
# /  "ARTH 200 - Survey of Art History"
CODE = r"([A-Z]{2,6}[\s\-\u2013:]?\d{2,5}[A-Z]?)"
CREDITS = r"(\d+(?:\.\d+)?(?:\s*[-\u2013]\s*\d+(?:\.\d+)?)?)"

TITLE_PATTERNS = [
    # The separator after a course code is wildly inconsistent across catalogs:
    # a period, a colon, an en dash, or nothing but a space.
    # The separator class must include the EM dash. Madison writes
    # "ART 102 — Title"; without \u2014 the match stopped at the space and the
    # dash became the first character of every title on that catalog.
    re.compile(rf"^{CODE}[\.\:\-\u2013\u2014\s]+(.+?)\s*[\.\:]\s*{CREDITS}\s*"
               r"(?:credit|unit|hour)s?\b", re.I),
    re.compile(rf"^{CODE}[\.\:\-\u2013\u2014\s]+(.+?)\s*\(\s*{CREDITS}\s*"
               r"(?:credit|unit|hour)s?\s*\)", re.I),
    re.compile(rf"^{CODE}[\.\:\-\u2013\u2014\s]+(.+?)\s*$"),
    # V24, after the catch-all so it can only fire where every pattern above
    # failed: comma-separated headings, "HON 101, Leadership, 6 Units" (Azusa)
    # and "ART 101, INTRODUCTION TO ART, 4 Credits" (Oregon State).
    re.compile(rf"^{CODE}\s*,\s*(.+?)\s*,\s*{CREDITS}\s*"
               r"(?:credit|unit|hour)s?\b", re.I),
    re.compile(rf"^{CODE}\s*,\s*(.+?)\s*$"),
    # V26, last: numeric department codes, "60-101 Foundations: Risk, Agency,
    # Failure" (Carnegie Mellon). Every heading shaped like this failed before.
    re.compile(r"^(\d{2}-\d{3}[A-Z]?)[\.\:\s\u2013\u2014-]+(.+?)\s*$"),
]


def parse_course_heading(text):
    """-> (code, title, credits|'') or None."""
    t = clean(text)
    if not t:
        return None
    for i, pat in enumerate(TITLE_PATTERNS):
        m = pat.match(t)
        if not m:
            continue
        # Normalize the separator so ART-101, ART 101 and ART101 are one code.
        code = re.sub(r"[\s\-\u2013:]+", " ", m.group(1)).strip()
        code = re.sub(r"^([A-Z]{2,6})(\d)", r"\1 \2", code)
        title = m.group(2).strip(" .:-\u2013\u2014")
        credits = m.group(3).strip() if m.lastindex and m.lastindex >= 3 else ""
        if not title or len(title) > 220:
            continue
        return code, title, credits
    return None


# ---------------------------------------------------------------- courseleaf

# Modern CourseLeaf (verified against catalog.scad.edu, 2026-2027) puts code,
# title and credits in separate detail-* spans and nests divs inside each
# courseblock. Older installs use a single courseblocktitle element. Both ship.
CL_SPLIT = re.compile(r'<div[^>]+class="[^"]*\bcourseblock\b[^"]*"[^>]*>', re.I)
CL_DETAIL_CODE = re.compile(
    r'class="[^"]*(?:\bdetail-code\b|\bcourseblockcode\b)[^"]*"[^>]*>(.*?)</span>',
    re.I | re.S)
CL_DETAIL_TITLE = re.compile(
    r'class="[^"]*(?:\bdetail-title\b|\bcourseblock__title\b)[^"]*"[^>]*>'
    r'(.*?)</span>', re.I | re.S)
CL_DETAIL_HOURS = re.compile(
    r'class="[^"]*(?:detail-hours|courseblock__hours)[^"]*"[^>]*>(.*?)</span>',
    re.I | re.S)
CL_OLD_TITLE = re.compile(
    r'<(?:p|div|h[1-6])[^>]+class="[^"]*(?:courseblocktitle|cb_title)[^"]*"[^>]*>'
    r'(.*?)</(?:p|div|h[1-6])>', re.I | re.S)
# Three generations in the wild: courseblockextra (SCAD), courseblockdesc /
# cb_desc (older), and courseblock__description (Cal Poly). The double-underscore
# variant must not also catch courseblock__details, which holds prerequisites.
CL_DESC = re.compile(
    r'<(?:p|div)[^>]+class="[^"]*(?:courseblockextra|courseblockdesc|cb_desc|'
    r'courseblock__description|courseblock_description)'
    r'[^"]*"[^>]*>(.*?)</(?:p|div)>', re.I | re.S)

# courseblockextra also carries prerequisites, attributes and scheduling notes.
# Those are metadata, not the description, and they poison the scoring text.
# courseblockextra carries every field a school chooses to publish, not just
# prerequisites. Madison adds Audience, Learning Outcomes, Last Taught and
# Requisites; 2,075 of its 2,116 descriptions were mostly field labels, and
# "Audience: Undergraduate" is 23 characters that clear no bar worth clearing.
# Two traps here. Trailing s? matters — "learning outcome" followed by "s" kills
# the \b, which is how "Prerequisites:" slipped past AC_META two rounds ago. And
# the colon is REQUIRED: without it, "Credit is given for independent research"
# reads as a field label and a real description gets discarded. Labels always
# carry a colon; the window allows "Repeatable for Credit: Yes".
CL_NOT_DESC = re.compile(
    r"^\s*(?:prerequisite|corequisite|pre-?req|co-?req|requisite|attribute|"
    r"cross-?listed|repeatable|grade mode|offered|restriction|equivalent|"
    r"typically offered|when offered|audience|learning outcome|last taught|"
    r"course designation|designation|level|breadth|general education|"
    r"l&s credit|liberal arts|instructor|department|component|schedule type|"
    r"credit|unit)s?\b[^:\n]{0,30}:", re.I)

CL_INDEX_PATHS = ["/coursesaz/", "/courses/", "/course-descriptions/",
                  "/coursedescriptions/", "/courselist/", "/coursesaz",
                  "/course-descriptions", "/courses", "/allcourses/",
                  "/undergraduate/courses/", "/graduate/courses/",
                  "/coursesatoz/", "/courses-a-z/",
                  # V24, appended so every school finds the index it found before:
                  # VCU and Iowa State, Colorado State and Marshall, UC Davis,
                  # Auburn. Each verified to exist in cldiag --evidence.
                  "/azcourses/", "/courses-az/", "/courses-subject-code/",
                  "/coursesofinstruction/"]
# The last path segment of a course index, and so the parent of every subject
# page. CU Boulder's /courses-a-z/ was FETCHED, returned 177 subject links, and
# was rejected because this list did not contain its segment.
# Every segment that can PARENT a subject page. V29 adds allcourses,
# coursesatoz and courselist, which were in CL_INDEX_PATHS but not here: UC
# Irvine's /allcourses/ac_eng/ was therefore accepted as the index, no subject
# links were recognised beneath it, and the school yielded 13 academic-writing
# courses out of a full catalog.
CL_INDEX_SEGMENTS = ["coursesaz", "courses", "course-descriptions",
                     "coursedescriptions", "university-course-descriptions",
                     "azcourses", "courses-a-z", "courses-az",
                     "courses-subject-code", "coursesofinstruction",
                     "allcourses", "coursesatoz", "courselist",
                     # HARVEST_OVERRIDE_V2: Baylor /undergraduate/courses-instruction/
                     "courses-instruction"]
# Penn State nests: /university-course-descriptions/ lists section indexes like
# /undergraduate/, and subjects sit one level below that. Allow the extra segment.
CL_SUBJECT_LINK = re.compile(
    r'href="([^"]*/(?:' + "|".join(re.escape(x) for x in CL_INDEX_SEGMENTS) +
    r')/[a-z0-9_\-]+(?:/[a-z0-9_\-]+)?/?)"', re.I)


def cl_parent_index(fetcher, res):
    """A subject page sitting under an index segment means the index is its
    PARENT. Accepting the child stops the crawl at one subject."""
    url = res.get("url") or ""
    parts = [x for x in urllib.parse.urlparse(url).path.split("/") if x]
    if len(parts) < 2 or parts[-2].lower() not in CL_INDEX_SEGMENTS:
        return None
    parent = urllib.parse.urljoin(url, "../")
    if not fetcher.allowed(parent):
        return None
    r = fetcher.get(parent)
    n_here = len(set(CL_SUBJECT_LINK.findall(res.get("body") or "")))
    n_up = len(set(CL_SUBJECT_LINK.findall(r.get("body") or "")))
    if r.get("status") == 200 and n_up > max(n_here, 2):
        log(f"    index was a subject page; climbed to {parent} "
            f"({n_up} subject links)")
        return r
    return None


def cl_is_index(res):
    """V24: stricter. The old test — any 200 page containing the string
    'courseblock' or ONE subject-shaped link — accepted Yale's instructors'
    handbook, Nebraska-Omaha's stylesheet (/css/courseleaf.css) and Iowa State's
    search results page as course indexes."""
    b = (res.get("body") or "")
    url = res.get("url") or ""
    path = urllib.parse.urlparse(url).path.lower()
    if res.get("status") != 200:
        return False
    if path.endswith((".css", ".js", ".pdf", ".xml", ".json")) or "/search" in path \
            or "?" in url:
        return False
    n_subj = len(set(CL_SUBJECT_LINK.findall(b)))
    n_blocks = len(CL_SPLIT.findall(b))
    last = path.rstrip("/").split("/")[-1] if path.strip("/") else ""
    return (n_subj >= 10 or n_blocks >= 3
            or (n_subj >= 2 and last in CL_INDEX_SEGMENTS))


def cl_root_indexes(fetcher, res):
    """Every course index the catalog root links to directly: links whose last
    path segment is an index segment. CU Denver's root links three —
    undergraduate, graduate, Anschutz — none reachable by a fixed path."""
    out, seen = [], set()
    for href in re.findall(r'href="([^"#?]+)"', res.get("body") or "", re.I):
        u = urllib.parse.urljoin(res.get("url") or "", html.unescape(href))
        last = urllib.parse.urlparse(u).path.rstrip("/").split("/")[-1].lower()
        if last not in CL_INDEX_SEGMENTS or u.rstrip("/") in seen:
            continue
        if urllib.parse.urlparse(u).netloc != urllib.parse.urlparse(
                res.get("url") or "").netloc or not fetcher.allowed(u):
            continue
        seen.add(u.rstrip("/"))
        r = fetcher.get(u)
        if cl_is_index(r):
            out.append(r)
    return out


def cl_find_index(fetcher, root, res):
    """Locate the A-to-Z course index."""
    base = root if root.endswith("/") else root + "/"
    for path in CL_INDEX_PATHS:
        url = urllib.parse.urljoin(base, path.lstrip("/"))
        if not fetcher.allowed(url):
            continue
        r = fetcher.get(url)
        if cl_is_index(r):
            return r
    # Catalogs that name the index something else still link to it from the root.
    for u in D.catalog_links(res, limit=12):
        if not fetcher.allowed(u):
            continue
        r = fetcher.get(u)
        if cl_is_index(r):
            return r
        # One hop further: the link often lands on a section page that in turn
        # lists the subjects.
        if r["status"] == 200 and r.get("body"):
            for u2 in D.catalog_links(r, limit=6):
                if not fetcher.allowed(u2):
                    continue
                r2 = fetcher.get(u2)
                if cl_is_index(r2):
                    return r2
    return None


def cl_bfs_find(fetcher, root, res, budget=25):
    """Last resort: walk one hop out from the catalog root looking for course
    blocks. Yale's root is an 8kb landing page that splits by school and never
    uses the word 'course', so nothing keyed on link text can reach its catalog.
    """
    ru = urllib.parse.urlparse(res.get("url") or root)
    tried = 0
    for href in re.findall(r'href="([^"]+)"', res.get("body") or "", re.I):
        if tried >= budget:
            break
        u = urllib.parse.urljoin(res["url"], html.unescape(href))
        pu = urllib.parse.urlparse(u)
        if pu.netloc != ru.netloc or "#" in u or "?" in u:
            continue
        if pu.path.rstrip("/") in ("", ru.path.rstrip("/")):
            continue
        if not fetcher.allowed(u):
            continue
        r = fetcher.get(u)
        tried += 1
        if cl_is_index(r):
            return r
    return None


# V24 heading variants, each taken verbatim from cldiag --evidence. They run
# only after the original strategies return nothing, so every block that parsed
# before V24 parses byte-for-byte the same, and no paid classification is
# invalidated.
CL_VARIANT_CODE = re.compile(          # UT detail-ut_code, NC State detail-
    r'class="[^"]*\bdetail-[a-z_]*code\b[^"]*"[^>]*>(.*?)</span>',  # coursecode,
    re.I | re.S)                                                     # ODU xrefcode
CL_TAG_TITLE = re.compile(             # Montclair <span>, UW-Milwaukee <button>
    r'<(?:span|button|a)[^>]+class="[^"]*\bcourseblocktitle\b[^"]*"[^>]*>'
    r'(.*?)</(?:span|button|a)>', re.I | re.S)
CL_DATA_CODE = re.compile(r'\bdata-code="([^"]+)"', re.I)            # Alabama
CL_SPLIT_CODE = re.compile(                                          # Nebraska,
    r'class="[^"]*\b(?:cb_subject_code|cb_subj)\b[^"]*"[^>]*>([^<]+)<.*?'  # Alabama
    r'class="[^"]*\b(?:cb_course_number|cb_numb)\b[^"]*"[^>]*>([^<]+)<',
    re.I | re.S)
CL_CB_TITLE = re.compile(
    r'class="[^"]*\bcb_title\b[^"]*"[^>]*>(.*?)</div>', re.I | re.S)
CL_STRONG = re.compile(r"<strong>(.*?)</strong>", re.I | re.S)       # Auburn


def _norm_code(code):
    code = re.sub(r"[\s\-\u2013:]+", " ", clean(code)).strip()
    return re.sub(r"^([A-Z]{2,6})(\d)", r"\1 \2", code)


def cl_block_heading(chunk):
    """-> (code, title, credits) from any CourseLeaf generation seen so far."""
    got = _cl_heading_original(chunk)
    if got:
        return got
    return _cl_heading_v24(chunk)


def _cl_heading_original(chunk):
    """Through V23, unchanged. Returns None where it used to."""
    code_m = CL_DETAIL_CODE.search(chunk)
    title_m = CL_DETAIL_TITLE.search(chunk)
    if code_m and title_m:
        code = clean(code_m.group(1))
        title = clean(title_m.group(1)).strip(" .:-\u2013")
        hours_m = CL_DETAIL_HOURS.search(chunk)
        credits = ""
        if hours_m:
            cm = re.search(r"\d+(?:\.\d+)?(?:\s*[-\u2013]\s*\d+(?:\.\d+)?)?",
                           clean(hours_m.group(1)))
            credits = cm.group(0) if cm else ""
        if code and title:
            code = re.sub(r"[\s\-\u2013]+", " ", code).strip()
            code = re.sub(r"^([A-Z]{2,5})(\d)", r"\1 \2", code)
            return code, title[:220], credits
    old = CL_OLD_TITLE.search(chunk)
    if old:
        return parse_course_heading(old.group(1))
    return None


def _cl_hours(chunk):
    m = CL_DETAIL_HOURS.search(chunk) or re.search(
        r'class="[^"]*\bcb_hours_numb\b[^"]*"[^>]*>([^<]+)<', chunk, re.I)
    if not m:
        return ""
    cm = re.search(r"\d+(?:\.\d+)?(?:\s*[-\u2013]\s*\d+(?:\.\d+)?)?", clean(m.group(1)))
    return cm.group(0) if cm else ""


def _cl_heading_v24(chunk):
    # a. a separate code element plus a title element
    code = ""
    m = CL_VARIANT_CODE.search(chunk)
    if m:
        code = _norm_code(m.group(1))
    if not code:
        m = CL_DATA_CODE.search(chunk[:400])
        if m:
            code = _norm_code(m.group(1))
    if not code:
        m = CL_SPLIT_CODE.search(chunk)
        if m:
            code = _norm_code(f"{m.group(1)} {m.group(2)}")
    title = ""
    m = CL_DETAIL_TITLE.search(chunk)
    if m:
        title = clean(m.group(1))
    if not title:
        m = CL_CB_TITLE.search(chunk)
        if m:
            title = clean(m.group(1))
    if code and re.match(r"^[A-Z]{2,6} \d", code) and title and title != code:
        return code, title.strip(" .:-\u2013")[:220], _cl_hours(chunk)
    # b. the whole heading sits in one element: a <span>/<button> courseblocktitle,
    #    or Marshall's detail-code span, which carries code AND title
    m = CL_TAG_TITLE.search(chunk)
    if m:
        got = parse_course_heading(m.group(1))
        if got:
            return got
    m = CL_VARIANT_CODE.search(chunk)
    if m:
        # Cut at the START of the tag that carries detail-hours, not at the word:
        # the word sits inside that tag's class attribute, and cutting there
        # leaks '<span class="credits text' into the title (Marshall).
        dh = chunk.find("detail-hours", m.end())
        if dh > 0:
            dh = chunk.rfind("<", m.end(), dh)
        row_end = [x for x in (dh, chunk.find("</div>", m.end())) if x > 0]
        seg = chunk[m.start():min(row_end) if row_end else m.end() + 600]
        got = parse_course_heading(re.sub(r"^[^>]*>", "", seg))
        if got:
            return got
    # c. no heading class at all: the block's first <strong> (Auburn)
    m = CL_STRONG.search(chunk[:600])
    if m:
        got = parse_course_heading(m.group(1))
        if got:
            code, title, cr = got
            pm = re.match(r"^(.*?)\s*\(\s*(\d+(?:[-\u2013]\d+)?)\s*\)\s*$", title)
            if pm and not cr:
                title, cr = pm.group(1), pm.group(2)
            return code, title, cr
    return None


# HARVEST_V7: "Prerequisite(s): X. Description…" — strip the clause, keep the rest.
REQ_LEAD = re.compile(
    r"^\s*(?:prerequisite|corequisite|co-?requisite|pre-?requisite|pre-?req|co-?req|"
    r"requisite|recommended preparation|registration restriction)s?\b[^.:]{0,4}[:,]?\s*"
    r"(?:[^.]|\.(?=\s*[a-z(]))*?\.\s+(?=[A-Z])", re.I)


def strip_leading_requisite(t):
    """Return the text after a leading requisite sentence, or '' if there is
    no such sentence or the remainder is too short to be a description."""
    m = REQ_LEAD.match(t or "")
    if not m:
        return ""
    rest = t[m.end():].strip()
    return rest if len(rest) >= MIN_DESC_CHARS else ""


# HARVEST_V5: two more CourseLeaf templates, tried only when CL_DESC yields
# nothing for the block, so existing descriptions never change.
CL_DESC_SPAN = re.compile(
    r'<span[^>]+class="[^"]*\bdetail-description\b[^"]*"[^>]*>(.*?)</span>', re.I | re.S)
CL_DESC_DIV = re.compile(
    r'<div[^>]+class="[^"]*\bcourseblockdesc\b[^"]*"[^>]*>(.*?)</div>', re.I | re.S)
CL_DIV_STRIP = re.compile(
    r'<p[^>]+class="[^"]*\bcredits\b[^"]*"[^>]*>.*?</p>|<em>.*?</em>|\(<span class="label">Typically Offered:.*?\)',
    re.I | re.S)


CL_NESTED_EXTRA = re.compile(
    r'(<p[^>]+class="[^"]*courseblockdesc[^"]*"[^>]*>)\s*<p[^>]+class="[^"]*courseblockextra[^"]*"[^>]*>.*?</p>',
    re.I | re.S)   # HARVEST_V7: SFSU nests the prereq paragraph inside the description paragraph


def cl_block_description(chunk):
    chunk = CL_NESTED_EXTRA.sub(r"\1", chunk)
    parts = []
    for raw in CL_DESC.findall(chunk):
        t = clean(raw)
        if not t:
            continue
        if CL_NOT_DESC.match(t):
            t2 = strip_leading_requisite(t)   # HARVEST_V7
            if t2 and not CL_NOT_DESC.match(t2):
                parts.append(t2)
            continue
        parts.append(t)
    desc = " ".join(parts).strip()[:6000]
    if desc:
        return desc
    for raw in CL_DESC_SPAN.findall(chunk):
        t = clean(raw)
        if t and not CL_NOT_DESC.match(t):
            parts.append(t)
    if parts:
        return " ".join(parts).strip()[:6000]
    for raw in CL_DESC_DIV.findall(chunk):
        t = clean(CL_DIV_STRIP.sub(" ", raw))
        t = re.sub(r"^\s*(?:Prereq(?:uisite)?s?:[^.]*\.)\s*", "", t)
        if t and not CL_NOT_DESC.match(t) and len(t) >= MIN_DESC_CHARS:
            parts.append(t)
    return " ".join(parts).strip()[:6000]


def parse_courseleaf_page(body, source_url):
    """Extract course records from one CourseLeaf subject page."""
    out = []
    chunks = CL_SPLIT.split(body or "")[1:]
    for chunk in chunks:
        parsed = _cl_heading_original(chunk)
        was_parsed_before = parsed is not None
        if not parsed:
            parsed = _cl_heading_v24(chunk)
        if not parsed:
            continue
        code, title, credits = parsed
        desc = cl_block_description(chunk)
        # V25: V24 let this fire on ANY block without a description class, so
        # every undescribed course at 10 working schools gained a description —
        # new text, a new cache key, re-billing (Virginia Tech: 1,723 courses).
        # Only blocks nothing parsed before may get it.
        if not was_parsed_before and not desc and not CL_DESC.search(chunk):
            # V24, Auburn: <p><strong>HEADING</strong>LEC. 3. Description...</p>.
            # Only where no description class exists at all, so no block that had
            # a description before gets a different one now.
            sm = CL_STRONG.search(chunk[:600])
            rest = clean(chunk[sm.end():]) if sm else ""
            rest = re.sub(r"^(?:(?:LEC|LAB|SEM|STU|PRA|CLI|CLN|DSL|IND)\.?\s*"
                          r"[\d\.\-]+\.?\s*)+", "", rest)
            desc = rest.strip()[:6000]
        out.append({
            "code": code, "title": title, "credits": credits,
            "description": desc, "source_url": source_url,
            "tier": 1 if desc else 2,
        })
    return out


def harvest_courseleaf(fetcher, inst, cap):
    root = inst["catalog_url"]
    res = fetcher.get(root)
    # HARVEST_OVERRIDE_V1: a verified override URL is usually the index itself.
    idx = res if (inst.get("_override") and cl_is_index(res)) else None
    if idx is None:
        idx = cl_find_index(fetcher, root, res)
    if idx is not None:
        idx = cl_parent_index(fetcher, idx) or idx
    extra = []
    if idx is None:
        found = cl_root_indexes(fetcher, res)
        if found:
            idx, extra = found[0], found[1:]
    if idx is None:
        idx = cl_bfs_find(fetcher, root, res)
    if idx is None:
        return [], 1, "no course index found"
    inst["_index_url"] = idx["url"] + (f" +{len(extra)} more" if extra else "")

    # V8's collection logic, which harvested 49 schools and 192,734 courses,
    # plus one narrow addition: descend only when a page yields NO courses.
    #
    # The V9/V11 prefix walk replaced this and cost 8 schools and 120,000
    # records. For a flat catalog every subject page yields courses, so nothing
    # extra is fetched and this is byte-identical to V8. Only a section index
    # like Penn State's, which yields nothing, gets its own links followed.
    # HARVEST_OVERRIDE_V2: under an override the index path is known, so any
    # link one segment below it is a subject page whatever the segment is named.
    _ovr_prefix = ""
    if inst.get("_override"):
        _op = urllib.parse.urlparse(root).path.rstrip("/") + "/"
        _ovr_prefix = _op
    _OVR_LINK = re.compile(r'href="([^"#?]+)"', re.I)

    def subject_links(body, base):
        out = []
        for href in CL_SUBJECT_LINK.findall(body or ""):
            u = urllib.parse.urljoin(base, html.unescape(href))
            out.append(u)
        if _ovr_prefix:
            for href in _OVR_LINK.findall(body or ""):
                u = urllib.parse.urljoin(base, html.unescape(href))
                pth = urllib.parse.urlparse(u).path
                if (pth.startswith(_ovr_prefix)
                        and pth[len(_ovr_prefix):].strip("/")
                        and "/" not in pth[len(_ovr_prefix):].strip("/")
                        and u not in out):
                    out.append(u)
        return out

    courses, seen_codes = [], set()
    pages = 2
    visited = {idx["url"].rstrip("/")} | {e["url"].rstrip("/") for e in extra}
    queue = [(u, 1) for u in subject_links(idx["body"], idx["url"])]
    for e in extra:
        queue += [(u, 1) for u in subject_links(e["body"], e["url"])]

    while queue and pages <= cap:
        u, depth = queue.pop(0)
        if u.rstrip("/") in visited or not fetcher.allowed(u):
            continue
        visited.add(u.rstrip("/"))
        r = fetcher.get(u)
        pages += 1
        if r["status"] != 200 or not r.get("body"):
            continue

        got = parse_courseleaf_page(r["body"], r["url"])
        if got:
            for c in got:
                key = (c["code"], c["title"][:40])
                if key not in seen_codes:
                    seen_codes.add(key)
                    courses.append(c)
        elif depth < 3:
            # A section index, not a subject page. Follow its own subject links.
            # V24: depth 3, for Drexel's /coursedescriptions/quarter/undergrad/.
            for u2 in subject_links(r["body"], r["url"]):
                if u2.rstrip("/") not in visited:
                    queue.append((u2, depth + 1))

    # Some catalogs render every course on the index itself.
    if not courses:
        courses = parse_courseleaf_page(idx["body"], idx["url"])

    if pages > cap:
        return courses, pages, (f"PAGE CAP HIT at {cap} — this catalog is "
                                f"truncated, raise --cap and re-harvest")
    return courses, pages, "" if courses else "index found but no course blocks"


# ---------------------------------------------------------------- acalog

# Acalog's expanded listing (verified against catalogue.usc.edu, catoid 22)
# renders each course as:
#   <h3>AAN 515 Regression Modeling</h3> Units: 2<br>Terms Offered: FaSpSm
#   <hr>Description text. <br><em>Prerequisite:</em> ... <br>Grading Option: ...
#
# The preview_course links in that markup are PREREQUISITE cross-references, not
# course entries — their anchor text is a bare code with no title. Keying on them
# was the V4 bug.
AC_H3 = re.compile(r"<h3[^>]*>(.*?)</h3>", re.I | re.S)
AC_SPLIT = re.compile(r"<h3[^>]*>", re.I)
# Three forms in the wild: "Units: 2" (USC), "Credits: 3" (Hawaii) and
# "(3 units)" (CSULB) — the last puts the number before the word.
AC_UNITS = re.compile(
    r"(?:units?|credits?|credit hours?)\s*:\s*([\d.]+(?:\s*[-\u2013]\s*[\d.]+)?)"
    r"|\(\s*([\d.]+(?:\s*[-\u2013]\s*[\d.]+)?)\s*(?:units?|credits?|hours?)\s*\)",
    re.I)
AC_CATOID = re.compile(r"catoid=(\d+)", re.I)
AC_NAV_COURSES = re.compile(
    r'<a[^>]+href="([^"]*content\.php[^"]*navoid=(\d+)[^"]*)"[^>]*>\s*(?:<[^>]+>\s*)*'
    r'([^<]{0,60}courses?[^<]{0,60})</a>', re.I)


def ac_course_navoids(body):
    """Every nav entry that might be the course listing, best guess first.

    Columbia College's first 'course' link leads to a page with no courses on it.
    The listing has to be identified by what it returns, not by its label, so all
    candidates are collected and each is tried until one yields courses.
    """
    scored = []
    for _href, navoid, label in AC_NAV_COURSES.findall(body or ""):
        t = clean(label).lower()
        score = 0
        if "course description" in t:
            score += 5
        if re.fullmatch(r"courses?", t):
            score += 4
        if "course" in t:
            score += 2
        if "all courses" in t or "course listing" in t or "course inventory" in t:
            score += 3
        if "archive" in t or "search" in t:
            score -= 3
        scored.append((score, navoid))
    scored.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for _s, n in scored:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out

# Everything Acalog puts around the description. These are metadata, and left in
# they would feed the scorer sentences made of the word "prerequisite".
# Acalog paginates with its own links. Reading them beats guessing at the
# parameter: page 1 of USC returned 93 courses and the guessed cpage URL for
# page 2 returned nothing new, so the loop stopped at a twentieth of the catalog.
AC_PAGE_LINK = re.compile(
    r'href="([^"]*content\.php[^"]*filter(?:%5B|\[)cpage(?:%5D|\])=(\d+)[^"]*)"',
    re.I)

# Trailing s? matters: Acalog writes "Prerequisites:" as often as "Prerequisite:",
# and \b after "prerequisite" refuses to match when the next character is "s".
# CSULB writes "<hr>(3 units)<br>Prerequisite: ...<br>Description". A bare units
# parenthetical is metadata, but it does not match AC_META, so the loop treated
# it as the description, marked the description started, and then broke at the
# prerequisite — losing the real text entirely. 3,837 of CSULB's 4,579 records
# read exactly "(3 units)".
#
# This was never a regression. It has been wrong since the Acalog adapter
# shipped; the old coverage rule counted any non-empty string, so CSULB reported
# 100% coverage on nine-character descriptions. MIN_DESC_CHARS exposed it.
AC_BARE_UNITS = re.compile(
    r"^\s*\(?\s*[\d.]+(?:\s*[-\u2013]\s*[\d.]+)?\s*"
    r"(?:units?|credits?|hours?)\s*\)?\s*$", re.I)

AC_META = re.compile(
    r"^\s*(?:prerequisite|corequisite|co-?requisite|pre-?requisite|recommended "
    r"preparation|registration restriction|instruction mode|grading option|"
    r"terms? offered|unit|credit|credit hour|crosslisted|cross-?listed|"
    r"duplicates credit|repeatable|note|restriction|equivalent|when offered|"
    r"typically offered|class hour|lab/discussion|lab hour|lecture hour|"
    r"contact hour|studio hour|schedule type|grade mode)s?\b",
    # Bare "offered", "attribute", "component", "level", "college" and
    # "department" were tried here and removed: they kill real descriptions
    # that merely begin with the word. "Offered as a studio elective for
    # majors in their third year" is a description, not a field label.
    re.I)

# Some catalogs emit a bare grading or credit flag with no label and no colon —
# UMass Dartmouth writes "Graded" and "Exclude Credit" as standalone segments.
# Those are six and fourteen characters of nothing, and under the old non-empty
# coverage rule they made that school read 97% covered on 1% real descriptions.
AC_BARE_FLAG = re.compile(
    r"^\s*(?:graded|pass/?fail|ungraded|not graded|exclude credit|"
    r"no credit|audit|letter grade|s/u|p/np|in progress)\s*[.]?\s*$", re.I)


def parse_acalog_page(body, source_url):
    """Extract course records from one expanded Acalog listing page."""
    out = []
    body = body or ""
    for chunk in AC_SPLIT.split(body)[1:]:
        head, sep, rest = chunk.partition("</h3>")
        if not sep:
            continue
        parsed = parse_course_heading(head)
        if not parsed:
            continue
        code, title, _ = parsed

        # Stop at the end of this course's list item so the next course's text
        # cannot bleed into this one's description.
        rest = re.split(r"</li>|</td>", rest, maxsplit=1)[0]

        credits = ""
        um = AC_UNITS.search(rest[:400])
        if um:
            credits = re.sub(r"\s+", "", um.group(1) or um.group(2) or "")

        # The hr divides the metadata header from the description proper.
        after = rest.split("<hr", 1)[1] if "<hr" in rest else rest
        after = after.partition(">")[2] if after.startswith(("/>", " ", ">")) else after

        # Metadata sits on either side of the hr depending on the install:
        # USC writes "Units: 2<br>Terms Offered<hr>Description", Hawaii writes
        # "<hr>Credits: 3<br><br>Description". Breaking at the first metadata
        # segment returned nothing for every Hawaii-shaped catalog. Skip leading
        # metadata instead, and stop only once a description has begun.
        parts, started = [], False
        for seg in re.split(r"<br\s*/?>", after):
            t = clean(seg)
            if not t:
                continue
            if (AC_META.match(t) or AC_BARE_UNITS.match(t)
                    or AC_BARE_FLAG.match(t)):
                if started:
                    break
                t2 = strip_leading_requisite(t) if not started else ""   # HARVEST_V7
                if t2 and not (AC_META.match(t2) or AC_BARE_UNITS.match(t2) or AC_BARE_FLAG.match(t2)):
                    parts.append(t2)
                    started = True
                continue
            parts.append(t)
            started = True
        desc = " ".join(parts).strip()[:6000]
        # HARVEST_V8: attribute labels (<em><strong>…</strong></em>) and bare
        # "(3 cr.)" units are not description; strip them off the FRONT only.
        _labels = [clean(x) for x in re.findall(r"<em>\s*<strong>(.*?)</strong>\s*</em>", after, re.S)]
        _changed = True
        while _changed and desc:
            _changed = False
            m0 = re.match(r"^\(\s*\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?\s*(?:cr|credits?|units?)\.?\s*\)\s*", desc, re.I)
            if m0:
                desc = desc[m0.end():].strip(); _changed = True
            for lb in _labels:
                if lb and desc.startswith(lb):
                    desc = desc[len(lb):].strip(" ;,.-"); _changed = True
        if len(desc) < MIN_DESC_CHARS:
            # an attribute label was taken for the description and the real one
            # sits after a metadata line. Longest clean segment wins.
            cands = []
            for seg in re.split(r"<br\s*/?>", after):
                t = clean(seg)
                if (t and not AC_META.match(t) and not AC_BARE_UNITS.match(t)
                        and not AC_BARE_FLAG.match(t) and t not in _labels
                        and not re.match(r"^\(\s*\d", t)):
                    cands.append(t)
            if cands:
                best = max(cands, key=len)
                if len(best) >= MIN_DESC_CHARS and len(best) > len(desc):
                    desc = best[:6000]

        out.append({
            "code": code, "title": title, "credits": credits,
            "description": desc, "source_url": source_url,
            "tier": 1 if desc else 2,
        })
    return out


def ac_listing_url(base, catoid, navoid, page):
    q = {
        "catoid": catoid, "navoid": navoid, "expand": "1",
        "filter[27]": "-1", "filter[29]": "", "filter[keyword]": "",
        "filter[32]": "1", "filter[cpage]": str(page),
        "cur_cat_oid": catoid, "filter[exact_match]": "1",
    }
    return f"{base}/content.php?" + urllib.parse.urlencode(q)


def harvest_acalog(fetcher, inst, cap):
    url = inst["catalog_url"]
    res = fetcher.get(url)
    body = res["body"] or ""
    p = urllib.parse.urlparse(res["url"])
    base = f"{p.scheme}://{p.netloc}" + os.path.dirname(p.path).rstrip("/")

    m = AC_CATOID.search(res["url"]) or AC_CATOID.search(body)
    if not m:
        return [], 1, "no catoid found"
    catoid = m.group(1)

    if res["status"] in D.Fetcher.RETRY_STATUS:
        return [], 1, (f"catalog served HTTP {res['status']} challenge page; "
                       f"install playwright to clear it")

    navoids = ac_course_navoids(body)
    # HARVEST_OVERRIDE_V1: content.php?catoid=N&navoid=M from the worklist IS
    # the course listing. Try that navoid before any sidebar guess.
    if inst.get("_override"):
        mn = re.search(r"navoid=(\d+)", url, re.I)
        if mn:
            navoids = [mn.group(1)] + [n for n in navoids if n != mn.group(1)]
    if not navoids:
        return [], 1, "no courses navigation found"

    courses, pages = [], 1
    tried = []
    for navoid in navoids[:4]:
        got, used, err = _ac_walk(fetcher, base, catoid, navoid, cap)
        pages += used
        tried.append(f"navoid {navoid}: {len(got)}")
        if got:
            return got, pages, ""
        if err:
            return got, pages, err
    return [], pages, ("no courses on any candidate listing (" +
                       "; ".join(tried) + ")")


def _ac_walk(fetcher, base, catoid, navoid, cap):
    courses, pages = [], 0
    seen_codes = set()
    queue = [ac_listing_url(base, catoid, navoid, 1)]
    visited = set()

    while queue and pages <= cap:
        u = queue.pop(0)
        if u in visited or not fetcher.allowed(u):
            continue
        visited.add(u)
        r = fetcher.get(u)
        pages += 1
        if r["status"] in D.Fetcher.RETRY_STATUS:
            return courses, pages, (f"listing served HTTP {r['status']} hold page "
                                    f"after {len(visited)} page(s)")
        if r["status"] != 200:
            continue

        for c in parse_acalog_page(r["body"], r["url"]):
            if c["code"] not in seen_codes:
                seen_codes.add(c["code"])
                courses.append(c)

        # Follow Acalog's own pagination rather than guessing the parameter.
        for href, _num in AC_PAGE_LINK.findall(r["body"] or ""):
            nxt = urllib.parse.urljoin(r["url"], html.unescape(href))
            if nxt not in visited and nxt not in queue:
                queue.append(nxt)

    return courses, pages, ""


# ---------------------------------------------------------------- registry

# ---------------------------------------------------------------- coursedog

# Coursedog is a real single-page app. Its API lives on app.coursedog.com, not on
# the school's host, and it authorises by ORIGIN — no Authorization header, no
# cookie. Stripping referer and origin from a replay produces 401; keeping them
# produces 200. Verified against calarts, 2026-09-09.
#
# Response shape: {"listLength": N, "data": [...], "limit": L, "skip": S}
# Course fields: code, subjectCode, courseNumber, name, longName, description,
#                credits.creditHours.min
CD_SEARCH = "courses/search"
CD_DROP_HEADERS = {"host", "content-length", "connection", "accept-encoding"}
CD_PAGE = 100


def cd_pages(fetcher, inst):
    """-> candidate pages that might run the course search, best first.

    V27. The old code loaded exactly one address, `<catalog_url>/courses`, and
    gave up. That is right for Berkeley, whose catalog_url IS the Coursedog app,
    and wrong for Rutgers and UC Santa Cruz, where we recorded a registrar or
    landing page: /courses does not exist there, nothing is captured, and the
    school reports "playwright required" although Playwright was running.
    """
    root = inst["catalog_url"]
    base = root if root.endswith("/") else root + "/"
    out = [urllib.parse.urljoin(base, "courses"), root]
    if fetcher is not None:
        try:
            res = fetcher.get(root)
        except Exception:
            res = {}
        body = res.get("body") or ""
        host = urllib.parse.urlparse(root).netloc.lower()
        same, other = [], []
        for href, text in re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>',
                                     body, re.I | re.S):
            label = clean(text).lower()
            if "course" not in (href + " " + label).lower():
                continue
            u = urllib.parse.urljoin(res.get("url") or root, html.unescape(href))
            pu = urllib.parse.urlparse(u)
            # A catalog PDF is not a page that runs the search. Hunter's root
            # links three of them, and they filled the candidate list ahead of
            # the page that works.
            if pu.scheme not in ("http", "https") or pu.path.lower().endswith(
                    (".pdf", ".doc", ".docx", ".zip", ".csv", ".xlsx")):
                continue
            (same if pu.netloc.lower() == host else other).append(u)
        out += same + other
    seen, uniq = set(), []
    for u in out:
        if u.rstrip("/") not in seen:
            seen.add(u.rstrip("/"))
            uniq.append(u)
    return uniq[:6]


def cd_capture(inst, ua, fetcher=None):
    """One browser page-load per candidate, to learn the endpoint the app calls.

    The slug and catalogId cannot be derived from the hostname with any
    confidence, and guessing derivations is what cost this project four rounds.
    Ask the app.
    """
    try:
        import browser as B
    except ImportError:
        return None
    if not B.available():
        return None
    found = []
    for url in cd_pages(fetcher, inst):
        calls = B.capture(url, ua, match="coursedog", log=lambda m: log("  " + m))
        # GET as well as POST: some installs fetch the search over a query
        # string, and requiring POST discarded a working endpoint.
        hits = [c for c in calls
                if c.get("method") in ("POST", "GET") and CD_SEARCH in (c.get("url") or "")]
        if hits:
            log(f"  captured {len(hits)} search call(s) from {url}")
            found.extend(hits)
            # Several are normal: the app fires an empty search on load and a
            # filtered one after. Rutgers' first returns listLength 0, which is
            # why one capture was not enough.
            if len(found) > 1:
                break
        else:
            log(f"  no course search captured at {url}")
    return found or None


def cd_headers(post, catalog_url, ua):
    h = {k: v for k, v in (post.get("headers") or {}).items()
         if not k.startswith(":") and k.lower() not in CD_DROP_HEADERS
         and not str(v).startswith("<")}
    p = urllib.parse.urlparse(catalog_url)
    h.setdefault("content-type", "application/json")
    h.setdefault("accept", "application/json")
    h.setdefault("referer", catalog_url)
    h.setdefault("origin", f"{p.scheme}://{p.netloc}")
    h["user-agent"] = ua
    return h


def cd_page_url(url, skip, limit):
    u = re.sub(r"skip=\d+", f"skip={skip}", url)
    u = re.sub(r"limit=\d+", f"limit={limit}", u)
    return u


def parse_coursedog_page(payload, source_url):
    """Extract course records from one Coursedog search response."""
    out = []
    data = (payload or {}).get("data")
    if not isinstance(data, list):
        return out
    for c in data:
        if not isinstance(c, dict):
            continue
        subj = (c.get("subjectCode") or "").strip()
        num = (c.get("courseNumber") or "").strip()
        code = f"{subj} {num}".strip() if subj and num else (
            c.get("code") or "").strip()
        code = re.sub(r"^([A-Za-z]{2,6})(\d)", r"\1 \2", code).upper()
        title = clean(c.get("longName") or c.get("name") or "")
        if not code or not title:
            continue
        credits = ""
        ch = ((c.get("credits") or {}).get("creditHours") or {})
        lo, hi = ch.get("min"), ch.get("max")
        if lo is not None:
            credits = f"{lo}-{hi}" if hi not in (None, lo) else str(lo)
        rec = {
            "code": code, "title": title[:220], "credits": credits,
            "description": clean(c.get("description") or "")[:6000],
            "source_url": source_url,
            "tier": 1 if described(clean(c.get("description") or "")) else 2,
        }
        # The per-course identifier. source_url on this platform is the search
        # endpoint a whole batch came from, so it is neither public nor
        # per-course; this is the only thing that can rebuild a page link, and
        # it was previously discarded here. Costs nothing to keep.
        cd_id = (c.get("id") or c.get("courseGroupId")
                 or c.get("courseId") or "")
        if cd_id:
            rec["platform_id"] = str(cd_id)
        out.append(rec)
    return out


def cd_try(post, inst, fetcher, cap):
    """-> (courses, pages, note) for one captured search call."""
    url = post["url"]
    # A GET endpoint has no body; sending "{}" to one is a 400.
    body = (post.get("post_data") or "").encode() or None
    if post.get("method") == "POST" and not body:
        body = b"{}"
    headers = cd_headers(post, inst["catalog_url"], fetcher.ua)

    courses, pages, skip, total = [], 1, 0, None
    seen = set()
    while pages <= cap:
        u = cd_page_url(url, skip, CD_PAGE)
        req = urllib.request.Request(u, data=body, headers=headers,
                                     method=post.get("method") or "POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = json.loads(r.read(4_000_000).decode("utf-8", "ignore"))
        except Exception as e:
            return courses, pages, f"api error at skip={skip}: {type(e).__name__}: {e}"[:180]
        pages += 1

        if total is None:
            total = payload.get("listLength")
        batch = parse_coursedog_page(payload, u)
        fresh = [c for c in batch if c["code"] not in seen]
        for c in fresh:
            seen.add(c["code"])
        courses.extend(fresh)

        if not batch:
            break
        skip += CD_PAGE
        if total is not None and skip >= total:
            break
        time.sleep(1.0)          # app.coursedog.com serves every school

    return courses, pages, "" if courses else "api returned no courses"


def harvest_coursedog(fetcher, inst, cap):
    posts = cd_capture(inst, fetcher.ua, fetcher)
    if not posts:
        return [], 1, ("no course search captured on any candidate page "
                       "(tried the catalog root, /courses, and its course links)")
    # Try each captured call and keep the first that actually returns courses.
    best, pages_total, note = [], 0, ""
    for i, post in enumerate(posts, 1):
        courses, pages, err = cd_try(post, inst, fetcher, cap)
        pages_total += pages
        log(f"  search call {i}/{len(posts)}: {len(courses)} courses"
            + (f" ({err})" if err else ""))
        if len(courses) > len(best):
            best, note = courses, err
        if best:
            break
    return best, max(pages_total, 1), "" if best else (note or "api returned no courses")


# ---------------------------------------------------------------- cleancatalog

# Clean Catalog is a Drupal product; verified against catalog.ringling.edu.
# The course list lives at /classes, paginated A-Z, as teaser cards:
#
#   <article class="node--type-class ... course-teaser-card">
#     <a href="/computer-animation/anim-208" class="course-teaser-badge">ANIM 208</a>
#     <span class="field--name-field-credits ...">3</span>
#     <h2 class="course-teaser-title"><a ...><span ...>Traditional Animation I</span></a></h2>
#
# The teasers carry NO description — that sits on the linked detail page. Without
# following the link every Clean Catalog school lands at 0% coverage and is
# correctly unranked, which would waste the adapter. So the detail pages are
# fetched, capped, and the cap is reported.
CC_CARD = re.compile(r'<article[^>]+course-teaser-card.*?</article>', re.I | re.S)
CC_BADGE = re.compile(
    r'<a[^>]+href="([^"]+)"[^>]+course-teaser-badge[^>]*>(.*?)</a>', re.I | re.S)
CC_TITLE = re.compile(
    r'class="[^"]*course-teaser-title[^"]*"[^>]*>(.*?)</h2>', re.I | re.S)
CC_CREDITS = re.compile(
    r'field--name-field-credits[^>]*>(.*?)</span>', re.I | re.S)
CC_LETTER = re.compile(r'href="(/classes/[a-z0-9])"', re.I)
# HARVEST_CC_TABLE_V1: ArtCenter's table view. One <article> per row.
CC_TROW = re.compile(
    r'<article[^>]+class="[^"]*\bcourse-teaser-table\b[^"]*"[^>]*>.*?</article>',
    re.I | re.S)
CC_TROW_CODE = re.compile(
    r'course-teaser-table-label.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
CC_TROW_TITLE = re.compile(
    r'course-teaser-table-title.*?field--name-field-item[^>]*>(.*?)</span>', re.I | re.S)
CC_TROW_CREDITS = re.compile(
    r'course-teaser-table-credits.*?field--name-field-credits[^>]*>(.*?)</span>', re.I | re.S)
CC_PAGER = re.compile(r'<li[^>]+class="[^"]*pager__item[^"]*"[^>]*>\s*<a[^>]+href="([^"]+)"', re.I | re.S)
CC_DESC_ONE = re.compile(
    r'class="[^"]*field--name-field-description[^"]*"[^>]*>(.*?)</div>', re.I | re.S)
# The detail page body: Drupal's field--name-body, or the description field.
CC_DESC = re.compile(
    r'class="[^"]*field--name-(?:body|field-description)[^"]*"[^>]*>(.*?)'
    r'</div>\s*</div>', re.I | re.S)


def parse_cleancatalog_page(body, source_url):
    """Teaser cards from one /classes page. Descriptions are fetched separately."""
    out = []
    # HARVEST_CC_TABLE_V1: table rows first; a page has one shape or the other.
    for row in CC_TROW.findall(body or ""):
        km = CC_TROW_CODE.search(row)
        if not km:
            continue
        href, code = km.group(1), clean(km.group(2))
        code = re.sub(r"[\s\-\u2013]+", " ", code).strip().upper()
        tm = CC_TROW_TITLE.search(row)
        title = clean(tm.group(1)) if tm else ""
        if not code or not title:
            continue
        cm = CC_TROW_CREDITS.search(row)
        credits = clean(cm.group(1)) if cm else ""
        out.append({
            "code": code, "title": title[:220],
            "credits": re.sub(r"[^\d.\-]", "", credits),
            "description": "", "detail_href": href,
            "source_url": source_url, "tier": 2,
        })
    for card in CC_CARD.findall(body or ""):
        bm = CC_BADGE.search(card)
        if not bm:
            continue
        href, code = bm.group(1), clean(bm.group(2))
        code = re.sub(r"[\s\-\u2013]+", " ", code).strip().upper()
        tm = CC_TITLE.search(card)
        title = clean(tm.group(1)) if tm else ""
        if not code or not title:
            continue
        cm = CC_CREDITS.search(card)
        credits = clean(cm.group(1)) if cm else ""
        out.append({
            "code": code, "title": title[:220],
            "credits": re.sub(r"[^\d.\-]", "", credits),
            "description": "", "detail_href": href,
            "source_url": source_url, "tier": 2,
        })
    return out


def cc_class_urls(catalog_url):
    """Candidate /classes URLs, deepest first.

    Ringling serves the catalog at a host root, so root + "/classes" works.
    ArtCenter serves it at handbooks.artcenter.edu/artcenter-catalog, a subpath,
    where the same construction 404s. Try the subpath first, then the host root.
    """
    root = catalog_url.rstrip("/")
    out = [root + "/classes"]
    p = urllib.parse.urlparse(root)
    host_root = f"{p.scheme}://{p.netloc}"
    if host_root.rstrip("/") != root:
        out.append(host_root + "/classes")
    return out


def harvest_cleancatalog(fetcher, inst, cap):
    root = inst["catalog_url"].rstrip("/")
    listing, r = None, None
    # HARVEST_OVERRIDE_V2: a verified override URL is the listing; try it first.
    _cands = ([root] if inst.get("_override") else []) + cc_class_urls(inst["catalog_url"])
    for u in _cands:
        if not fetcher.allowed(u):
            continue
        rr = fetcher.get(u)
        if rr["status"] == 200 and rr.get("body") and (
                CC_CARD.search(rr["body"]) or CC_TROW.search(rr["body"])):
            listing, r = u, rr
            root = u.rstrip("/").rsplit("/", 1)[0]
            break
    if r is None:
        # Fall back to whatever the catalog root links to as "Courses".
        rootres = fetcher.get(inst["catalog_url"])
        for u in D.catalog_links(rootres, limit=10):
            if not u.rstrip("/").endswith("/classes"):
                continue
            rr = fetcher.get(u)
            if rr["status"] == 200 and CC_CARD.search(rr.get("body") or ""):
                listing, r = u, rr
                root = u[: -len("/classes")]
                break
    if r is None:
        return [], 1, "no /classes listing with teaser cards or table rows found"

    pages = [listing]
    for href in CC_LETTER.findall(r["body"]):
        u = urllib.parse.urljoin(listing, href)
        if u not in pages:
            pages.append(u)

    # HARVEST_CC_TABLE_V1: pager links (?page=N) discovered page by page.
    def _pager(body, base):
        out = []
        for href in CC_PAGER.findall(body or ""):
            u = urllib.parse.urljoin(base, html.unescape(href))
            if u not in pages and u not in out:
                out.append(u)
        return out

    courses, seen, fetched = [], set(), 1
    pages += _pager(r["body"], listing)
    i = 0
    while i < len(pages):
        u = pages[i]
        i += 1
        if fetched > cap:
            break
        rr = r if u == listing else fetcher.get(u)
        fetched += 0 if u == listing else 1
        if rr["status"] != 200:
            continue
        for c in parse_cleancatalog_page(rr["body"], rr["url"]):
            if c["code"] not in seen:
                seen.add(c["code"])
                courses.append(c)
        pages += _pager(rr["body"], rr["url"])

    if not courses:
        return [], fetched, "/classes reachable but no teaser cards parsed"

    # Descriptions live one hop away, one fetch per course.
    truncated = False
    for c in courses:
        if fetched > cap:
            truncated = True
            break
        u = urllib.parse.urljoin(root + "/", c.pop("detail_href", "").lstrip("/"))
        if not u or not fetcher.allowed(u):
            continue
        d = fetcher.get(u)
        fetched += 1
        if d["status"] != 200:
            continue
        dm = CC_DESC_ONE.search(d["body"] or "") or CC_DESC.search(d["body"] or "")
        if dm:
            txt = clean(dm.group(1))
            if described(txt):
                c["description"] = txt[:6000]
                c["tier"] = 1
                c["source_url"] = d["url"]
    for c in courses:
        c.pop("detail_href", None)

    err = (f"PAGE CAP HIT at {cap} — descriptions incomplete, raise --cap"
           if truncated else "")
    return courses, fetched, err


# ---------------------------------------------------------------- smartcatalog
# HARVEST_SC_V1. SmartCatalog IQ (Suffolk, Kean, Lesley, Portland State,
# UTRGV, Suffolk …). Root /courses lists subjects; a subject page lists every
# course inline; the course page carries the description.
SC_ANY_LINK = re.compile(r'href="([^"#?]+)"', re.I)   # HARVEST_V6
SC_COURSE_LINK = re.compile(
    r'<li>\s*<a href="([^"#?]+)"[^>]*>\s*<span>([^<]+)</span>\s*([^<]+)</a>', re.I | re.S)
# HARVEST_V9: Portland State writes the code inline: Art&nbsp;101&nbsp;Title
SC_COURSE_LINK_PLAIN = re.compile(
    r'<li>\s*<a href="([^"#?]+)"[^>]*>\s*([A-Za-z]{2,5}(?:&nbsp;|\s)\d{3,4}[A-Z]?)(?:&nbsp;|\s)+([^<]+)</a>', re.I | re.S)
SC_DESC = re.compile(r'<div class="desc">(.*?)</div>', re.I | re.S)
# HARVEST_V8: inline entries — h2 with the course link, then the desc div.
SC_INLINE = re.compile(
    r'<h2>\s*<a href="([^"#?]+)"[^>]*>\s*<span>([^<]+)</span>\s*([^<]+)</a>\s*</h2>'
    r'(?:\s*<div class="desc">(.*?)</div>)?', re.I | re.S)
SC_CREDITS = re.compile(r'<div class="credits">\s*([^<]*?)\s*</div>', re.I | re.S)


def harvest_smartcatalog(fetcher, inst, cap):
    root = inst["catalog_url"].rstrip("/")
    r = fetcher.get(root)
    if r["status"] != 200 or not r.get("body"):
        return [], 1, f"root returned {r['status']}"
    base = r["url"]
    # HARVEST_V6: a subject page is exactly one path segment below the root.
    rootpath = urllib.parse.urlparse(root).path.rstrip("/") + "/"
    subjects = []
    for href in SC_ANY_LINK.findall(r["body"]):
        u = urllib.parse.urljoin(base, html.unescape(href))
        pth = urllib.parse.urlparse(u).path
        if not pth.startswith(rootpath):
            continue
        tail = pth[len(rootpath):].strip("/")
        if tail and "/" not in tail and u not in subjects:
            subjects.append(u)
    if not subjects:
        return [], 1, f"no subject links one level below {rootpath}"
    inst["_index_url"] = base
    courses, seen, fetched = [], set(), 1

    def _inline(body, page_url):
        """HARVEST_V8: courses written inline on this page, with descriptions."""
        n = 0
        for href, code, title, desc in SC_INLINE.findall(body or ""):
            code = re.sub(r"[\s\-]+", " ", clean(code)).strip().upper()
            title = clean(title)
            if not code or not title or code in seen:
                continue
            seen.add(code)
            d = clean(desc) if desc else ""
            courses.append({
                "code": code, "title": title[:220], "credits": "",
                "description": d[:6000],
                "source_url": urllib.parse.urljoin(page_url, html.unescape(href)),
                "tier": 1 if d else 2,
            })
            n += 1
        return n

    if _inline(r["body"], base) >= 50:
        # Tampa: the root IS the catalogue. Subject pages would only repeat it.
        subjects = []
    for su in subjects:
        if fetched > cap:
            break
        sr = fetcher.get(su); fetched += 1
        if sr["status"] != 200 or not sr.get("body"):
            continue
        _inline(sr["body"], sr["url"])
        subjpath = urllib.parse.urlparse(su).path.rstrip("/") + "/"
        _links = SC_COURSE_LINK.findall(sr["body"])
        if not _links:
            _links = [(h, html.unescape(c).replace("\xa0", " "), t)
                      for h, c, t in SC_COURSE_LINK_PLAIN.findall(sr["body"])]   # HARVEST_V9
        for href, code, title in _links:
            if not urllib.parse.urlparse(urllib.parse.urljoin(su, html.unescape(href))).path.startswith(subjpath):
                continue   # HARVEST_V6: only this subject's own course links
            code = re.sub(r"[\s\-]+", " ", clean(code)).strip().upper()
            title = clean(title)
            if not code or not title or code in seen:
                continue
            seen.add(code)
            cu = urllib.parse.urljoin(sr["url"], html.unescape(href))
            desc, credits = "", ""
            if fetched <= cap:
                cr = fetcher.get(cu); fetched += 1
                if cr["status"] == 200 and cr.get("body"):
                    dm = SC_DESC.search(cr["body"]); desc = clean(dm.group(1)) if dm else ""
                    cm = SC_CREDITS.search(cr["body"]); credits = clean(cm.group(1)) if cm else ""
            courses.append({
                "code": code, "title": title[:220], "credits": credits,
                "description": desc[:6000], "source_url": cu,
                "tier": 1 if desc else 2,
            })
    err = ""
    if fetched > cap:
        err = f"CAP {cap} reached after {len(courses)} courses; raise --cap"
    return courses, fetched, err


# ---------------------------------------------------------------- pdf
# HARVEST_PDF_V1. Catalogue PDFs: MICA, KCAI, CIA, CCS, MCAD and most of the
# specialist art colleges publish nothing else. pdfcat.py holds the parser,
# written against six real catalogues on 2026-09-17.

def _pdf_download(url, path, ua):
    import ssl, urllib.error
    m = re.match(r"(https://docs\.google\.com/document/d/(?:e/)?[^/]+)", url)
    if m and "export" not in url:
        url = m.group(1) + ("/pub?output=pdf" if "/d/e/" in url else "/export?format=pdf")
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/pdf,*/*"})
    try:
        r = urllib.request.urlopen(req, timeout=120)
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" not in str(e):
            raise
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        r = urllib.request.urlopen(req, timeout=120, context=ctx)
    with r:
        data = r.read()
    if not data.startswith(b"%PDF"):
        return False, r.headers.get("Content-Type", "")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return True, "application/pdf"


def harvest_pdf(fetcher, inst, cap):
    import pdfcat
    url = inst["catalog_url"]
    path = os.path.join("cache", "pdf", f"{inst['unitid']}.pdf")
    if not (os.path.exists(path) and open(path, "rb").read(5).startswith(b"%PDF")):
        try:
            ok, ctype = _pdf_download(url, path, getattr(fetcher, "ua", D.DEFAULT_CONTACT))
        except Exception as e:
            return [], 1, f"pdf download failed: {type(e).__name__}: {e}"[:160]
        if not ok:
            return [], 1, f"link is not a PDF ({ctype}); viewer/flipbook/doc — needs a file link"
    try:
        pages = pdfcat.extract_pages(path)
    except Exception as e:
        return [], 1, f"pdf extract failed: {type(e).__name__}: {e}"[:160]
    courses, interleaved = pdfcat.parse_pages(pages, url)
    inst["_index_url"] = url
    err = ""
    if interleaved:
        err = (f"{interleaved} of {len(pages)} pages skipped as multi-column; "
               f"coverage is partial")
    if not courses:
        err = "pdf parsed but no course codes found (catalogue lists courses by title only?)"
    return courses, len(pages), err


def not_implemented(name):
    def _f(fetcher, inst, cap):
        return [], 0, f"{name} adapter not implemented yet"
    return _f


ADAPTERS = {
    "courseleaf": harvest_courseleaf,
    "acalog": harvest_acalog,
    "coursedog": harvest_coursedog,
    "smartcatalog": harvest_smartcatalog,   # HARVEST_SC_V1
    "kuali": not_implemented("kuali"),
    "cleancatalog": harvest_cleancatalog,
    "pdf": harvest_pdf,          # HARVEST_PDF_V1
    "other": not_implemented("other"),
}


# ---------------------------------------------------------------- run

def harvest_one(fetcher, inst, cap, out_dir):
    plat = inst.get("platform", "")
    rec = {
        "marker": MARKER,
        "unitid": inst["unitid"], "name": inst["name"],
        "platform": plat, "catalog_url": inst.get("catalog_url", ""),
        "catalog_year": inst.get("catalog_year", ""),
        "render": inst.get("render", ""),
        "retrieved": hst_today(),
        "measures": "course code, title, description, credits. No scoring.",
        "courses": [], "n_courses": 0, "pages_fetched": 0, "error": "",
    }
    fn = ADAPTERS.get(plat)
    if fn is None:
        rec["error"] = f"no adapter for platform '{plat}'"
        return rec
    try:
        courses, pages, err = fn(fetcher, inst, cap)
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"[:200]
        return rec

    courses = enrich(courses)
    rec["courses"] = courses
    rec["n_courses"] = len(courses)          # equals len(courses), always
    rec["pages_fetched"] = pages
    rec["error"] = err
    rec["index_url"] = inst.get("_index_url", "")
    if inst.get("_override"):
        rec["catalog_url_override"] = True
        rec["catalog_url_discovered"] = inst.get("catalog_url_discovered", "")
        rec["override_status"] = inst.get("override_status", "")
    rec["tier_counts"] = dict(Counter(str(c["tier"]) for c in courses))
    rec["min_description_chars"] = MIN_DESC_CHARS
    rec["description_coverage"] = (
        round(sum(1 for c in courses if c["tier"] == 1) / len(courses), 3)
        if courses else 0.0)
    rec["nonempty_description_rate"] = (
        round(sum(1 for c in courses if (c.get("description") or "").strip())
              / len(courses), 3) if courses else 0.0)
    rec["duplicate_rate"] = duplicate_rate(courses)
    rec["n_courses_deduped"] = n_deduped(courses)
    rec["subjects"] = sorted({c["subject"] for c in courses if c["subject"]})

    # Never overwrite a better result with a worse one. A regression in an
    # adapter should cost a run, not the corpus: V10 wrote zero-course files over
    # Temple's 10,160 and NYU's 4,934 before anyone saw the yield table.
    path = os.path.join(out_dir, f"{inst['unitid']}.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                prev = json.load(fh)
            if prev.get("n_courses", 0) > rec["n_courses"]:
                rec["error"] = (f"kept previous harvest ({prev['n_courses']} "
                                f"courses) — this run returned {rec['n_courses']}")
                rec["regressed"] = True
                return rec
        except Exception:
            pass
    write_json_atomic(path, rec)
    return rec


def main():
    p = argparse.ArgumentParser(description="Harvest courses from located catalogs.")
    p.add_argument("--catalogs", default="data/catalogs.json")
    p.add_argument("--out-dir", default="data/courses")
    p.add_argument("--log-dir", default="data")
    p.add_argument("--cache-dir", default="cache/discover",
                   help="shared with discover.py on purpose; pages already "
                        "fetched are not fetched again")
    p.add_argument("--platforms", default="courseleaf,acalog",
                   help="comma separated; only these are attempted")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--schools", default="",
                   help="comma-separated unitids; overrides --platforms. For "
                        "diagnosing one failure without re-running a platform")
    p.add_argument("--cap", type=int, default=600,
                   help="max subject pages or listing pages per school. "
                        "200 silently truncated Madison, which has ~200 subject "
                        "pages, to a third of its catalog")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--timeout", type=float, default=25.0)
    p.add_argument("--contact", default=D.DEFAULT_CONTACT)
    p.add_argument("--no-solve", action="store_true",
                   help="do not use a browser to clear JavaScript challenges")
    p.add_argument("--recompute", action="store_true",
                   help="re-derive subject, section, tier, coverage and duplicate "
                        "rate from already-harvested files. No fetching.")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--lock", default="data/.harvest.lock",
                   help="one harvest or recompute at a time; flock-based")
    p.add_argument("--overrides", default="",
                   help="worklist CSV; rows with harvest_ready=yes replace "
                        "catalog_url with course_catalog_url (HARVEST_OVERRIDE_V1)")
    p.add_argument("--include-attempt", action="store_true",
                   help="with --overrides, also take harvest_ready=attempt rows "
                        "(search-only interfaces; a zero records as blocked)")
    a = p.parse_args()

    log(f"{MARKER} — run {hst_today()} HST")
    ok, holder = acquire_lock(a.lock)
    if not ok:
        log(f"  ANOTHER HARVEST HOLDS THE LOCK — {holder or a.lock}")
        log("  Refusing to run. Two harvests, or a recompute during a harvest,")
        log("  write the same files. Check: pgrep -fl harvest.py")
        sys.exit(3)

    if a.recompute:
        n = 0
        for fn in sorted(os.listdir(a.out_dir)):
            if not fn.endswith(".json"):
                continue
            fp = os.path.join(a.out_dir, fn)
            with open(fp, encoding="utf-8") as fh:
                rec = json.load(fh)
            cs = enrich(rec.get("courses", []))
            rec["courses"] = cs
            rec["n_courses"] = len(cs)
            rec["marker"] = MARKER
            rec["min_description_chars"] = MIN_DESC_CHARS
            rec["tier_counts"] = dict(Counter(str(c["tier"]) for c in cs))
            rec["description_coverage"] = (
                round(sum(1 for c in cs if c["tier"] == 1) / len(cs), 3)
                if cs else 0.0)
            rec["nonempty_description_rate"] = (
                round(sum(1 for c in cs if (c.get("description") or "").strip())
                      / len(cs), 3) if cs else 0.0)
            rec["duplicate_rate"] = duplicate_rate(cs)
            rec["n_courses_deduped"] = n_deduped(cs)
            rec["subjects"] = sorted({c["subject"] for c in cs if c["subject"]})
            missing = sum(1 for c in cs if not c.get("source_url"))
            if missing:
                log(f"  {rec['name']}: {missing} records have no source_url")
            write_json_atomic(fp, rec)
            n += 1
            log(f"  {rec['name'][:40]:<40} cov {rec['description_coverage']:.0%} "
                f"(nonempty {rec['nonempty_description_rate']:.0%})  "
                f"dupe {rec['duplicate_rate']:.1%}  "
                f"dedup {rec['n_courses_deduped']}/{rec['n_courses']}  "
                f"{len(rec['subjects'])} subjects")
        log(f"\nRecomputed {n} files with MIN_DESC_CHARS={MIN_DESC_CHARS}")
        return

    with open(a.catalogs, encoding="utf-8") as fh:
        cat = json.load(fh)

    # HARVEST_OVERRIDE_V1
    if a.overrides:
        take = {"yes"} | ({"attempt"} if a.include_attempt else set())
        by_uid = {str(i["unitid"]): i for i in cat["institutions"]}
        n_set = n_new = n_plat = 0
        with open(a.overrides, encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                if (row.get("harvest_ready") or "").strip() not in take:
                    continue
                url = (row.get("course_catalog_url") or "").strip()
                if not url:
                    continue
                uid = str(row["unitid"]).strip()
                inst = by_uid.get(uid)
                if inst is None:
                    inst = {"unitid": uid, "name": row.get("school", uid),
                            "state": row.get("state", ""), "completions": 0,
                            "homepage": row.get("homepage", ""),
                            "found_via": "worklist", "confidence": "verified",
                            "catalog_year": ""}
                    cat["institutions"].append(inst)
                    by_uid[uid] = inst
                    n_new += 1
                plat = (row.get("platform") or "").strip()
                if plat and plat in ADAPTERS and plat != inst.get("platform"):
                    inst["platform"] = plat
                    n_plat += 1
                inst["catalog_url_discovered"] = inst.get("catalog_url", "")
                inst["catalog_url"] = url
                inst["_override"] = True
                inst["override_status"] = row.get("status", "")
                n_set += 1
        log(f"  OVERRIDES from {a.overrides}: {n_set} catalog_url set "
            f"({n_new} institutions added, {n_plat} platforms corrected)"
            + ("; attempt rows included" if a.include_attempt else ""))

    want = {s.strip() for s in a.platforms.split(",") if s.strip()}
    if a.schools:
        ids = {x.strip() for x in a.schools.split(",") if x.strip()}
        insts = [i for i in cat["institutions"]
                 if str(i["unitid"]) in ids and i.get("catalog_url")]
        want = {i.get("platform") for i in insts}
    else:
        insts = [i for i in cat["institutions"]
                 if i.get("catalog_url") and i.get("platform") in want]
    insts.sort(key=lambda i: -i.get("completions", 0))
    if a.limit:
        insts = insts[:a.limit]

    log(f"  {len(insts)} institutions on platforms: {', '.join(sorted(want))}")
    log(f"  {a.workers} workers, {a.delay}s per domain, cap {a.cap} pages/school")
    if a.refresh:
        log("  --refresh: ignoring cached pages")

    os.makedirs(a.out_dir, exist_ok=True)
    fetcher = D.Fetcher(a.cache_dir, a.contact, a.delay, a.timeout,
                        a.insecure, refresh=a.refresh,
                        solve_challenges=not a.no_solve)
    if not a.no_solve:
        import browser as B
        log(f"  javascript challenges: "
            f"{'browser available' if B.available() else 'PLAYWRIGHT NOT INSTALLED'}")

    results, done, t0 = [], 0, time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(harvest_one, fetcher, i, a.cap, a.out_dir): i for i in insts}
        for f in as_completed(futs):
            results.append(f.result())
            done += 1
            if done % 10 == 0 or done == len(insts):
                log(f"  {done}/{len(insts)}  ({time.time()-t0:.0f}s)")

    results.sort(key=lambda r: -r["n_courses"])
    ok = [r for r in results if r["n_courses"] > 0]
    total = sum(r["n_courses"] for r in results)

    log(f"\nYIELD")
    log(f"  {len(ok)}/{len(results)} schools returned courses")
    log(f"  {total} course records")
    if ok:
        med = sorted(r["n_courses"] for r in ok)[len(ok) // 2]
        log(f"  median {med} courses per school")
        cov = sum(r["description_coverage"] for r in ok) / len(ok)
        log(f"  mean description coverage {cov:.1%}  "
            f"[below ~70% and score.py loses signal]")

    by_plat = defaultdict(lambda: [0, 0, 0])
    for r in results:
        b = by_plat[r["platform"]]
        b[0] += 1
        b[1] += 1 if r["n_courses"] else 0
        b[2] += r["n_courses"]
    log("\nBy platform")
    log(f"  {'platform':<14}{'schools':>8}{'with courses':>14}{'courses':>10}")
    for plat, (n, w, c) in sorted(by_plat.items(), key=lambda kv: -kv[1][2]):
        log(f"  {plat:<14}{n:>8}{w:>14}{c:>10}")

    regressed = [r for r in results if r.get("regressed")]
    if regressed:
        log(f"\n  {len(regressed)} schools returned FEWER courses than the "
            f"previous run — previous kept, THIS IS AN ADAPTER REGRESSION:")
        for r in regressed[:15]:
            log(f"     {r['name'][:38]:<38} {r['error'][:56]}")

    empty = [r for r in results if not r["n_courses"]]
    if empty:
        log(f"\n  {len(empty)} returned nothing — REVIEW:")
        for r in empty[:20]:
            log(f"     {r['platform']:<12} {r['name'][:34]:<34} {r['error'][:44]}")

    log("\nTop yields, sanity check these against the live catalog")
    for r in ok[:10]:
        log(f"  {r['n_courses']:>6} courses  {r['description_coverage']:>5.0%} desc  "
            f"{r['name'][:40]}")

    sp = os.path.join(a.log_dir, "harvest_summary.csv")
    with open(sp, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["unitid", "name", "platform", "catalog_year", "n_courses",
                    "description_coverage", "pages_fetched", "error"])
        for r in results:
            w.writerow([r["unitid"], r["name"], r["platform"], r["catalog_year"],
                        r["n_courses"], r["description_coverage"],
                        r["pages_fetched"], r["error"]])
    log(f"\nWrote {sp}")
    log(f"Wrote {len(ok)} files to {a.out_dir}/")

    lp = os.path.join(a.log_dir, "harvest_run.log")
    with open(lp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LOG) + "\n")
    log(f"Wrote {lp}")

    if not a.no_solve:
        import browser as B
        B.shutdown()


if __name__ == "__main__":
    main()
