#!/usr/bin/env python3
"""
BOTTEGA_SCORE_V34   (the MARKER constant below is authoritative)

score.py — score harvested courses against the rubric, in two stages.

Stage one is a deliberately over-broad keyword prefilter, tuned for recall. It
cuts a corpus of a million-plus course records down to a few percent.

Stage two classifies only that shortlist with a model, which is where precision
comes from. Every classification is cached by course-text hash, so a rerun costs
nothing and reproduces exactly.

The RUBRIC IS NOT IN THIS FILE. It lives in rubric_v0.1.json, because what counts
as entrepreneurship training and where the line falls between a 2 and a 3 are
editorial decisions, not code. Change the rubric, bump its version, rerun.

USAGE  (--rubric is required: a forgotten flag once meant a silent v0.2 run)
  python3 score.py --rubric rubric_v0.7.json --dry-run
  python3 score.py --rubric rubric_v0.7.json --schools 217493,140951
  python3 score.py --rubric rubric_v0.7.json

  export ANTHROPIC_API_KEY=sk-...         # required for stage two

OUTPUTS
  data/scores/<unitid>.json    per-axis counts, shares, scores, and every hit
  data/index.csv               one row per school, the ranking table
  data/classifications.jsonl   the classification cache; deleting it costs money
  data/score_run.log
"""

import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

MARKER = "BOTTEGA_SCORE_V40"
API_URL = "https://api.anthropic.com/v1/messages"

# Enforced, not advisory. A school below either floor is reported UNRANKED with a
# stated reason and is excluded from the ranking and from the share distribution
# the thresholds get calibrated against. Scoring it low would be reporting our
# own coverage as their curriculum.
COVERAGE_FLOOR = 0.70

# The unit of analysis is the ART UNIT: what fraction of an art unit's offerings
# sit on each axis. That is n_matched / n_art_courses. Whole-catalog share
# answers "how much of this university is art-venture", which is a different
# question nobody asked.
#
# The art-subject set is model-unstable — median 10.6% movement between
# classifier models, SFSU +110%, Emerson -99% — and that is a stated limitation
# to be reduced by improving the subject classifier, not a reason to answer a
# different question. Both shares stay on every record either way.
SHARE_KEY = "share_of_art_catalog"
SHARE_LABEL = "art-subject"

# Evidence is the whole defence of the index, so the cap is generous and, when it
# bites, the record says so rather than quietly disagreeing with n_matched.
EVIDENCE_CAP = 500

# --- art-subject denominator -------------------------------------------------
# A corpus-wide prefix list cannot work: Illustration is ILLUS at RISD, ILLU at
# SCAD, CDIL at MassArt, and folded into FA at Pratt. None reaches 8 schools, so
# a list built from common prefixes gives RISD a denominator near zero. Classify
# each school's own prefixes instead, with the instrument already in use.
#
# Scope is inherited from the roster, so music, theatre and dance are out by
# construction rather than by taste.
CIP_SCOPE = [
    "50.01 Visual and Performing Arts, General (including Digital Arts)",
    "50.02 Crafts, Folk Art and Artisanry",
    "50.04 Design and Applied Arts",
    "50.06 Film/Video and Photographic Arts",
    "50.07 Fine and Studio Arts",
    "50.13 Community/Environmental/Socially-Engaged Art",
]
# SUBJECT_SAMPLES (first five titles) retired in V20: see SAMPLE_RULE.
# Florida, The New School and TCU returned unparseable subject responses under
# BOTH model passes. Deterministic across two different models means structural,
# not a model failure — these are the schools with the longest prefix lists, and
# the response was being truncated. The New School has 324 prefixes and is the
# largest art school in the corpus, with no denominator at all.
SUBJECT_CHUNK = 80

# An implausible art share is a subject-classification failure presenting as a
# school. Same shape as the coverage floors: flag, state the reason, do not rank.
# Institution type is not in the course files, so the upper rule only fires on
# large catalogs.
ART_SHARE_MAX = 0.90
ART_SHARE_MAX_MIN_COURSES = 3000
ART_SHARE_MIN = 0.02
# A classification-failure label needs a real catalog behind it. Alabama yielded
# 20 courses, Azusa 15, UC Irvine 12 — nothing classified those wrongly, the
# harvest never got them, and saying "classification failed" repeats the error
# corrected for Maryland one level down. Penn State (299 courses, 0 of 51
# prefixes) and UTSA (1,864, 0 of 63) are the real failures.
CLASSIFY_FAIL_MIN_COURSES = 200
# ...and enough DEPARTMENTS to have been a classification. Missouri-Columbia
# showed 0 of 1 prefixes in 283 courses: one department captured, not a catalog
# — a harvest failure. Penn State read 51 prefixes and UTSA 63, judged none art,
# and those are classification failures. The prefix count is what separates them.
CLASSIFY_FAIL_MIN_PREFIXES = 10
# A denominator of 40 turns one matched course into 2.5%, off the top of any
# threshold. Same hazard as a seven-course catalog, one shared rule.
ART_DENOM_FLOOR = 100

# A harvest far below the corpus norm did not read the catalog. Both numbers are
# published; neither is a judgement about the school.
MIN_PLAUSIBLE_COURSES = 50
# The ratio rule is DISABLED by default, because courses-per-degree varies ~50x
# by institution type for legitimate reasons. Arizona is 18,016 courses over 215
# CIP-50 degrees (83.8); RISD is 1,293 over ~700 (1.8). An art college confers
# many degrees from a small focused catalog, so a median drawn across both types
# would flag RISD as a failed harvest — the coverage-bias failure arriving
# through the rule written to prevent it.
#
# The absolute floor catches what matters (7 courses is a failed read at any
# school). The ratio distribution is printed so the rule can be set from data,
# by axis of institution type, rather than guessed here.
HARVEST_RATIO_FLOOR = 0.0

LOG = []
MODEL_OVERRIDDEN = [False]


def log(msg):
    LOG.append(str(msg))
    print(msg, file=sys.stderr)


def hst_today():
    return datetime.now(timezone(timedelta(hours=-10))).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- stage one

def term_regex(term):
    """Word-boundaried. A trailing * means prefix match: negotiat* -> negotiation."""
    t = term.strip()
    prefix = t.endswith("*")
    if prefix:
        t = t[:-1]
    if not t:
        return None
    core = re.escape(t)
    left = r"\b" if t[0].isalnum() else ""
    right = "" if prefix else (r"\b" if t[-1].isalnum() else "")
    return left + core + right


def build_matchers(rubric):
    """Compile one recall-oriented regex per axis. Boundaried; * = prefix.

    v0.1 stripped whitespace before escaping, so ' ai ' became a bare substring
    matching painting, detail, available, maintain, chair. In an index of art
    schools the technology axis matched nearly everything.
    """
    out = {}
    for axis, spec in rubric["axes"].items():
        parts = [term_regex(t) for t in
                 sorted(spec["lexicon"], key=len, reverse=True)]
        out[axis] = re.compile("|".join(p for p in parts if p), re.I)
    return out


def which_terms(rubric, axis, text):
    """Which lexicon terms fired. Feeds the per-term carry report."""
    hits = []
    for t in rubric["axes"][axis]["lexicon"]:
        rx = term_regex(t)
        if rx and re.search(rx, text, re.I):
            hits.append(t)
    return hits


def course_text(c):
    return f"{c.get('title', '')}. {c.get('description', '')}".strip()


# --- within-school collapse -------------------------------------------------
# Scoring-chat rule, v0.7 blocking list item 4: within a school, records
# sharing a normalised title and description prefix are ONE course. CalArts put
# 42 entries into the Self census and 20 were the same course at undergraduate
# and graduate level. Counted raw, that inflates n_matched and n_art_courses at
# different rates: the numerator/denominator asymmetry, a third time.
#
# The key is harvest.py's dedupe_key, byte for byte, so harvest's
# n_courses_deduped and this file's n_courses agree. test_score.py asserts it.
# Blank titles are never merged, as in harvest.
# Set by --dedupe-articles. harvest.ARTICLE_STRIP must hold the same value:
# the two dedupe_key functions are one rule in two files and test_score asserts
# they agree. A leading article is not a second course — The New School lists
# NFLM 0353 "Business of Documentary" and NFLM 3535 "The Business of
# Documentary" with identical 756-character descriptions.
ARTICLE_STRIP = False

_ARTICLE = __import__("re").compile(r"^(the|a|an)\s+", __import__("re").I)

DEDUPE_RULE = ("within a school, records sharing a lowercased, stripped title "
               "and the first 200 characters of description are one course; "
               "blank titles are never merged")
DEDUPE_RULE_ARTICLES = ("within a school, records sharing a lowercased, "
                        "stripped title with any leading article removed, and "
                        "the first 200 characters of description, are one "
                        "course; blank titles are never merged")


def dedupe_rule():
    return DEDUPE_RULE_ARTICLES if ARTICLE_STRIP else DEDUPE_RULE


def dedupe_key(c):
    t = (c.get("title", "") or "").strip().lower()
    if ARTICLE_STRIP:
        t = _ARTICLE.sub("", t).strip()
    return (t, ((c.get("description", "") or "")[:200]).strip().lower())


# A description that is only a pointer at another course. Not a length test:
# the ruling is explicitly about what the text IS, not how short it is.
XREF_ONLY = __import__("re").compile(
    r"^\(?\s*(see|same as|formerly|cross[-\s]?listed(\s+(as|with))?)\b"
    r"[^.;]{0,60}\.?\s*\)?\.?$", __import__("re").I)
XREF_TARGET = __import__("re").compile(
    r"\b(?:see|same as|formerly|cross[-\s]?listed(?:\s+(?:as|with))?)\b"
    r"[^.;]{0,40}?\b([A-Z][A-Z&\s]{1,9}?)\s?(\d{2,5}[A-Za-z]?)", __import__("re").I)

WITHHOLD_BLANK = False
RESOLVE_XREFS = False
XREF_STATS = [0, 0, 0, 0]       # resolved, sibling missing, refused, adopted-uncached
RESOLVE_UNCACHED = False


def no_published_description(c):
    """True when a verdict on this course would rest on nothing published."""
    d = (c.get("description") or "").strip()
    return (not d) or bool(XREF_ONLY.match(d))


def resolve_xrefs(school, cache, sig):
    """A stub adopts its sibling's description, but only when the resolved text
    is already classified. Mutates the school's courses in place."""
    courses = school.get("courses") or []
    by_code = {}
    for c in courses:
        k = __import__("re").sub(r"\s+", " ", (c.get("code") or "").strip().upper())
        if k:
            by_code.setdefault(k, []).append(c)
    for c in courses:
        d = (c.get("description") or "").strip()
        if not d or not XREF_ONLY.match(d):
            continue
        m = XREF_TARGET.search(d)
        if not m:
            continue
        tgt = f"{m.group(1).strip().upper()} {m.group(2).upper()}"
        sib = next((x for x in by_code.get(tgt, [])
                    if len((x.get("description") or "").strip()) > 80), None)
        if not sib:
            XREF_STATS[1] += 1
            continue
        trial = dict(c)
        trial["description"] = sib["description"]
        if cache_key(course_text(trial), sig) not in cache:
            if not RESOLVE_UNCACHED:
                XREF_STATS[2] += 1
                continue
            XREF_STATS[3] += 1
        c["description"] = sib["description"]
        c["description_source"] = "cross_reference"
        c["description_from"] = tgt
        XREF_STATS[0] += 1


def in_art(c, art):
    return (c.get("subject") or "").strip().upper() in art


def n_distinct(courses, title_only=False):
    """Distinct courses under the rule. Identical to harvest.n_deduped."""
    keys, blank = set(), 0
    for c in courses:
        k = dedupe_key(c)
        if not k[0]:
            blank += 1
        else:
            keys.add(k[0] if title_only else k)
    return len(keys) + blank


def collapse(courses, art=None):
    """-> [(representative, members)] in catalog order of first appearance.

    When any member is in the art set the representative is an art member, so
    a group counts in the art unit exactly when one of its records does. That
    keeps every art numerator a subset of its denominator by construction.
    """
    groups, order = {}, []
    for i, c in enumerate(courses):
        k = dedupe_key(c)
        if not k[0]:
            k = ("", "", i)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(c)
    out = []
    for k in order:
        members = groups[k]
        rep = members[0]
        if art:
            arts = [m for m in members if in_art(m, art)]
            if arts:
                rep = arts[0]
        out.append((rep, members))
    return out


def prefilter(courses, matchers):
    """-> {axis: [course, ...]} plus the union, deduplicated."""
    hits = defaultdict(list)
    shortlist, seen = [], set()
    for c in courses:
        text = course_text(c)
        matched = [a for a, rx in matchers.items() if rx.search(text)]
        if not matched:
            continue
        c = dict(c, _candidate_axes=matched)
        for a in matched:
            hits[a].append(c)
        key = c.get("code", "") + "|" + text[:80]
        if key not in seen:
            seen.add(key)
            shortlist.append(c)
    return hits, shortlist


# ---------------------------------------------------------------- stage two

# The classifier prompt reads name, question, includes and excludes. It never
# sees the lexicon. Keying the cache on rubric_version therefore threw away paid
# classifications whose prompts were byte-identical, every time a lexicon term
# changed — and lexicon iteration is exactly what the carry report produces.
#
# Key on the prompt-affecting subset instead. rubric_version rides along as
# metadata so every cached row still records which rubric produced it.
PROMPT_FIELDS = ("name", "question", "includes", "excludes")


def prompt_signature(rubric, model):
    """Hash of the prompt the model actually receives, minus the courses.

    V17. Through V16 this hashed a chosen SUBSET of rubric fields, so the code
    scaffolding around them was invisible to it: build_prompt still told the
    model the valid names were "technology, venture, self, audience" on a
    five-axis rubric, and no signature ever changed to say so. Hashing the
    rendered template makes every prompt-affecting change — rubric or code —
    invalidate the cache, by construction rather than by remembering to list it.
    The lexicon is not in the prompt, so a lexicon edit still costs nothing.
    """
    blob = build_prompt(rubric, [])
    return hashlib.sha256(f"{model}|{blob}".encode()).hexdigest()[:16]


def cache_key(text, prompt_sig):
    """prompt_sig comes from prompt_signature(). Not the rubric version."""
    return hashlib.sha256(f"{prompt_sig}|{text}".encode()).hexdigest()[:32]


def load_cache(path):
    cache = {}
    if not os.path.exists(path):
        return cache
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
                cache[r["key"]] = r["axes"]
            except Exception:
                continue
    return cache


def append_cache(path, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def build_prompt(rubric, batch):
    axes = []
    for axis, spec in rubric["axes"].items():
        axes.append(
            f"{axis} — {spec['name']}\n"
            f"  Question: {spec['question']}\n"
            f"  Counts: {'; '.join(spec['includes'])}\n"
            f"  Does not count: {'; '.join(spec['excludes'])}")
    courses = []
    for i, c in enumerate(batch):
        desc = (c.get("description") or "")[:900]
        courses.append(f"[{i}] {c.get('code','')} {c.get('title','')}\n{desc}")

    # The decision rule lives in the rubric, where it is versioned, diffable and
    # part of the prompt signature. Changing it correctly invalidates the cache.
    instruction = (rubric.get("classifier", {}).get("instruction") or
                   "You are classifying university course descriptions for a "
                   "study of art and design curricula.\n\n"
                   f"For each course, decide which of these {len(rubric['axes'])} "
                   "categories it "
                   "belongs to. A course may belong to none, one, or several. "
                   "Judge what the course actually teaches, not what words "
                   "appear in it.")
    return (
        instruction + "\n\n"
        + "\n\n".join(axes) +
        "\n\nCOURSES:\n\n" + "\n\n".join(courses) +
        "\n\nRespond with JSON only, no preamble, no code fences. An object whose "
        "keys are the bracketed indices as strings and whose values are arrays of "
        f"matching category names ({', '.join(rubric['axes'])}). Use an "
        'empty array for none. Example: {"0": ["venture"], "1": [], "2": '
        '["technology", "audience"]}')


class AuthError(Exception):
    """401/403. A credential failure is a property of the RUN, not the batch.

    Retrying it burns a backoff sleep per batch and, unattended, produces 3,600
    doomed calls and a score set where every school reads
    classification_coverage 0.0. The floor catches that, so nothing false gets
    published — but the hours are gone.
    """


def call_api(prompt, model, api_key, max_retries=4):
    body = json.dumps({
        "model": model,
        # 2000 truncated the response once v0.7 lengthened the instruction:
        # four of five schools lost a 20-course batch to unparseable JSON, which
        # is 80 courses silently missing and a school sitting at 95% coverage,
        # above the floor and quietly depressed.
        "max_tokens": 8000,
        # SCORE_V36_OPUS: some models reject `temperature` outright
        # ("`temperature` is deprecated for this model", HTTP 400). Send it
        # only where it is accepted; the pinned model's request is unchanged.
        **({} if str(model).startswith(("claude-opus-5", "claude-mythos", "claude-fable"))
           else {"temperature": 0}),
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(API_URL, data=body, headers={
        "content-type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    })
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read())
            return "".join(b.get("text", "") for b in d.get("content", [])
                           if b.get("type") == "text")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError(f"HTTP {e.code}: check ANTHROPIC_API_KEY") from e
            if e.code in (429, 500, 502, 503, 529) and attempt < max_retries - 1:
                time.sleep(4 * (attempt + 1))
                continue
            raise
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(4 * (attempt + 1))
                continue
            raise
    return ""


def parse_response(text, n, valid_axes):
    """Never trust the shape. Anything unparseable classifies as nothing."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    out = []
    for i in range(n):
        v = d.get(str(i), [])
        if not isinstance(v, list):
            v = []
        out.append([a for a in v if a in valid_axes])
    return out


def classify(shortlist, rubric, api_key, cache_path, model, batch_size,
             dry=False, workers=1):
    """-> {course_key: [axes]}. Cached results are free and deterministic."""
    version = rubric["rubric_version"]
    sig = prompt_signature(rubric, model)
    cache = load_cache(cache_path)
    results, pending = {}, []

    for c in shortlist:
        text = course_text(c)
        k = cache_key(text, sig)
        if k in cache:
            results[k] = cache[k]
        else:
            pending.append((k, c, text))

    log(f"  {len(results)} cached, {len(pending)} to classify")
    if dry or not pending:
        return results, cache, 0

    if not api_key:
        log("  ANTHROPIC_API_KEY not set — cannot classify. Run with --dry-run "
            "to see prefilter counts, or export the key.")
        return results, cache, len(pending)

    valid = set(rubric["axes"])
    batches = [pending[i:i + batch_size]
               for i in range(0, len(pending), batch_size)]
    fresh, skipped = [], 0
    lock = threading.Lock()
    done = [0]

    def run(idx_batch):
        idx, batch = idx_batch
        prompt = build_prompt(rubric, [b[1] for b in batch])
        raw = call_api(prompt, model, api_key)      # AuthError propagates
        parsed = parse_response(raw, len(batch), valid)
        if parsed is not None:
            return idx, batch, parsed, ""

        # Retry one course at a time. A single unparseable course should not
        # take nineteen others with it.
        recovered, lost = [], 0
        for k, c, text in batch:
            try:
                one = parse_response(
                    call_api(build_prompt(rubric, [c]), model, api_key), 1, valid)
            except AuthError:
                raise
            except Exception:
                one = None
            if one is None:
                lost += 1
                recovered.append([])
            else:
                recovered.append(one[0])
        return idx, batch, recovered, (raw or "")[:200] if lost else ""

    log(f"  {len(batches)} batches, {workers} worker(s)")
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(run, (i, b)): i for i, b in enumerate(batches)}
            for f in as_completed(futs):
                i = futs[f]
                try:
                    _, batch, parsed, raw_sample = f.result()
                except AuthError:
                    raise
                except Exception as e:
                    log(f"  batch {i} failed: {type(e).__name__}: {e}")
                    with lock:
                        skipped += len(batches[i])
                    continue
                if raw_sample:
                    log(f"  batch {i} needed single-course retry. Raw response "
                        f"began:")
                    log(f"    {raw_sample!r}")
                if parsed is None:
                    log(f"  batch {i} returned unparseable JSON; skipped")
                    with lock:
                        skipped += len(batch)
                    continue
                with lock:
                    for (k, c, text), axes in zip(batch, parsed):
                        results[k] = axes
                        fresh.append({"key": k, "axes": axes,
                                      "code": c.get("code", ""), "model": model,
                                      "rubric_version": version,
                                      "prompt_sig": sig})
                    done[0] += 1
                    if len(fresh) >= 200:
                        append_cache(cache_path, fresh)
                        fresh = []
                    if done[0] % 20 == 0:
                        log(f"  classified {done[0] * batch_size}/{len(pending)}")
    except AuthError as e:
        if fresh:
            append_cache(cache_path, fresh)
        log(f"\n  ABORTING: {e}")
        log("  A credential failure is a property of the run, not the batch. "
            "Nothing further would succeed.")
        raise
    if fresh:
        append_cache(cache_path, fresh)
    if skipped:
        log(f"  WARNING: {skipped} courses fell out of classification. They "
            f"score as matching nothing, which depresses this school invisibly.")
    return results, cache, skipped


# ---------------------------------------------------------------- aggregate

def score_from_share(share, thresholds):
    """0-4, by how many published thresholds the share clears."""
    return sum(1 for t in thresholds if share >= t)


def harvest_plausible(n_courses, completions, corpus_median_ratio):
    """-> (implausible: bool, reason: str).

    Sacramento harvested 7 courses, UC-Irvine 13, Miami 3. Every one cleared the
    coverage floors, because description coverage on seven courses is trivially
    100%. They are failed harvests wearing the costume of small schools, and left
    in they would help define the quartiles that score RISD.

    The rule is derived from the corpus rather than invented: courses per degree
    conferred, against the corpus median. A school an order of magnitude below
    the median did not have its catalog read.
    """
    if not completions or corpus_median_ratio <= 0 or HARVEST_RATIO_FLOOR <= 0:
        return (n_courses < MIN_PLAUSIBLE_COURSES,
                f"only {n_courses} courses harvested" if
                n_courses < MIN_PLAUSIBLE_COURSES else "")
    ratio = n_courses / completions
    if ratio < corpus_median_ratio * HARVEST_RATIO_FLOOR:
        return True, (f"{n_courses} courses against {completions} degrees "
                      f"conferred is {ratio:.2f} per degree, below "
                      f"{HARVEST_RATIO_FLOOR:.0%} of the corpus median "
                      f"{corpus_median_ratio:.2f} — the catalog was not read")
    if n_courses < MIN_PLAUSIBLE_COURSES:
        return True, f"only {n_courses} courses harvested"
    return False, ""


def floor_check(description_coverage, classification_coverage):
    """-> (unranked: bool, reason: str). Enforced, not advisory."""
    reasons = []
    if description_coverage < COVERAGE_FLOOR:
        reasons.append(f"description coverage {description_coverage:.0%} "
                       f"below the {COVERAGE_FLOOR:.0%} floor")
    if classification_coverage < COVERAGE_FLOOR:
        reasons.append(f"classification coverage {classification_coverage:.0%} "
                       f"below the {COVERAGE_FLOOR:.0%} floor")
    return bool(reasons), "; ".join(reasons)


# v0.9 sampling rule. Through V19 a prefix was judged on its FIRST five titles
# in catalog order: Emerson's VM, 150 courses of visual and media arts, was
# excluded on five that happened to be media studies. Now: a random sample of
# DISTINCT titles (CalArts' 41 Independent Study shells are one title), sized
# with the prefix, seeded by school and prefix so a rerun draws the same titles.
SAMPLE_MIN, SAMPLE_MAX = 10, 30
SAMPLE_RULE = ("seeded random sample of distinct titles: all titles up to 10, "
               "then ceil(2*sqrt(distinct titles)), capped at 30; "
               "seed = sha256(unitid|prefix)")


def sample_size(n_titles):
    return min(n_titles, max(SAMPLE_MIN, math.ceil(2 * math.sqrt(n_titles))),
               SAMPLE_MAX)


def subject_rows(school, samples=None):
    """-> [{subject, n, samples, n_sampled, n_titles}], largest first.

    samples=None applies SAMPLE_RULE; an int forces a fixed size (tests)."""
    per, titles = Counter(), defaultdict(set)
    for c in school.get("courses") or []:
        subj = (c.get("subject") or "").strip().upper()
        if not subj:
            continue
        per[subj] += 1
        t = (c.get("title") or "").strip()
        if t:
            titles[subj].add(t[:70])
    out = []
    for k, n in per.most_common():
        pool = sorted(titles[k])
        size = sample_size(len(pool)) if samples is None else min(samples, len(pool))
        seed = int(hashlib.sha256(f"{school.get('unitid')}|{k}".encode())
                   .hexdigest()[:16], 16)
        pick = sorted(random.Random(seed).sample(pool, size))
        out.append({"subject": k, "n": n, "samples": pick,
                    "n_sampled": size, "n_titles": len(pool)})
    return out


def subject_blob(rows):
    """The subject cache key's content: prefix, count AND the sampled titles.
    Through V19 the titles were not in it, so a sampling change would have
    reused verdicts made on the old titles — Emerson's VM would have stayed
    out from cache."""
    return json.dumps([[r["subject"], r["n"], r["samples"]] for r in rows],
                      sort_keys=True, ensure_ascii=False)


# The rubric owns the words; the roster owns the scope. Families outside the
# roster's CIP selection are named mechanically so no rubric omission can let
# them back in: v0.8's list dropped arts management (50.10), which every prompt
# since V8 had named. Titles verified against the NCES CIP series-50 listing.
CIP50_OUT_OF_SCOPE = ["50.03 Dance", "50.05 Drama/Theatre Arts and Stagecraft "
                      "(including theatre design and technology, 50.0502)",
                      "50.09 Music",
                      "50.10 Arts, Entertainment and Media Management"]
SUBJECT_STATUSES = ("art", "extension", "administrative", "out")


# v0.11 subject_scope.scope_change_implementation: "A scope change must never be
# made by editing the subject prompt." Its architecture entry nevertheless sits
# in excluded_families, which IS the prompt — rendering it changes the subject
# signature and re-rolls every verdict in the corpus. So an entry naming a CIP
# family outside the roster's selection is held OUT of the prompt and asked as a
# narrow follow-up of prefixes already judged "art". The prompt stays
# byte-identical to v0.9/v0.10 and no existing verdict can move.
FOLLOWUP_RE = re.compile(r"\bCIP\s*0?4\b", re.I)


def split_scope(scope):
    """-> (prompt_scope, [follow-up entries]) for a rubric that still carries a
    CIP-04 rule inside excluded_families (v0.11). v0.12 moved these into
    post_classification_scope, so this returns nothing to follow there — it
    stays as a guard, because a scope rule must never reach the prompt."""
    fams = list((scope or {}).get("excluded_families") or [])
    follow = [x for x in fams if FOLLOWUP_RE.search(x)]
    if not follow:
        return scope, []
    trimmed = dict(scope)
    trimmed["excluded_families"] = [x for x in fams if x not in follow]
    return trimmed, follow


def followup_rules(rubric):
    """-> [(key, direction, text)] from post_classification_scope, plus any
    legacy entry still sitting in excluded_families.

    direction: "exclude" asks prefixes currently judged art and removes them;
    "include" asks prefixes currently judged out and admits them. The key is
    read from the rule's own name — architecture_out, film_media_in — so the
    rubric states the direction and the code never guesses it."""
    out = []
    for k, v in ((rubric or {}).get("post_classification_scope") or {}).items():
        if k in ("how",) or k.startswith("open_item") or not isinstance(v, str):
            continue
        if k.endswith("_in"):
            out.append((k, "include", v))
        elif k.endswith("_out"):
            out.append((k, "exclude", v))
        else:
            log(f"  post_classification_scope.{k} names no direction, so it is "
                f"NOT executed as a follow-up — which is correct for a rule "
                f"implemented in code or held. Add an _in or _out suffix ONLY "
                f"if a model should be asked this about every prefix.")
    for entry in split_scope((rubric or {}).get("subject_scope"))[1]:
        out.append(("excluded_families_cip04", "exclude", entry))
    return out


# v0.12 film_media_in: "carry the same vetoes here". A prefix whose titles say
# journalism, broadcast, theatre or acting is never asked, so the rule cannot
# admit a communication department by a word in a title.
SCOPE_VETO = ("journalism", "news writing", "reporting", "public relations",
              "advertising campaign", "acting", "theatre", "theater", "playwriting",
              "scene study", "stagecraft", "broadcast news", "voice and diction",
              "announcing", "radio station", "sports media")
# "dissertation" was here until v0.43. It is a graduate-catalogue word in a list
# of family words, and it vetoed 34 prefixes including UCSB CH and Binghamton
# ENG — neither of which is a film question.
SHAPE_HINTS = {
    "film": ("film", "cinema", "video", "animation", "moving image", "screenwriting",
             "documentary", "cinematography", "post production", "editing"),
    # v0.16 arts_business_under_education_in. Without a shape the rule asks about
    # every out-prefix at every school; with it, only prefixes whose titles carry
    # art-education or arts-business wording. JMU's ARTE sample — Leadership and
    # Community Engagement, Business and Marketing, Ethics and Law in the Arts —
    # matches on the arts-business side even with no teacher-prep course in it.
    "education": ("art education", "teaching", "teacher", "curriculum", "classroom",
                  "pedagog", "student teaching", "arts integration", "k-12",
                  "arts business", "arts management", "arts leadership",
                  "arts entrepreneur", "in the arts", "community engagement",
                  "business and marketing", "ethics and law"),
    # v0.43 design_schools_in. craft is word-bounded: as a substring it matches
    # Aircraft, Spacecraft and Statecraft, the artefact recorded in
    # maker_rule.artefact_record. "making" is deliberately absent — it matches
    # Decision Making, and filmmaking is the film gate's business.
    # "studio" is kept despite its breadth: it will ask recording, dance and
    # television studio prefixes and the majority test will decline them. It is
    # the one word here that might reach a design department not named design,
    # which is the blind spot's whole problem.
    "design": ("design", "typography", "visual communication", "industrial design",
               "human centered", "human-centered", "user experience", "interaction",
               "graphic", "branding", "product design", "service design",
               "wayfinding", "prototyp", r"\bcraft\b"),
    "fashion": ("fashion", "apparel", "textile", "pattern", "drap", "garment",
                "sewn", "knitwear", "millinery", "couture", "merchandis",
                "fibers", "fiber", "dress"),
}

# How many distinct keywords a prefix must carry to be ASKED. Not a constant:
# it is a property of how common the family's vocabulary is outside the family.
# film stays at 2 — at 1 it asks 891 prefixes and 48,666 courses, because film,
# video and editing appear throughout English, history and communications.
# design and fashion are narrower words: at 1 they ask 3,688 and 250. At 2,
# DePaul DES (100 courses) and Arkansas ARDS are excluded from the rule written
# for them. Any future shape sets its own threshold on that test and publishes
# the reasoning.

# ---------------------------------------------------------------------------
# Family guard. The rubric says family exclusions apply first and absolutely.
# An include follow-up examines prefixes that are already OUT, and an exclude
# follow-up examines only prefixes that are already IN — so nothing in the
# pipeline re-checks a prefix an include rule has just admitted. v0.43 admitted
# 2,544 architecture courses, 1,900 music courses, 1,180 landscape architecture
# and 633 dance that way.
#
# This refuses an admission whose sampled titles are substantially an excluded
# family, whatever the model answered. It never removes a prefix that was
# already in; it only declines to add one.
FAMILY_GUARD_SHARE = 0.30
FAMILY_GUARD = {
    "architecture": __import__("re").compile(
        r"architect|landscape arch|urban design|urban plan|site design|"
        r"building (systems|technology|science)|structures|construction (documents|"
        r"management)|historic preservation", __import__("re").I),
    "performing arts": __import__("re").compile(
        r"music|dance|theat(re|er)|opera|choir|orchestr|ballet|acting|vocal|"
        r"percussion|piano|conduct(ing)?\b|drama|recital|ensemble|repertoire|"
        r"choreograph|scene design|sound design|stagecraft|costume", __import__("re").I),
}


def family_refusal(samples):
    """-> (family, share) if an excluded family dominates the sample, else None."""
    rows = [s.lower() for s in (samples or [])]
    if not rows:
        return None
    worst = None
    for fam, rx in FAMILY_GUARD.items():
        share = sum(1 for s in rows if rx.search(s)) / len(rows)
        if share >= FAMILY_GUARD_SHARE and (worst is None or share > worst[1]):
            worst = (fam, round(share, 2))
    return worst

SHAPE_NEED = {"film": 2, "education": 1, "design": 1, "fashion": 1}

# A veto word must appear in at least this share of a prefix's sampled titles to
# disqualify it. As a single occurrence it killed UCSB FAMST — 630 courses of
# film, one theatre title in ten — and DePaul DES and HCD. Readmission is flat
# above this cut: 69 prefixes at 0.20, 78 at 0.30, 81 at 0.40, 82 at 0.50.
VETO_SHARE = 0.30


def shape_matches(samples, entry):
    """Does this prefix look like the family the rule is about, and survive the
    boundary wording? Titles only."""
    import re as _re
    rows = [s.lower() for s in (samples or [])]
    t = " | ".join(rows)
    # Proportional, not binary: one theatre title among ten no longer kills a
    # film department. Counted across ALL veto words together, not per word — a
    # theatre prefix spreads acting, stagecraft and playwriting across different
    # titles, and per-word shares would each sit under the cut while eight of
    # ten titles are plainly theatre.
    if rows:
        hit = sum(1 for s in rows if any(v in s for v in SCOPE_VETO))
        if hit / len(rows) >= VETO_SHARE:
            return False
    e = (entry or "").lower()
    # Every hint the rule's text names, not the first one found. "fashion_design_in"
    # contains both "design" and "fashion"; dict order made it design-only, so a
    # pattern-drafting prefix would have been gated out of the fashion rule by the
    # design vocabulary. A rule passes if ANY of its named shapes clears that
    # shape's own threshold.
    named = [(n, w) for n, w in SHAPE_HINTS.items() if n in e]
    if not named:
        return True
    for name, words in named:
        hits = sum(1 for w in words
                   if (_re.search(w, t) if w.startswith("\\b") else w in t))
        if hits >= SHAPE_NEED.get(name, 2):
            return True
    return False


def scope_signature(model, entry, direction="exclude"):
    """Hash of the RENDERED prompt, not the rule text. V26 tightened the wording
    and nothing re-asked: this hashed the entry alone, so the cache key never
    moved and 1,300 stale verdicts came back. Same defect as the V17 course
    signature, in the one prompt that had not been converted."""
    blob = scope_prompt("__TEMPLATE__", [], entry, direction)
    return cache_key(blob, hashlib.sha256(f"scope|{model}".encode()).hexdigest()[:16])


def scope_prompt(school_name, rows, entry, direction="exclude"):
    listing = "\n".join(
        f"{r['subject']}  ({r['n']} courses)\n    " + "\n    ".join(r["samples"])
        for r in rows)
    if direction == "include":
        head = (f"These subject prefixes at {school_name} were classified as NOT "
                f"art and design subjects. One family must now be admitted to that "
                f"set:")
        # V27. Without a majority rule the include side admitted whole
        # departments for a few matching titles: Berkeley CHICAN and ASAMST,
        # Hawaii LLEA, Wesleyan CEAS, Iowa State WFS — ethnic studies, languages
        # and area studies that teach some film.
        ask = ('For each prefix below, answer "include" only if MOST of its '
               'courses belong to that family — the prefix IS that department — '
               'or "keep_out" if it does not. Judge by what the courses teach, '
               'never by a word in a title.\n\n'
               'KEEP OUT, even where some titles mention film, media or cinema: '
               'ethnic, area and cultural studies; languages and literature; '
               'English and comparative literature; history; journalism, '
               'communication and broadcasting; theatre and performance; '
               'education. A department that teaches a few film courses is not a '
               'film department.')
        fmt = ('an object whose keys are the prefixes and whose values are '
               '"include" or "keep_out". Example: {"FILM": "include", '
               '"MFJS": "keep_out"}')
    else:
        head = (f"These subject prefixes at {school_name} were classified as art "
                f"and design subjects. One family must now be removed from that "
                f"set:")
        # V26. The first run excluded any prefix whose titles MENTIONED
        # architecture or CAD: Utah Valley's ARTH (art history), Belmont's CAD
        # (design history) and TCU's DEMT (fashion design) were all removed.
        # The rubric's test is what the courses teach, so the majority rule and
        # the in-scope families are stated explicitly.
        ask = ('For each prefix below, answer "exclude" only if MOST of its '
               'courses belong to that family — the prefix IS that department — '
               'or "keep" if it does not. Judge by what the courses teach, never '
               'by a word in a title.\n\n'
               'KEEP, even where some titles mention architecture, buildings, '
               'space or CAD: art history and architectural history; fashion, '
               'textile and apparel design; interior design and interior '
               'architecture; graphic, communication and product design; studio '
               'art; film and photography; foundation and design-thinking '
               'courses. These are art and design departments whose subject '
               'matter sometimes includes buildings.')
        fmt = ('an object whose keys are the prefixes and whose values are '
               '"exclude" or "keep". Example: {"ARCH": "exclude", "IDES": "keep"}')
    return (f"{head}\n\n{entry}\n\n{ask}\n\n" + listing +
            "\n\nRespond with JSON only, no preamble, no code fences: " + fmt)


def parse_scope_response(text, valid):
    """-> {prefix: "exclude"|"keep"}. Its own parser: parse_subject_response
    coerces every value into the art/extension/administrative/out vocabulary,
    which turned "exclude" into "out" and silently removed nothing."""
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    def norm(v):
        t = str(v).strip().lower()
        if t.startswith("ex"):
            return "exclude"
        if t.startswith("in"):
            return "include"
        return "keep"
    return {k: norm(v) for k, v in d.items() if k in valid}


def run_classify_scope(a, model, rubric):
    """The narrow follow-up. Reads data/subjects/, asks only about prefixes whose
    status is 'art', caches per prefix, and rewrites the art set. The course
    classifications are untouched, so re-aggregation is free."""
    rules = followup_rules(rubric)
    if not rules:
        log("  no post-classification scope rules in this rubric; nothing to do")
        return
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    cache = load_cache(a.subject_cache)
    files = sorted(f for f in os.listdir(a.subject_dir) if f.endswith(".json"))
    if a.schools:
        want = {x.strip() for x in a.schools.split(",") if x.strip()}
        files = [f for f in files if f[:-5] in want]
    fresh, changed, asked = [], 0, 0
    # load_cache returns {cache_key: axes}, so the signature is not in the loaded
    # map — it is a field on each raw row. Read the file. An unreadable or absent
    # cache yields an empty set, which disables the guard rather than blocking a
    # run: the guard exists to prevent an unpriced corpus-wide pass, not to stop
    # work when it cannot see the cache.
    known = set()
    _cp = getattr(a, "subject_cache", "") or ""
    if _cp and os.path.exists(_cp):
        try:
            with open(_cp, encoding="utf-8") as _fh:
                for _line in _fh:
                    _line = _line.strip()
                    if not _line:
                        continue
                    try:
                        _r = json.loads(_line)
                    except Exception:
                        continue
                    _s = _r.get("prompt_sig") or _r.get("prompt_signature")
                    if _s:
                        known.add(str(_s))
        except Exception:
            known = set()
    for key, direction, entry in rules:
        sig = scope_signature(model, entry, direction)
        log(f"  {key} ({direction}), prompt-sig {sig}")
        # A signature nobody has seen means every prefix in the corpus is about
        # to be asked. That is never something to discover from an invoice.
        if known and sig not in known and not getattr(a, "allow_new_followup", False):
            log(f"  REFUSED {key}: prompt-sig {sig} is not in the subject cache, "
                f"so this rule would ask the whole corpus. If that is intended, "
                f"pass --allow-new-followup and price it first.")
            continue
        for fn in files:
            path = os.path.join(a.subject_dir, fn)
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
            want = "art" if direction == "exclude" else "out"
            rows = [x for x in rec.get("art_subjects") or []
                    if _status(x.get("status") or ("art" if x.get("included") else "out"))
                    == want and "samples" in x]
            # An override is the rubric deciding a prefix by hand. The
            # follow-up must not overturn it: SCAD's ELDS was set to art by the
            # v0.13 override and the architecture rule removed it again on the
            # next pass, so the override looked applied and had no effect.
            fixed = set((rec.get("overrides_applied") or {}).keys())
            if fixed:
                rows = [r for r in rows if r["subject"] not in fixed]
            if direction == "include":
                # Shape gate first, then the family guard: the rubric says family
                # exclusions apply first and absolutely, and nothing else in the
                # pipeline re-checks a prefix an include rule has just admitted.
                kept = []
                for r in rows:
                    if not shape_matches(r.get("samples"), entry):
                        continue
                    ref = family_refusal(r.get("samples"))
                    if ref:
                        log(f"  GUARD refused {r.get('subject')} at "
                            f"{rec.get('name','')[:28]}: {ref[0]} {ref[1]:.0%} "
                            f"of sampled titles — family exclusions apply first")
                        continue
                    kept.append(r)
                rows = kept
            if not rows:
                continue
            verdict, pending = {}, []
            for r in rows:
                k = prefix_key(rec.get("name", ""), r, sig)
                v = cache.get(k)
                if v is None:
                    pending.append(r)
                else:
                    verdict[r["subject"]] = v
            if pending and not a.dry_run:
                if not api_key:
                    log("  ANTHROPIC_API_KEY not set — cannot run the scope pass")
                    return
                for i in range(0, len(pending), SUBJECT_CHUNK):
                    chunk = pending[i:i + SUBJECT_CHUNK]
                    try:
                        raw = call_api(scope_prompt(rec.get("name", ""), chunk,
                                                    entry, direction),
                                       model, api_key)
                    except AuthError:
                        raise
                    except Exception as e:
                        log(f"  {rec.get('name', fn)[:34]}: {type(e).__name__}: {e}")
                        continue
                    part = parse_scope_response(raw, {r["subject"] for r in chunk})
                    if part is None:
                        log(f"  {rec.get('name', fn)[:34]}: unparseable scope answer")
                        continue
                    for r in chunk:
                        v = part.get(r["subject"])
                        if v is None:
                            continue
                        verdict[r["subject"]] = v
                        asked += 1
                        k = prefix_key(rec.get("name", ""), r, sig)
                        cache[k] = v
                        fresh.append({"key": k, "axes": v, "code": rec.get("unitid"),
                                      "prefix": r["subject"], "model": model,
                                      "rubric_version": "scope", "prompt_sig": sig})
            elif pending and a.dry_run:
                log(f"  would ask {rec.get('name', fn)[:36]:<36} about "
                    f"{len(pending)} of {len(rows)} {want} prefixes")
                continue
            hit = [r["subject"] for r in rows if verdict.get(r["subject"]) == direction]
            if not hit:
                continue
            for x in rec["art_subjects"]:
                if x["subject"] not in hit:
                    continue
                if direction == "exclude":
                    x.update(included=False, status="out", scope_excluded=True,
                             scope_rule=key)
                else:
                    x.update(included=True, status="art", scope_included=True,
                             scope_rule=key)
            fld = "scope_excluded" if direction == "exclude" else "scope_included"
            rec[fld] = sorted(set((rec.get(fld) or []) + hit))
            n_art = sum(x["n"] for x in rec["art_subjects"] if x.get("included"))
            n_courses = rec.get("n_courses") or 0
            share = round(n_art / n_courses, 4) if n_courses else 0.0
            bad, reason, review = art_share_check(
                share, n_art, n_courses, len(rec.get("art_subjects") or []) or None)
            rec.update({"n_art_courses": n_art, "art_share_of_catalog": share,
                        "implausible": bad, "implausible_reason": reason,
                        "review_flags": review})
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2, ensure_ascii=False)
            changed += 1
            verb = "removed" if direction == "exclude" else "admitted"
            log(f"  {rec.get('name', fn)[:36]:<36} {verb} {'/'.join(hit)}; art unit "
                f"{n_art} courses{'   IMPLAUSIBLE — ' + reason if bad else ''}")
    if fresh and not a.dry_run:
        append_cache(a.subject_cache, fresh)
    log(f"\n  {changed} schools changed, {asked} prefix verdicts asked"
        + ("  [--dry-run: nothing written]" if a.dry_run else ""))


def _subject_scope_text(scope):
    if not scope:
        return ("\n\nEXCLUDED, without exception, EVEN AT a conservatory or an art "
        "school that teaches them well:\n"
        "  - performing arts: music (CIP 50.09), theatre and drama (50.05), "
        "dance (50.03), arts administration (50.10), creative writing\n"
        "  - building and industrial trades: construction and carpentry (46), "
        "electrical, HVAC and mechanic trades (47), welding and precision "
        "production (48)\n"
        "  - agriculture, horticulture and natural resources (01, 03)\n"
        "  - biological and life sciences (26)\n"
        "  - communication and journalism (09): journalism, public relations, "
        "speech, media studies\n"
        "  - family and consumer sciences (19)\n"
        "  - general liberal arts\n"
        "  - continuing education, extension, pre-college and non-credit "
        "programs, whatever their subject\n"
        "A prefix whose sample titles are acting, audition, ensemble, repertory, "
        "choreography, dance technique, partnering, musicianship, composition "
        "for performers, welding, plumbing, wiring, crop or plant science is NOT "
        "in scope.\n\n"
        "IN scope despite the neighbours above: communication design, graphic "
        "design and visual communication (50.04); scientific and medical "
        "illustration (50.04); film, video and photography (50.06).\n\n"
        "Include a prefix only when most of its sample titles are art and "
        "design courses.\n\n"
        )
    out = ["\n\nEXCLUDED, without exception, EVEN AT a conservatory or an art "
           "school that teaches them well:"]
    fams = scope.get("excluded_families") or []
    # An entry that begins NOTE is guidance, not an exclusion. v0.9's advertising
    # NOTE says IN SCOPE; printed as an EXCLUDED bullet it contradicts itself.
    out += [f"  - {x}" for x in fams if not x.lstrip().upper().startswith("NOTE")]
    out.append("  - every CIP 50 family outside the list above: "
               + "; ".join(CIP50_OUT_OF_SCOPE))
    out += [f"\n{x}" for x in fams if x.lstrip().upper().startswith("NOTE")]
    for key, head in (("protected", "IN SCOPE despite the neighbours above"),
                      ("excluded_delivery", "DELIVERY, NOT DISCIPLINE"),
                      ("not_subjects", "NOT SUBJECT CODES"),
                      ("positive_rule", "RULE")):
        if scope.get(key):
            out.append(f"\n{head}: {scope[key]}")
    return "\n".join(out) + "\n\n"


def subject_prompt(school_name, rows, scope=None):
    """scope: the rubric's subject_scope block, rendered verbatim. The in-scope
    CIP list always comes from CIP_SCOPE — the roster's own selection — never
    from rubric prose (v0.8's prose mislabels 50.13)."""
    listing = "\n".join(
        f"{r['subject']}  ({r['n']} courses; "
        f"{r.get('n_sampled', len(r['samples']))} of "
        f"{r.get('n_titles', len(r['samples']))} distinct titles, "
        f"sampled at random)\n    " +
        "\n    ".join(r["samples"]) for r in rows)
    fmt = ('an object whose keys are the prefixes and whose values are exactly '
           'one of: "art" (an art and design discipline in scope), "extension" '
           '(continuing education, extension, pre-college, summer intensive or '
           'non-credit), "administrative" (a campus, location or administrative '
           'code, not a discipline), "out" (any other discipline, including every '
           'excluded family). Example: {"ILLUS": "art", "HPSS": "out", '
           '"XYTH": "extension", "TUJ": "administrative"}')
    return (
        f"These are the subject prefixes used in the course catalog of "
        f"{school_name}, with course counts and sample titles.\n\n"
        "Decide which prefixes are ART AND DESIGN subjects, meaning they fall "
        "under these federal CIP families:\n\n"
        + "\n".join("  " + c for c in CIP_SCOPE)
        + _subject_scope_text(scope) +
        "Judge from the sample titles, not from what the letters look like — "
        "prefixes are local and opaque."
        "\n\n" + listing +
        "\n\nRespond with JSON only, no preamble, no code fences: " + fmt)


def subject_signature(model, scope=None):
    """Hash of the rendered template, so a scope edit re-classifies."""
    blob = subject_prompt("__TEMPLATE__", [], scope)
    return hashlib.sha256(f"{model}|{blob}".encode()).hexdigest()[:16]


def _status(v):
    if v is True:
        return "art"
    if v is False or v is None:
        return "out"
    if isinstance(v, (int, float)):
        return "art" if v else "out"
    t = str(v).strip().lower()
    if t in ("art", "in", "true", "yes"):
        return "art"
    if t in ("extension", "pre-college", "precollege", "continuing", "non-credit"):
        return "extension"
    if t in ("administrative", "campus", "location", "admin"):
        return "administrative"
    return "out"


def parse_subject_response(text, valid):
    """-> {prefix: status}, status in SUBJECT_STATUSES. Bools still accepted."""
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    return {k: _status(v) for k, v in d.items() if k in valid}


def is_art(v):
    return _status(v) == "art"


def art_share_check(art_share, n_art, n_courses, n_prefixes=None):
    """-> (held_back: bool, published_reason: str, review_flags: [str]).

    v0.13 unranked_reasons: a published reason must be mechanically true of that
    school and checkable by its dean. The art-share heuristic is a REVIEW
    TRIGGER, not a reason: it called Maryland-College Park a classification
    failure when every one of its forty largest prefixes was correctly judged
    and its 86-course art unit is simply what a 5,770-course university with a
    modest art department has.

    Counts are published. Ratios are reviewed."""
    published, review = [], []
    if art_share > ART_SHARE_MAX and n_courses >= ART_SHARE_MAX_MIN_COURSES:
        review.append(f"art share {art_share:.0%} of a {n_courses}-course catalog "
                      f"is implausibly HIGH — read the prefix list")
    if art_share < ART_SHARE_MIN:
        review.append(f"art share {art_share:.1%} is implausibly LOW — read the "
                      f"prefix list")
    enough = (n_prefixes is None or n_prefixes >= CLASSIFY_FAIL_MIN_PREFIXES)
    if n_art == 0 and n_courses >= CLASSIFY_FAIL_MIN_COURSES and enough:
        # Not a ratio: no art subject in a catalog this size is a mechanical
        # fact, and it is the case the failure label was written for.
        published.append(f"no art-subject prefix identified among {n_prefixes} "
                         f"subject prefixes; subject classification failed"
                         if n_prefixes is not None else
                         f"no art-subject prefix identified in a {n_courses}-course "
                         f"catalog; subject classification failed")
    elif n_art == 0:
        # A thin harvest explains the zero; the harvest reason already says so.
        published.append(
            f"no art-subject prefix identified; only {n_prefixes} subject "
            f"prefix{'es' if n_prefixes != 1 else ''} harvested"
            if n_prefixes is not None
            else f"no art-subject prefix identified; only {n_courses} courses harvested")
    elif n_art < ART_DENOM_FLOOR:
        published.append(f"art-subject set below the {ART_DENOM_FLOOR}-course "
                         f"floor ({n_art} courses)")
    return bool(published), "; ".join(published), review


def rank_corr(xs, ys):
    """Spearman, no scipy. Ties get average ranks."""
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r
    if len(xs) < 3:
        return float("nan")
    rx, ry = rank(xs), rank(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return num / (dx * dy) if dx and dy else float("nan")


def apply_exclusivity(results, rubric):
    """Drop venture wherever practice is present on the same course.

    The rubric's tiebreak already says practice wins when the two are balanced.
    Enforcing it here makes the rule deterministic instead of hoped-for: it
    cannot drift with a model change, and it chooses the less flattering reading,
    so the index cannot overstate venture.
    """
    rule = (rubric.get("axis_exclusivity") or {})
    pairs = rule.get("pairs") or [["venture", "practice"]]
    dropped = 0
    for k, axes in results.items():
        if not isinstance(axes, list):
            continue
        for loser, winner in pairs:
            if loser in axes and winner in axes:
                axes.remove(loser)
                dropped += 1
    return dropped


def aggregate(school, shortlist, results, rubric, model,
              evidence_cap=EVIDENCE_CAP, art_subjects=None):
    sig = prompt_signature(rubric, model)
    version = rubric["rubric_version"]
    per_axis = defaultdict(list)

    # Every count below is COLLAPSED under DEDUPE_RULE; the raw count rides
    # beside it. n_courses_raw is the harvest's record count. The collapse is
    # computed from the courses present, so a fixture with no course list
    # collapses nothing.
    courses = school.get("courses") or []
    n_courses_raw = school["n_courses"]
    n_collapsed = len(courses) - n_distinct(courses)
    n_courses = max(n_courses_raw - n_collapsed, 0)
    total = n_courses or 1

    # Both denominators are computed. Neither is chosen here — that is editorial,
    # and the rank correlation between them is the evidence for whether the
    # choice is load-bearing at all.
    art = set(art_subjects or [])
    art_list = [c for c in courses if in_art(c, art)] if art else []
    n_art_raw = len(art_list)
    n_art = n_distinct(art_list) if art else 0
    school_groups = {dedupe_key(rep_)
                     if dedupe_key(rep_)[0] else None: len(members)
                     for rep_, members in collapse(courses)}

    withheld, withheld_cat = defaultdict(int), defaultdict(int)
    _art = set(art_subjects or [])
    for c in shortlist:
        k = cache_key(course_text(c), sig)
        axes_here = results.get(k, [])
        if WITHHOLD_BLANK and axes_here and no_published_description(c):
            # Withdrawn from the numerator, counted, and left in the
            # denominator. The school still offers the course. Counted twice
            # over: once against the art unit, which is the denominator the
            # published percentages use, and once against the whole catalogue.
            in_unit = in_art(c, _art) if _art else False
            for axis in axes_here:
                withheld_cat[axis] += 1
                if in_unit:
                    withheld[axis] += 1
            continue
        for axis in axes_here:
            per_axis[axis].append(c)

    axes_out, scores = {}, {}
    split, title_only_matched = {}, {}
    for axis, spec in rubric["axes"].items():
        matched_raw = per_axis.get(axis, [])
        groups = collapse(matched_raw, art)
        matched = [rep_ for rep_, _ in groups]
        also = {id(rep_): [m.get("code", "") for m in members if m is not rep_]
                for rep_, members in groups}
        share = len(matched) / total
        s = score_from_share(share, spec["thresholds"])
        scores[axis] = s
        n_art_matched = (sum(1 for c in matched if in_art(c, art))
                         if art else 0)
        n_art_matched_raw = (sum(1 for c in matched_raw if in_art(c, art))
                             if art else 0)
        share_art = (n_art_matched / n_art) if (art and n_art) else None
        # A group counts as matched when any member matched. Where members of
        # one course disagree (descriptions differ past 200 characters, or one
        # missed the prefilter) that is the generous reading, so count it.
        split[axis] = sum(1 for rep_, members in groups
                          if dedupe_key(rep_)[0]
                          and school_groups.get(dedupe_key(rep_), 0) > len(members))
        title_only_matched[axis] = (
            n_distinct([c for c in matched_raw if in_art(c, art)],
                       title_only=True) if art else None)
        # Sample evenly across the matched list rather than slicing off the
        # front. The list arrives in catalog order, so courses[:12] returned only
        # A-titles and made the first evidence read narrower than it was.
        if len(matched) > evidence_cap and evidence_cap > 1:
            # Inclusive span: first and last are always present, so the sample
            # shows the range of what matched rather than one end of it.
            step = (len(matched) - 1) / (evidence_cap - 1)
            shown = [matched[round(i * step)] for i in range(evidence_cap)]
        else:
            shown = matched[:evidence_cap]

        # How much of venture is explicitly branded as entrepreneurship.
        ent = [c for c in matched
               if re.search(r"entrepreneur", c.get("title", "") or "", re.I)]
        axes_out[axis] = {
            "name": spec["name"],
            "n_matched": len(matched),
            "n_matched_raw": len(matched_raw),
            "share_of_catalog": round(share, 5),
            "share_of_art_catalog": (round(share_art, 5)
                                     if share_art is not None else None),
            "n_matched_art": n_art_matched if art else None,
            "n_matched_art_raw": n_art_matched_raw if art else None,
            "score": s,
            "score_art": (score_from_share(share_art, spec["thresholds"])
                          if share_art is not None else None),
            "n_titled_entrepreneurship": len(ent),
            "titled_entrepreneurship": [c.get("code", "") for c in ent[:60]],
            "evidence_shown": len(shown),
            "evidence_truncated": len(matched) > evidence_cap,
            "evidence_cap": evidence_cap,
            # in_art_subjects says which denominator each match counts under.
            # Without it the evidence contradicts the art share: Temple's ACCT
            # and ADV matches appear in the list but are excluded from the art
            # numerator, which reads as a numerator bug rather than a display one.
            "courses": [{"code": c.get("code", ""), "title": c.get("title", ""),
                         "subject": c.get("subject", ""),
                         "in_art_subjects": (
                             ((c.get("subject") or "").strip().upper() in art)
                             if art else None),
                         "also_listed_as": also.get(id(c), []),
                         "source_url": c.get("source_url", "")}
                        for c in shown],
        }

    return {
        "marker": MARKER,
        "unitid": school["unitid"],
        "name": school["name"],
        "platform": school.get("platform", ""),
        "catalog_year": school.get("catalog_year", ""),
        "rubric_version": version,
        "prompt_signature": sig,
        "model": model,
        "scored": hst_today(),
        "measures": rubric["measures"],
        "n_courses": n_courses,
        "n_courses_raw": n_courses_raw,
        # V22. A hand-collected file holds the art unit only, so its "whole
        # catalog" IS its art unit and its whole-catalog share means nothing.
        "catalog_scope": school.get("catalog_scope", "whole_catalog"),
        "n_collapsed": n_collapsed,
        # Verdicts withdrawn because nothing was published to read. A dean
        # whose school carries a large number here is looking at the cheapest
        # thing they can fix to improve their own numbers.
        # Art unit first: it is the denominator the published percentages use.
        "unclassified_no_description": dict(withheld),
        "n_unclassified_no_description": sum(withheld.values()),
        "unclassified_no_description_catalog": dict(withheld_cat),
        "n_unclassified_no_description_catalog": sum(withheld_cat.values()),
        "description_coverage": school.get("description_coverage", 0),
        "axes": axes_out,
        "bottega_quotient": round(sum(scores.values()) / len(scores), 2),
        "n_art_courses": (n_art if art else None),
        "n_art_courses_raw": (n_art_raw if art else None),
        "n_art_collapsed": ((n_art_raw - n_art) if art else None),
        "dedupe": {
            "rule": DEDUPE_RULE,
            # Groups counted matched although some member was not. Near zero
            # means the generous reading costs nothing.
            "split_groups": split,
            # DIAGNOSTIC ONLY: what a looser title-only key would give. Lets the
            # scoring chat see whether the rule is load-bearing without a
            # second code round.
            "title_only": {
                "n_art_courses": (n_distinct(art_list, title_only=True)
                                  if art else None),
                "n_matched_art": title_only_matched,
            },
        },
        "art_share_of_catalog": (round(n_art / total, 4) if art else None),
        "bottega_quotient_art": (
            round(sum(a["score_art"] for a in axes_out.values())
                  / len(axes_out), 2)
            if art and all(a["score_art"] is not None
                           for a in axes_out.values()) else None),
        "coverage_floor": COVERAGE_FLOOR,
        "model_overridden": MODEL_OVERRIDDEN[0],
    }


# ---------------------------------------------------------------- run

# --- composition ------------------------------------------------------------
# A second, separate classification: every course in the art unit gets exactly
# ONE bucket, exhaustive and mutually exclusive, summing to 100%. It answers the
# question the axes cannot — what is this curriculum made of — because the axes
# overlap by design and only measure Bottega content.
#
# NO PREFILTER. The prefilter exists to hold cost down across 500k records; an
# art unit is a few hundred to a few thousand courses, so classifying all of
# them removes the prefilter as a source of error entirely for the dean-facing
# product.
OTHER_TRIPWIRE = 0.05


def composition_prompt(rubric, batch):
    comp = rubric["composition"]
    buckets = "\n".join(f"  {k} — {v}" for k, v in comp["buckets"].items())
    courses = []
    for i, c in enumerate(batch):
        desc = (c.get("description") or "")[:700]
        courses.append(f"[{i}] {c.get('code','')} {c.get('title','')}\n{desc}")
    return (
        comp["question"] + "\n\nBUCKETS:\n" + buckets +
        "\n\nCOURSES:\n\n" + "\n\n".join(courses) +
        "\n\nRespond with JSON only, no preamble, no code fences: an object "
        "whose keys are the bracketed indices as strings and whose values are "
        'exactly one bucket name. Example: {"0": "studio", "1": "capstone"}')


def composition_signature(rubric, model):
    """Hash of the rendered composition prompt, minus courses. See
    prompt_signature for why a field subset is not enough."""
    blob = composition_prompt(rubric, [])
    return hashlib.sha256(f"{model}|{blob}".encode()).hexdigest()[:16]


def parse_composition_response(text, n, valid):
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    out = []
    for i in range(n):
        v = d.get(str(i))
        # Exactly one bucket. A list is the model refusing the constraint, and
        # an unhashable value would otherwise raise inside the membership test.
        out.append(v if isinstance(v, str) and v in valid else None)
    return out


def run_classify_composition(a, model, rubric):
    """One bucket per art-unit course. No prefilter, no shortlist."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    sig = composition_signature(rubric, model)
    valid = set(rubric["composition"]["buckets"])
    cache = load_cache(a.composition_cache)
    art_by_school = load_art_subjects(a.subject_dir)
    report_maker_log()
    if not art_by_school:
        log("  no art-subject sets found — run --classify-subjects first; "
            "composition scores the ART UNIT, not the whole catalog")
        return
    os.makedirs(a.composition_dir, exist_ok=True)
    log(f"  composition prompt-sig {sig}  ({len(valid)} buckets)")

    files = sorted(f for f in os.listdir(a.courses) if f.endswith(".json"))
    if a.schools:
        want = {x.strip() for x in a.schools.split(",") if x.strip()}
        files = [f for f in files if f[:-5] in want]
    elif a.limit:
        files = [f for f in files if art_by_school.get(f[:-5])][:a.limit]

    grand, grand_raw = Counter(), Counter()
    done = 0
    for fn in files:
        with open(os.path.join(a.courses, fn), encoding="utf-8") as fh:
            school = json.load(fh)
        art = art_by_school.get(str(school["unitid"]))
        if not art:
            continue
        courses = [c for c in (school.get("courses") or [])
                   if (c.get("subject") or "").strip().upper() in art]
        if not courses:
            continue

        # Pending is unique by cache key. Identical texts were each sent and
        # paid for — CalArts' Independent Study shells, once per department.
        results, pending, queued = {}, [], set()
        for c in courses:
            k = cache_key(course_text(c), sig)
            if k in cache:
                results[k] = cache[k]
            elif k not in queued:
                queued.add(k)
                pending.append((k, c))

        if pending and not a.dry_run:
            if not api_key:
                log("  ANTHROPIC_API_KEY not set — cannot classify composition")
                return
            # V21. This read `pending[i:i + a.batch_size or 20]`, which Python
            # parses as `(i + a.batch_size) or 20`: with no --batch-size, batch 0
            # was pending[0:20] and every later batch pending[20:20], empty. The
            # pilot classified 20 courses a school and reported them as the art
            # unit. The V14 fixture had two courses — one batch — so it passed.
            bs = a.batch_size or rubric["classifier"].get("batch_size") or 20
            batches = [pending[i:i + bs] for i in range(0, len(pending), bs)]
            fresh, lock = [], threading.Lock()

            def run(idx_batch):
                idx, batch = idx_batch
                raw = call_api(composition_prompt(rubric, [b[1] for b in batch]),
                               model, api_key)
                return batch, parse_composition_response(raw, len(batch), valid)

            try:
                with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
                    futs = {ex.submit(run, (i, b)): i
                            for i, b in enumerate(batches)}
                    for f in as_completed(futs):
                        try:
                            batch, parsed = f.result()
                        except AuthError:
                            raise
                        except Exception as e:
                            log(f"    batch failed: {type(e).__name__}: {e}")
                            continue
                        if parsed is None:
                            log(f"    batch of {len(batch)} returned unparseable "
                                f"JSON; those courses stay unclassified")
                            continue
                        with lock:
                            for (k, c), bucket in zip(batch, parsed):
                                if bucket:
                                    results[k] = bucket
                                    fresh.append({"key": k, "axes": bucket,
                                                  "code": c.get("code", ""),
                                                  "model": model,
                                                  "rubric_version":
                                                      rubric["rubric_version"],
                                                  "prompt_sig": sig})
            except AuthError as e:
                if fresh:
                    append_cache(a.composition_cache, fresh)
                log(f"\n  ABORTING: {e}")
                raise
            if fresh:
                append_cache(a.composition_cache, fresh)

        if a.dry_run:
            log(f"  would classify {school['name'][:36]:<36} "
                f"{n_distinct(courses):>5} art-unit courses "
                f"({len(courses)} records, {len(pending)} uncached texts)")
            continue

        # Primary counts are COLLAPSED under DEDUPE_RULE so n_art_courses here
        # equals n_art_courses on the axis record. Raw counts are kept beside
        # them: the gap in `independent` is the registrar-coding fact.
        counts_raw = Counter()
        for c in courses:
            b = results.get(cache_key(course_text(c), sig))
            if b:
                counts_raw[b] += 1
                c["bucket"] = b
        groups = collapse(courses)
        counts = Counter()
        unclassified = bucket_split = 0
        for rep_, members in groups:
            b = rep_.get("bucket")
            if b:
                counts[b] += 1
            else:
                unclassified += 1
            if len({m.get("bucket") for m in members}) > 1:
                bucket_split += 1
        total = sum(counts.values()) or 1
        grand.update(counts)
        grand_raw.update(counts_raw)

        other_share = counts.get("other", 0) / total
        rec = {
            "marker": MARKER, "unitid": school["unitid"], "name": school["name"],
            "model": model, "prompt_signature": sig,
            "rubric_version": rubric["rubric_version"], "scored": hst_today(),
            "n_art_courses": len(groups),
            "classification_coverage": round(
                sum(counts.values()) / (len(groups) or 1), 4),
            # A composition is published only when it describes the art unit,
            # not a slice of it. Same floor as the axes.
            "coverage_ok": sum(counts.values()) / (len(groups) or 1)
                           >= COVERAGE_FLOOR,
            "n_art_courses_raw": len(courses),
            "n_art_collapsed": len(courses) - len(groups),
            "dedupe_rule": DEDUPE_RULE,
            "n_bucket_split": bucket_split,
            "n_classified": sum(counts.values()),
            "n_unclassified": unclassified,
            "buckets": {k: counts.get(k, 0) for k in valid},
            "buckets_raw": {k: counts_raw.get(k, 0) for k in valid},
            "bucket_pct": {k: round(counts.get(k, 0) / total * 100, 2)
                           for k in valid},
            "other_share": round(other_share, 4),
            "taxonomy_suspect": other_share > OTHER_TRIPWIRE,
            "courses": [{"code": r.get("code", ""), "title": r.get("title", ""),
                         "subject": r.get("subject", ""),
                         "bucket": r.get("bucket"),
                         "also_listed_as": [m.get("code", "") for m in ms
                                            if m is not r],
                         "source_url": r.get("source_url", "")}
                        for r, ms in groups],
        }
        with open(os.path.join(a.composition_dir, f"{school['unitid']}.json"),
                  "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=2, ensure_ascii=False)
        done += 1
        top = ", ".join(f"{k} {rec['bucket_pct'][k]:.0f}%"
                        for k, _ in counts.most_common(4))
        flag = "   TAXONOMY SUSPECT" if rec["taxonomy_suspect"] else ""
        if not rec["coverage_ok"]:
            flag += (f"   INCOMPLETE — {rec['classification_coverage']:.0%} "
                     f"classified; not publishable")
        # "N courses" used to print the CLASSIFIED count, which is how 20-course
        # slices passed for whole art units. Both numbers, always.
        log(f"  {school['name'][:34]:<34} {sum(counts.values()):>5} of "
            f"{len(groups):>5} courses   {top}{flag}")

    if grand and not a.dry_run:
        gt = sum(grand.values())
        gr = sum(grand_raw.values()) or 1
        log(f"\nCORPUS COMPOSITION  [{done} art units, {gt:,} courses "
            f"collapsed from {gr:,} records, one bucket each]")
        log(f"  {'bucket':<15} {'courses':>7}  {'share':>6}   {'records':>7}")
        for k, n in grand.most_common():
            log(f"  {k:<15} {n:>7,}  {n / gt:>6.1%}   {grand_raw[k]:>7,}")
        if grand.get("other", 0) / gt > OTHER_TRIPWIRE:
            log(f"\n  'other' is {grand['other'] / gt:.1%} corpus-wide, above "
                f"the {OTHER_TRIPWIRE:.0%} tripwire.")
            log("  The taxonomy is wrong and should be revised, not forced.")
    log(f"\n  {done} schools written to {a.composition_dir}/")


def prefix_key(school_name, r, sig):
    """v0.10 subject_set_stability: one cached verdict per PREFIX, keyed on what
    the model sees for that prefix alone — school, prefix, course count, sampled
    titles. Adding a course re-keys its own prefix and no other. Through V22 the
    key covered the whole school, so Loyola Marymount's art unit went 268 -> 319
    on one added course, re-judged in full."""
    return cache_key(json.dumps([school_name, r["subject"], r["n"], r["samples"]],
                                ensure_ascii=False), sig)


def build_subject_seeds(sig, cache, subject_dir, seed_dirs):
    """Verdicts already given, so the switch to per-prefix caching re-asks and
    re-bills nothing. -> {(unitid, prefix, n, samples): (status, source)}.

    Course snapshots come first, oldest first: for a prefix whose count and
    samples are unchanged since the snapshot, the EARLIEST verdict wins. That is
    the stability rule applied retroactively — LMU's departments that did not
    change get back the verdict they had before its course list grew. Then the
    current subject files, for everything else."""
    seeds = {}
    for d in seed_dirs:
        if not os.path.isdir(d):
            log(f"  seed snapshot {d} not found; skipped")
            continue
        n_seeded = 0
        for fn in sorted(f for f in os.listdir(d) if f.endswith(".json")):
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as fh:
                    old_school = json.load(fh)
            except Exception:
                continue
            rows = subject_rows(old_school)
            if not rows:
                continue
            old = cache.get(cache_key(subject_blob(rows), sig))
            if not isinstance(old, dict):
                continue
            for r in rows:
                k = (str(old_school.get("unitid")), r["subject"], r["n"], tuple(r["samples"]))
                if k not in seeds and r["subject"] in old:
                    seeds[k] = (_status(old[r["subject"]]), f"snapshot {os.path.basename(d.rstrip('/'))}")
                    n_seeded += 1
        log(f"  seeded {n_seeded} prefix verdicts from snapshot {d}")
    if os.path.isdir(subject_dir):
        for fn in sorted(f for f in os.listdir(subject_dir) if f.endswith(".json")):
            try:
                with open(os.path.join(subject_dir, fn), encoding="utf-8") as fh:
                    rec = json.load(fh)
            except Exception:
                continue
            ov = rec.get("overrides_applied") or {}
            for x in rec.get("art_subjects") or []:
                if "samples" not in x:
                    continue
                k = (str(rec.get("unitid")), x["subject"], x["n"], tuple(x["samples"]))
                if k not in seeds:
                    # the MODEL's verdict, never an override's: overrides are
                    # re-applied after classification
                    st = (ov.get(x["subject"]) or {}).get("model") or x.get("status") \
                        or ("art" if x.get("included") else "out")
                    seeds[k] = (_status(st), "subject file")
    return seeds


def run_classify_subjects(a, model, rubric=None):
    """One call per school. Classifies that school's own prefixes."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    # v0.12 scope_change_implementation: a scope change must never be made by
    # editing the subject prompt. When a rubric edits that prose anyway, pin the
    # prompt to the rubric that last set it deliberately and let the follow-up
    # carry the change — otherwise ~19,000 prefix verdicts re-roll.
    scope_src = rubric
    if getattr(a, "subject_scope_from", ""):
        with open(a.subject_scope_from, encoding="utf-8") as fh:
            scope_src = json.load(fh)
        log(f"  subject prompt pinned to {a.subject_scope_from} "
            f"(rubric {scope_src.get('rubric_version')}); rules and overrides from "
            f"{rubric.get('rubric_version')}")
    scope, _follow = split_scope((scope_src or {}).get("subject_scope"))
    if _follow:
        log(f"  {len(_follow)} scope entries held OUT of the prompt for the "
            f"follow-up pass (--classify-scope); the prompt is unchanged")
    # Editorial overrides by rule: {unitid: {prefix: status}}. Applied AFTER
    # classification, so they are not in the signature and cost nothing to
    # change; recorded on every school they touch, so they are never silent.
    overrides = {str(k): v for k, v in
                 ((rubric or {}).get("subject_overrides") or {}).items()
                 if not str(k).startswith("_")}
    sig = subject_signature(model, scope)
    if scope:
        log("  subject scope read from the rubric; in-scope CIP list from the "
            "roster (CIP_SCOPE)")
    cache = load_cache(a.subject_cache)
    os.makedirs(a.subject_dir, exist_ok=True)
    log(f"  subject prompt-sig {sig}  [one cached verdict per prefix]")
    seed_dirs = [x.strip() for x in (getattr(a, "subject_seed_from", "") or "").split(",")
                 if x.strip()]
    seeds = build_subject_seeds(sig, cache, a.subject_dir, seed_dirs)

    files = sorted(f for f in os.listdir(a.courses) if f.endswith(".json"))
    # --schools was silently ignored here: a five-school request reclassified
    # every school.
    if a.schools:
        want = {x.strip() for x in a.schools.split(",") if x.strip()}
        files = [f for f in files if f[:-5] in want]
    elif a.limit:
        files = files[:a.limit]
    done = skipped = 0
    fresh = []

    for fn in files:
        with open(os.path.join(a.courses, fn), encoding="utf-8") as fh:
            school = json.load(fh)
        rows = subject_rows(school)
        if not rows:
            continue
        uid = str(school["unitid"])
        verdict, pending, sources = {}, [], Counter()
        for r in rows:
            pk = prefix_key(school["name"], r, sig)
            v = cache.get(pk)
            if v is not None:
                verdict[r["subject"]] = _status(v)
                sources["cached"] += 1
                continue
            sd = seeds.get((uid, r["subject"], r["n"], tuple(r["samples"])))
            if sd is not None:
                verdict[r["subject"]] = sd[0]
                sources[sd[1]] += 1
                cache[pk] = sd[0]
                fresh.append({"key": pk, "axes": sd[0], "code": uid, "prefix": r["subject"],
                              "model": model, "rubric_version": "subjects",
                              "prompt_sig": sig, "seeded_from": sd[1]})
                continue
            pending.append(r)

        if pending:
            if a.dry_run:
                log(f"  would classify {school['name'][:38]:<38} {len(pending)} of "
                    f"{len(rows)} prefixes")
                skipped += 1
                continue
            if not api_key:
                log("  ANTHROPIC_API_KEY not set — cannot classify subjects")
                return
            # Only the prefixes nothing has judged yet are asked, in chunks: a
            # 324-prefix school cannot fit one response. Each answer is cached
            # under its own prefix's key.
            chunks = [pending[i2:i2 + SUBJECT_CHUNK]
                      for i2 in range(0, len(pending), SUBJECT_CHUNK)]
            failed = 0
            for ci, chunk in enumerate(chunks):
                try:
                    raw = call_api(subject_prompt(school["name"], chunk, scope),
                                   model, api_key)
                except AuthError:
                    raise
                except Exception as e:
                    log(f"  {school['name'][:34]}: chunk {ci + 1}/{len(chunks)} "
                        f"{type(e).__name__}: {e}")
                    failed += 1
                    continue
                part = parse_subject_response(raw, {r["subject"] for r in chunk})
                if part is None:
                    log(f"  {school['name'][:34]}: chunk {ci + 1}/{len(chunks)} "
                        f"unparseable. Raw response began:")
                    log(f"    {(raw or '')[:200]!r}")
                    failed += 1
                    continue
                for r in chunk:
                    if r["subject"] in part:
                        st = part[r["subject"]]
                        verdict[r["subject"]] = st
                        sources["fresh"] += 1
                        pk = prefix_key(school["name"], r, sig)
                        cache[pk] = st
                        fresh.append({"key": pk, "axes": st, "code": uid,
                                      "prefix": r["subject"], "model": model,
                                      "rubric_version": "subjects", "prompt_sig": sig})
            if failed == len(chunks) and not verdict:
                log(f"  {school['name'][:38]}: no chunk parsed; skipped")
                skipped += 1
                continue
            if failed:
                log(f"  {school['name'][:34]}: {failed} of {len(chunks)} chunks "
                    f"lost — art set is incomplete")

        applied = {}
        for pfx, st in (overrides.get(str(school["unitid"])) or {}).items():
            if pfx in {r["subject"] for r in rows}:
                verdict = dict(verdict)
                applied[pfx] = {"model": _status(verdict.get(pfx)),
                                "override": _status(st)}
                verdict[pfx] = _status(st)
        art = [r for r in rows if is_art(verdict.get(r["subject"]))]
        # Excluded for DELIVERY or as non-subject codes: reported per school so
        # the decision is visible and reversible (scoring chat, v0.8).
        excl = {}
        for st in ("extension", "administrative"):
            rs = [r for r in rows if _status(verdict.get(r["subject"])) == st]
            excl[st] = {"prefixes": [r["subject"] for r in rs],
                        "n_courses": sum(r["n"] for r in rs)}
        n_art = sum(r["n"] for r in art)
        total = school["n_courses"] or 1
        share = n_art / total
        bad, reason, review = art_share_check(share, n_art, total, len(rows))
        rec = {
            "marker": MARKER, "unitid": school["unitid"], "name": school["name"],
            "model": model, "prompt_signature": sig, "scored": hst_today(),
            "cip_scope": CIP_SCOPE,
            "art_subjects": [{"subject": r["subject"], "n": r["n"],
                              "included": is_art(verdict.get(r["subject"])),
                              "status": _status(verdict.get(r["subject"])),
                              "n_sampled": r.get("n_sampled"),
                              "n_titles": r.get("n_titles"),
                              "samples": r["samples"]} for r in rows],
            "n_art_courses": n_art, "n_courses": school["n_courses"],
            "art_share_of_catalog": round(share, 4),
            "implausible": bad, "implausible_reason": reason,
            # Never published; printed so a human reads the prefix list.
            "review_flags": review,
            "excluded_delivery": excl["extension"],
            "excluded_not_subjects": excl["administrative"],
            "subject_scope_from_rubric": bool(scope),
            "overrides_applied": applied,
            "sampling_rule": SAMPLE_RULE,
            "verdict_sources": dict(sources),
        }
        # A dry run writes nothing. It used to rewrite every cached school's
        # subject file, which is not what "dry" promises.
        if not a.dry_run:
            with open(os.path.join(a.subject_dir, f"{school['unitid']}.json"),
                      "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2, ensure_ascii=False)
            done += 1
        flag = f"   HELD BACK — {reason}" if bad else ""
        flag += ("   REVIEW: " + "; ".join(review)) if review else ""
        if applied:
            flag += "   [override: " + ", ".join(
                f"{k} {v['model']}->{v['override']}" for k, v in applied.items()) + "]"
        for st, lab in (("extension", "extension/pre-college"),
                        ("administrative", "non-subject codes")):
            if excl[st]["prefixes"]:
                flag += (f"   [{lab} out: {'/'.join(excl[st]['prefixes'])}, "
                         f"{excl[st]['n_courses']} courses]")
        log(f"  {school['name'][:38]:<38} {len(art):>3}/{len(rows):<3} prefixes  "
            f"{n_art:>6}/{total:<6} courses  {share:>5.0%}{flag}")

    if fresh and not a.dry_run:
        append_cache(a.subject_cache, fresh)
    log(f"\n  {done} schools written to {a.subject_dir}/, {skipped} skipped"
        + ("  [--dry-run: nothing written]" if a.dry_run else ""))


def load_subject_flags(subject_dir):
    """-> {unitid: reason} for subject sets the classifier run marked
    implausible. The flag was written to every subject file and never read, so
    a 15-course art unit at UTEP was ranked as if it were RISD's."""
    out = {}
    if not os.path.isdir(subject_dir):
        return out
    for fn in sorted(f for f in os.listdir(subject_dir) if f.endswith(".json")):
        try:
            with open(os.path.join(subject_dir, fn), encoding="utf-8") as fh:
                r = json.load(fh)
            if r.get("implausible"):
                out[str(r["unitid"])] = r.get("implausible_reason") or "implausible"
        except Exception:
            continue
    return out


def join_reasons(*parts):
    """One reason per fact, deduplicated, order preserved. Oklahoma published
    'only 9 courses harvested; only 9 courses harvested' because the subject
    reason and the harvest reason each said it."""
    out = []
    for part in parts:
        for bit in (x.strip() for x in (part or "").split(";")):
            if bit and bit not in out:
                out.append(bit)
    return "; ".join(out)


def art_unit_check(rec, subject_flag):
    """-> (unranked: bool, reason: str). The ranking is on the ART-UNIT share,
    so a school without a trustworthy art unit is reported, never ranked."""
    if subject_flag:
        # v0.13: the published reason is the fact itself. "art-subject set
        # implausible:" prefixed a judgement onto a count, and outlived the
        # change that stopped us publishing judgements.
        return True, subject_flag
    if not rec.get("n_art_courses"):
        return True, "no art-subject denominator"
    return False, ""


_READ_LOCK = None


def acquire_read_lock(path):
    """-> (True, "") or (False, holder). A SHARED lock on harvest's lock file.

    Any number of score runs may hold it together; a harvest, which takes it
    exclusive, cannot start while one does, and a score run cannot start while a
    harvest is writing. On 2026-09-10 a subject run read acalog files while a
    replay rewrote them, and Delaware's count moved between two runs ten
    minutes apart."""
    global _READ_LOCK
    if not os.path.exists(os.path.dirname(path) or "."):
        return True, ""
    fh = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        fh.seek(0)
        holder = fh.read().strip()
        fh.close()
        return False, holder
    _READ_LOCK = fh
    return True, ""


def catalog_fingerprint(courses):
    keys = sorted((c.get("code", ""), c.get("title", ""), c.get("description", ""))
                  for c in courses)
    return hashlib.sha256(json.dumps(keys, ensure_ascii=False).encode()).hexdigest()


def _domain(web):
    h = urllib.parse.urlparse(web if "://" in (web or "") else f"https://{web}").netloc
    return h.lower().removeprefix("www.")


def find_duplicate_catalogs(course_dir, files, roster_rec):
    """-> ({secondary_unitid: publisher_unitid}, {publisher: [secondaries]}).
    The publisher is the institution whose own web domain hosts the catalog;
    failing that, the one with more in-scope degrees."""
    groups = defaultdict(list)
    for fn in files:
        try:
            with open(os.path.join(course_dir, fn), encoding="utf-8") as fh:
                sch = json.load(fh)
        except Exception:
            continue
        if sch.get("courses"):
            groups[catalog_fingerprint(sch["courses"])].append(
                (str(sch["unitid"]), sch.get("catalog_url", "")))
    dup_of, shares = {}, {}
    for members in groups.values():
        if len(members) < 2:
            continue
        host = urllib.parse.urlparse(members[0][1]).netloc.lower()
        own = [u for u, _ in members
               if _domain(roster_rec.get(u, {}).get("webaddr", "")) and
               host.endswith(_domain(roster_rec.get(u, {}).get("webaddr", "")))]
        pri = own[0] if len(own) == 1 else max(
            (u for u, _ in members),
            key=lambda u: (roster_rec.get(u, {}).get("completions") or {}).get("total", 0))
        for u, _ in members:
            if u != pri:
                dup_of[u] = pri
                shares.setdefault(pri, []).append(u)
    return dup_of, shares


def _lex(exact, prefix):
    import re as _re
    def esc(t):
        return r"[\s\-]+".join(_re.escape(w) for w in t.split())
    pats = [r"\b" + esc(t) + r"\b" for t in exact] + [r"\b" + esc(t) for t in prefix]
    return _re.compile("|".join(pats), _re.I) if pats else _re.compile(r"(?!)")


def maker_rule_ratios(titles, lex):
    """The three published ratios, over a prefix's own titles."""
    n = len(titles) or 1
    mp = _lex(lex["makers_practice"]["exact"], lex["makers_practice"]["prefix"])
    ar = _lex(lex["art_referent"]["exact"],
              [t for t in lex["art_referent"]["prefix"] if t != "design"])
    dz = _lex([], ["design"])
    na = _lex(lex["art_referent"]["design_not_art"], [])
    ad = _lex(lex["admin"]["exact"], lex["admin"]["prefix"])
    prac = [bool(mp.search(t)) for t in titles]
    art = [bool(ar.search(t)) or (bool(dz.search(t)) and not na.search(t))
           for t in titles]
    adm = [bool(ad.search(t)) for t in titles]
    return {"makers_practice": sum(prac) / n,
            "art_referent": sum(art) / n,
            "admin_only": sum(1 for i in range(len(titles))
                              if adm[i] and not prac[i]) / n}


# The rubric says family exclusions apply first and absolutely. The subjects
# file records status "out" without recording WHY, and subject_scope's
# excluded_families is prose rather than a term list, so this cannot be
# enforced from the data. It is enforced conservatively instead: a prefix whose
# titles are substantially performing arts is refused and reported, whatever
# its three ratios say. Nothing is caught by this guard today — EMA and FPA are
# the only prefixes that pass — so it changes no number now and closes the gap
# before the corpus grows.
_PERF = __import__("re").compile(
    r"music|dance|theat(re|er)|opera|choir|orchestr|ballet|acting|vocal|"
    r"percussion|piano|drama|performing arts", __import__("re").I)
MAKER_PERF_GUARD = 0.30


def apply_maker_rule(rec, rubric, log=None):
    """-> (set of prefixes admitted, [notes]). Admits only; never removes."""
    block = (rubric or {}).get("maker_rule") or {}
    lex, th = block.get("lexicons"), block.get("thresholds")
    if not lex or not th:
        return set(), ["maker_rule block absent from the rubric — nothing applied"]
    def cut(k, default):
        try:
            return float(str(th[k]).split()[-1])
        except Exception:
            return default
    c_prac, c_art = cut("makers_practice", 0.5), cut("art_referent", 0.5)
    c_adm = cut("admin_only", 0.5)
    # The ruling was measured over every title a prefix has, not over the stored
    # sample, so the implementation reads the courses file. Falling back to the
    # sample would compute a different ratio from the one that was ruled on and
    # would do it silently; where the fallback is used it is recorded.
    full = {}
    src = "courses"
    cp = os.path.join(MAKER_COURSES_DIR or "", f"{rec.get('unitid')}.json")
    if MAKER_COURSES_DIR and os.path.exists(cp):
        try:
            with open(cp, encoding="utf-8") as fh:
                for c in (json.load(fh).get("courses") or []):
                    full.setdefault((c.get("subject") or "").strip().upper(),
                                    []).append(c.get("title") or "")
        except Exception:
            full, src = {}, "samples (courses file unreadable)"
    else:
        src = "samples (no courses file)"
    admitted, notes = set(), []
    for x in rec.get("art_subjects", []):
        if x.get("included") or str(x.get("status", "")).strip().lower() != "out":
            continue
        titles = full.get(str(x.get("subject", "")).strip().upper()) or list(
            x.get("samples") or [])
        if len(titles) < 4:
            continue
        r = maker_rule_ratios(titles, lex)
        if not (r["makers_practice"] >= c_prac and r["art_referent"] >= c_art
                and r["admin_only"] < c_adm):
            continue
        # SCORE_V35_GUARD: the guard's terms and rule are published in the
        # rubric (v0.49 maker_rule.performing_arts_guard); majority, not presence.
        _g = block.get("performing_arts_guard") or {}
        if _g.get("terms"):
            _gp = __import__("re").compile(
                r"\b(?:" + "|".join(__import__("re").escape(t) for t in _g["terms"]) + ")",
                __import__("re").I)
            perf = sum(1 for t in titles if _gp.search(t)) / len(titles)
            _refuse = perf > 0.5
        else:
            perf = sum(1 for t in titles if _PERF.search(t)) / len(titles)
            _refuse = perf >= MAKER_PERF_GUARD
        if _refuse:
            notes.append(f"REFUSED {x['subject']}: performing-arts share "
                         f"{perf:.2f} — family exclusions apply first")
            continue
        admitted.add(x["subject"])
        notes.append(f"admitted {x['subject']} n={x.get('n')} "
                     f"titles={len(titles)} from={src} "
                     f"practice={r['makers_practice']:.2f} "
                     f"artref={r['art_referent']:.2f} "
                     f"admin_only={r['admin_only']:.2f}")
    return admitted, notes


_XREF_CACHE = {}
MAKER_RULE = False
MAKER_RUBRIC = None
MAKER_COURSES_DIR = "data/courses"
MAKER_LOG = []


def report_maker_log():
    """Say what the rule did. Admissions and refusals both."""
    if not MAKER_LOG:
        if MAKER_RULE:
            log("  MAKER RULE: no prefix met all three terms")
        return
    n_adm = sum(1 for _, notes in MAKER_LOG for x in notes if x.startswith("admitted"))
    n_ref = sum(1 for _, notes in MAKER_LOG for x in notes if x.startswith("REFUSED"))
    log(f"  MAKER RULE: {n_adm} admitted, {n_ref} refused by the family guard")
    for uid, notes in MAKER_LOG:
        for x in notes:
            log(f"    {uid}  {x}")


def load_art_subjects(subject_dir):
    """-> {unitid: set(prefixes)}. Empty when the mode has not been run."""
    out = {}
    if not os.path.isdir(subject_dir):
        return out
    for fn in sorted(f for f in os.listdir(subject_dir) if f.endswith(".json")):
        try:
            with open(os.path.join(subject_dir, fn), encoding="utf-8") as fh:
                r = json.load(fh)
            art = {x["subject"] for x in r["art_subjects"] if x.get("included")}
            if MAKER_RULE and MAKER_RUBRIC:
                extra, notes = apply_maker_rule(r, MAKER_RUBRIC)
                if notes:
                    MAKER_LOG.append((str(r["unitid"]), notes))
                art |= extra
            out[str(r["unitid"])] = art
        except Exception:
            continue
    return out

# SCORE_V39_COURSE_BOARD -------------------------------------------------------
COURSE_BOARD = {}          # unitid -> list of row dicts
COURSE_BOARD_UNMATCHED = []


def load_course_board(path):
    """Read a course-level board: unitid, code, title, axis, action, board."""
    import csv as _csv
    board = {}
    for r in _csv.DictReader(open(path, encoding="utf-8-sig")):
        act = (r.get("action") or "").strip().upper()
        if act not in ("REMOVE", "ADD"):
            continue
        board.setdefault(str(r["unitid"]).strip(), []).append({
            "code": " ".join((r.get("code") or "").split()).upper(),
            "title": (r.get("title") or "").strip().lower()[:60],
            "axis": (r.get("axis") or "").strip().lower(),
            "action": act, "board": (r.get("board") or "").strip(),
            "raw_code": r.get("code", ""), "raw_title": r.get("title", "")})
    return board


def apply_course_board(school, shortlist, results, sig, board_rows):
    """Adjust this school's verdicts per the board. Returns the applied list.

    Never mutates a verdict list in place (they may be shared with the cache's
    in-memory copy); every adjusted key gets a new list.
    """
    applied = []
    if not board_rows:
        return applied
    by = {}
    for c in shortlist:
        k = (" ".join((c.get("code") or "").split()).upper(),
             (c.get("title") or "").strip().lower()[:60])
        by.setdefault(k, []).append(c)
    for row in board_rows:
        hits = by.get((row["code"], row["title"]))
        if not hits:
            COURSE_BOARD_UNMATCHED.append((str(school.get("unitid")), row["raw_code"], row["raw_title"], row["board"]))
            continue
        # SCORE_V40_BOARD_GROUPS: rule the counted course — every record at the
        # school in the named record's dedupe group, as the scorer collapses them.
        _grp = {dedupe_key(h) for h in hits}
        hits = [c for c in shortlist if dedupe_key(c) in _grp]
        for c in hits:
            key = cache_key(course_text(c), sig)
            axes = list(results.get(key) or [])
            before = list(axes)
            if row["action"] == "REMOVE" and row["axis"] in axes:
                axes.remove(row["axis"])
            elif row["action"] == "ADD" and row["axis"] not in axes:
                axes.append(row["axis"])
            results[key] = axes
            applied.append({"code": c.get("code", ""), "title": c.get("title", ""),
                            "axis": row["axis"], "action": row["action"], "board": row["board"],
                            "before": before, "after": list(axes)})
    return applied
# ------------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(description="Score harvested courses.")
    p.add_argument("--course-board", default="",
                   help="CSV of course-level rulings (unitid, code, title, axis, "
                        "action REMOVE|ADD, board), applied after classification "
                        "and recorded on each scorecard")
    p.add_argument("--art-unit-full", action="store_true",
                   help="classify every described course inside the art unit, "
                        "not only the lexicon prefilter's shortlist; courses "
                        "outside the art unit still go through the prefilter")
    p.add_argument("--maker-rule", action="store_true",
                   help="admit prefixes by the v0.25 maker rule")
    p.add_argument("--withhold-blank", action="store_true",
                   help="withhold verdicts resting on empty or cross-reference-only "
                        "descriptions; numerator only")
    p.add_argument("--dedupe-articles", action="store_true",
                   help="a leading article does not make a second course")
    p.add_argument("--resolve-uncached", action="store_true",
                   help="with --resolve-xrefs, adopt a sibling description even "
                        "when the resolved text is not cached; this bills")
    p.add_argument("--resolve-xrefs", action="store_true",
                   help="a cross-reference stub adopts its sibling's description")
    p.add_argument("--drop-catalog-dupes", default="",
                   help="comma-separated unitids harvested from another school's "
                        "catalogue, e.g. 183822")
    p.add_argument("--courses", default="data/courses")
    p.add_argument("--rubric", required=True,
                   help="REQUIRED. The default was rubric_v0.2.json — four axes, "
                        "no practice, no composition — so a forgotten flag "
                        "scored the wrong index and overwrote the right one "
                        "without a word")
    p.add_argument("--out-dir", default="data/scores")
    p.add_argument("--log-dir", default="data")
    p.add_argument("--index-with-scores", action="store_true",
                   help="force index.csv and the run log beside the score files. "
                        "Implied whenever --out-dir is set away from the default, "
                        "so a five-school run cannot overwrite a 130-row rollup")
    p.add_argument("--cache", default="data/classifications.jsonl")
    p.add_argument("--model", default="",
                   help="must equal the rubric's classifier.model unless "
                        "--allow-model-override; the model is part of the "
                        "instrument")
    p.add_argument("--allow-model-override", action="store_true",
                   help="exploratory runs only; every record is marked")
    p.add_argument("--batch-size", type=int, default=0)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--schools", default="",
                   help="comma-separated unitids. --limit takes the first N "
                        "files by name, which are mostly empty ones")
    p.add_argument("--harvest-ratio-floor", type=float, default=None,
                   help="flag a school whose courses-per-degree is below this "
                        "fraction of the corpus median. Off by default: the "
                        "ratio varies ~50x by institution type")
    p.add_argument("--classify-subjects", action="store_true",
                   help="classify each school's own subject prefixes as art or "
                        "not; one call per school, cached")
    p.add_argument("--roster", default="data/roster.json",
                   help="supplies completions, for the harvest plausibility rule")
    p.add_argument("--subject-dir", default="data/subjects")
    p.add_argument("--subject-cache", default="data/subject_classifications.jsonl")
    p.add_argument("--subject-scope-from", default="",
                   help="render the SUBJECT PROMPT from this rubric's subject_scope "
                        "while rules and overrides come from --rubric. Pins the "
                        "prompt when a later rubric edits scope prose that would "
                        "otherwise re-key every prefix in the corpus")
    p.add_argument("--subject-seed-from", default="",
                   help="comma-separated course snapshots, OLDEST FIRST, whose "
                        "whole-school verdicts seed the per-prefix cache: a prefix "
                        "unchanged since a snapshot keeps its earliest verdict")
    p.add_argument("--allow-new-followup", action="store_true",
                   help="permit a scope follow-up whose prompt signature is not "
                        "yet in the subject cache; this asks the whole corpus")
    p.add_argument("--classify-scope", action="store_true",
                   help="narrow follow-up: remove a CIP family from art sets "
                        "already classified, cached per prefix, prompt untouched")
    p.add_argument("--classify-composition", action="store_true",
                   help="one bucket per art-unit course, exhaustive and "
                        "mutually exclusive. No prefilter.")
    p.add_argument("--composition-dir", default="data/composition")
    p.add_argument("--composition-cache",
                   default="data/composition_classifications.jsonl")
    p.add_argument("--workers", type=int, default=6,
                   help="concurrent classification batches. Offline "
                        "classification parallelises cleanly; 1 for sequential")
    p.add_argument("--evidence-cap", type=int, default=EVIDENCE_CAP,
                   help="max matched courses recorded per axis; the record "
                        "always reports whether it bit")
    p.add_argument("--dry-run", action="store_true",
                   help="prefilter only; no API key needed, no cost")
    p.add_argument("--harvest-lock", default="data/.harvest.lock",
                   help="shared with harvest.py; a score run will not read "
                        "course files while a harvest is writing them")
    a = p.parse_args()
    ok, holder = acquire_read_lock(a.harvest_lock)
    if not ok:
        print(f"A HARVEST IS WRITING COURSE FILES — {holder or a.harvest_lock}\n"
              f"Refusing to read a corpus that is changing under the run. "
              f"Wait for: pgrep -fl harvest.py", file=sys.stderr)
        sys.exit(3)

    # A run redirected with --out-dir must not write its rollup over the full
    # one. A five-school pass replaced a 130-row index.csv with five rows.
    if a.out_dir != "data/scores":
        a.index_with_scores = True

    log(f"{MARKER} — run {hst_today()} HST")
    with open(a.rubric, encoding="utf-8") as fh:
        rubric = json.load(fh)

    # Patch 1 declared these flags and never connected them. A flag that is
    # accepted and ignored produces a number that looks like an answer.
    global MAKER_RULE, MAKER_RUBRIC, MAKER_COURSES_DIR, ARTICLE_STRIP
    if getattr(a, "maker_rule", False):
        MAKER_RULE = True
        MAKER_RUBRIC = rubric
        MAKER_COURSES_DIR = a.courses
        block = (rubric or {}).get("maker_rule") or {}
        if not block.get("lexicons"):
            print("--maker-rule was passed but the rubric carries no "
                  "maker_rule.lexicons block. Refusing to run a rule that is "
                  "not published.", file=sys.stderr)
            sys.exit(2)
        log(f"  MAKER RULE ON — lexicons from {os.path.basename(a.rubric)}, "
            f"thresholds {block.get('thresholds')}")
    global WITHHOLD_BLANK, RESOLVE_XREFS, _XREF_CACHE
    if getattr(a, "withhold_blank", False):
        WITHHOLD_BLANK = True
        log("  WITHHOLD — a verdict resting on no published description is "
            "withdrawn from the numerator, counted, and left in the denominator")
    global RESOLVE_UNCACHED
    if getattr(a, "resolve_uncached", False):
        RESOLVE_UNCACHED = True
        log("  RESOLVE UNCACHED — a stub will adopt its sibling's description "
            "even when the result has never been classified. THIS SPENDS MONEY: "
            "run with --dry-run first and read the 'to classify' total, which is "
            "the real bill and may exceed the refused count, because resolution "
            "runs before the prefilter.")
    if getattr(a, "resolve_xrefs", False):
        RESOLVE_XREFS = True
        _XREF_CACHE = load_cache(a.cache)
        log(f"  RESOLVE XREFS — {len(_XREF_CACHE):,} cached verdicts available; "
            f"a stub is only resolved when its resolved text is already one of "
            f"them")
    if getattr(a, "art_unit_full", False):
        log("  ART-UNIT FULL — inside each art unit every described course is "
            "classified; the prefilter governs only the whole-catalogue scope")
    if getattr(a, "dedupe_articles", False):
        ARTICLE_STRIP = True
        try:
            import harvest as _H
            _H.ARTICLE_STRIP = True
        except Exception:
            pass
        log(f"  DEDUPE — {dedupe_rule()}")
    # The model is part of the instrument. v0.6 was classified by Sonnet 4.6
    # through --model while the rubric named Haiku 4.5; the acceptance run then
    # took the rubric default, and every axis rose 43% with no rubric change.
    pinned = rubric["classifier"]["model"]
    if a.model and a.model != pinned and not a.allow_model_override:
        print(f"--model {a.model} differs from the rubric's classifier.model "
              f"{pinned}.\nThe model is part of the instrument: change it in the "
              f"rubric and bump the version, or pass --allow-model-override for "
              f"an exploratory run.", file=sys.stderr)
        sys.exit(2)
    model = a.model or pinned
    MODEL_OVERRIDDEN[0] = model != pinned
    if MODEL_OVERRIDDEN[0]:
        log(f"  MODEL OVERRIDE: {model} in place of the rubric's {pinned} — "
            f"every record is marked; not for publication")
    batch = a.batch_size or rubric["classifier"]["batch_size"]
    log(f"  rubric {rubric['rubric_version']}  model {model}  "
        f"prompt-sig {prompt_signature(rubric, model)}")
    log("  [cache is keyed on the prompt signature, not the rubric version — a "
        "lexicon-only change no longer invalidates paid classifications]")
    if rubric.get("status", "").startswith("SEED"):
        log("  NOTE: rubric is a seed. Thresholds are guesses and must be "
            "calibrated against the real distribution before publication.")

    if a.classify_composition:
        if "composition" not in rubric:
            log("  this rubric has no composition block — needs v0.6 or later")
            return
        log("\nCLASSIFYING COMPOSITION  [every art-unit course, one bucket, "
            "no prefilter]")
        try:
            run_classify_composition(a, model, rubric)
        except AuthError:
            log("\nRun aborted.")
        return

    if a.classify_subjects:
        log("\nCLASSIFYING SUBJECT PREFIXES  [one call per school]")
        run_classify_subjects(a, model, rubric)
        return
    if a.classify_scope:
        run_classify_scope(a, model, rubric)
        return

    global HARVEST_RATIO_FLOOR
    if a.harvest_ratio_floor is not None:
        HARVEST_RATIO_FLOOR = a.harvest_ratio_floor
        log(f"  harvest ratio floor enabled at {HARVEST_RATIO_FLOOR:.0%} "
            f"of the corpus median")

    files = sorted(f for f in os.listdir(a.courses) if f.endswith(".json"))
    if a.schools:
        want = {x.strip() for x in a.schools.split(",") if x.strip()}
        files = [f for f in files if f[:-5] in want]
        missing = want - {f[:-5] for f in files}
        if missing:
            log(f"  no course file for: {', '.join(sorted(missing))}")
    elif a.limit:
        # Take the first N files WITH COURSES; the directory is mostly empty
        # files and --limit 5 was silently scoring one school.
        picked = []
        for f in files:
            try:
                with open(os.path.join(a.courses, f), encoding="utf-8") as fh:
                    if json.load(fh).get("n_courses"):
                        picked.append(f)
            except Exception:
                continue
            if len(picked) >= a.limit:
                break
        files = picked
    log(f"  {len(files)} harvested schools")

    completions, roster_rec, tier_threshold = {}, {}, None
    try:
        with open(a.roster, encoding="utf-8") as fh:
            _ro = json.load(fh)
        tier_threshold = _ro.get("tier_threshold")
        for i in _ro["institutions"]:
            completions[str(i["unitid"])] = i["completions"]["total"]
            roster_rec[str(i["unitid"])] = i
    except Exception:
        log(f"  no roster at {a.roster} — harvest plausibility and tier rules disabled")

    # v0.10 duplicate_catalogs: two institutions publishing a byte-identical
    # catalog are one catalog, reported once under its publisher.
    dup_of, shares = find_duplicate_catalogs(a.courses, files, roster_rec)
    for sec, pri in dup_of.items():
        log(f"  DUPLICATE CATALOG: {roster_rec.get(sec, {}).get('name', sec)} publishes "
            f"the same catalog as {roster_rec.get(pri, {}).get('name', pri)}; "
            f"counted once, under the publisher")

    art_by_school = load_art_subjects(a.subject_dir)
    report_maker_log()
    subject_flags = load_subject_flags(a.subject_dir)
    global COURSE_BOARD
    if getattr(a, "course_board", ""):
        COURSE_BOARD = load_course_board(a.course_board)
        log(f"  COURSE BOARD — {sum(len(v) for v in COURSE_BOARD.values())} rulings at "
            f"{len(COURSE_BOARD)} schools from {a.course_board}; applied after "
            f"classification, recorded on each scorecard")
    if art_by_school:
        log(f"  art-subject sets loaded for {len(art_by_school)} schools; "
            f"both denominators will be computed")
        if subject_flags:
            log(f"  {len(subject_flags)} art-subject sets are flagged implausible "
                f"and will be reported, not ranked")
    else:
        log("  no art-subject sets found — run --classify-subjects to get the "
            "art denominator alongside the whole-catalog one")

    matchers = build_matchers(rubric)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    os.makedirs(a.out_dir, exist_ok=True)

    rows, corpus, shortlisted = [], 0, 0
    all_hits = defaultdict(list)

    # Corpus median courses-per-degree, computed before scoring so the
    # plausibility rule is derived from the data rather than chosen.
    ratios = []
    all_files = sorted(f for f in os.listdir(a.courses) if f.endswith(".json"))
    for fn in all_files:
        try:
            with open(os.path.join(a.courses, fn), encoding="utf-8") as fh:
                sc = json.load(fh)
            cmp_ = completions.get(str(sc["unitid"]))
            if sc.get("n_courses") and cmp_:
                ratios.append(sc["n_courses"] / cmp_)
        except Exception:
            continue
    ratios.sort()
    corpus_ratio = [ratios[len(ratios) // 2] if ratios else 0.0]
    if ratios:
        def rq(f):
            return ratios[min(int(len(ratios) * f), len(ratios) - 1)]
        log(f"\n  COURSES PER DEGREE CONFERRED  [n={len(ratios)} schools, "
            f"whole corpus]")
        log(f"    min {ratios[0]:.2f}  p10 {rq(.10):.2f}  p25 {rq(.25):.2f}  "
            f"median {corpus_ratio[0]:.2f}  p75 {rq(.75):.2f}  "
            f"max {ratios[-1]:.2f}")
        log("    [varies ~50x by institution type for legitimate reasons: a big "
            "public has")
        log("     a huge catalog and modest CIP-50 completions, an art college "
            "the reverse.")
        log("     Set --harvest-ratio-floor from this only if it can be made "
            "type-aware;")
        log(f"     the absolute floor of {MIN_PLAUSIBLE_COURSES} courses runs "
            f"regardless.]")
    for fn in files:
        with open(os.path.join(a.courses, fn), encoding="utf-8") as fh:
            school = json.load(fh)
        if not school.get("courses"):
            continue
        if str(school["unitid"]) in dup_of:
            continue            # counted once, under its publisher
        corpus += school["n_courses"]
        if RESOLVE_XREFS:
            resolve_xrefs(school, _XREF_CACHE, prompt_signature(rubric, model))

        hits, shortlist = prefilter(school["courses"], matchers)
        if getattr(a, "art_unit_full", False):
            # SCORE_V37_ARTUNIT_FULL: inside the art unit the prefilter does not
            # decide what is seen; every described course is classified. Keyed on
            # the same dedupe key the scorer counts with, so a course already
            # shortlisted is not added twice.
            _art = art_by_school.get(str(school["unitid"])) or set()
            if _art:
                # SCORE_V38_FULLKEY: compare on what the classifier keys on —
                # the full course text. The dedupe key (title + first 200 chars)
                # is shared across subjects by boilerplate entries, which made
                # V37 skip art-unit courses whose non-art twin was shortlisted.
                _sig_now = prompt_signature(rubric, model)
                _have = {cache_key(course_text(c), _sig_now) for c in shortlist}
                _extra = [c for c in school["courses"]
                          if in_art(c, _art)
                          and not no_published_description(c)
                          and cache_key(course_text(c), _sig_now) not in _have]
                if _extra:
                    shortlist = list(shortlist) + _extra
                    log(f"    ART-UNIT FULL: +{len(_extra)} art-unit courses the "
                        f"prefilter did not match")
        shortlisted += len(shortlist)
        for axis, cs in hits.items():
            all_hits[axis].extend(cs)
        log(f"\n{school['name']}  {school['n_courses']} courses  "
            f"-> {len(shortlist)} shortlisted "
            f"({len(shortlist) / max(school['n_courses'], 1):.1%})")
        for axis in rubric["axes"]:
            log(f"    {axis:<12} {len(hits.get(axis, [])):>5} prefilter hits")

        try:
            results, _, skipped = classify(shortlist, rubric, api_key, a.cache,
                                           model, batch, dry=a.dry_run,
                                           workers=a.workers)
        except AuthError:
            log("\nRun aborted before any score was written.")
            return
        n_excl = apply_exclusivity(results, rubric)
        if n_excl:
            log(f"    exclusivity: dropped venture on {n_excl} courses that "
                f"also carried practice")
        _cb = apply_course_board(school, shortlist, results,
                                 prompt_signature(rubric, model),
                                 COURSE_BOARD.get(str(school["unitid"])))
        if _cb:
            log(f"    course board: {len(_cb)} ruling(s) applied")
        rec = aggregate(school, shortlist, results, rubric, model,
                        evidence_cap=a.evidence_cap,
                        art_subjects=art_by_school.get(str(school["unitid"])))
        if _cb:
            rec["course_board_applied"] = _cb
        if rec["n_collapsed"] or rec.get("n_art_collapsed"):
            log(f"    collapsed {rec['n_collapsed']} duplicate records"
                + (f" ({rec['n_art_collapsed']} in the art unit)"
                   if rec.get("n_art_collapsed") is not None else ""))
        rec["classification_coverage"] = (
            round(1 - skipped / len(shortlist), 3) if shortlist else 1.0)
        rec["unranked"], rec["unranked_reason"] = floor_check(
            rec["description_coverage"], rec["classification_coverage"])
        _uid = str(school["unitid"])
        if _uid in shares:
            rec["shares_catalog_with"] = [
                {"unitid": u, "name": roster_rec.get(u, {}).get("name", u)}
                for u in shares[_uid]]
            rec["completions_combined"] = sum(
                completions.get(u, 0) or 0 for u in [_uid] + shares[_uid])
        a_bad, a_why = art_unit_check(rec, subject_flags.get(str(school["unitid"])))
        rec["art_set_implausible"] = str(school["unitid"]) in subject_flags
        if a_bad:
            rec["unranked"] = True
            rec["unranked_reason"] = join_reasons(rec["unranked_reason"], a_why)
        # v0.10 tier_and_reporting: any harvested in-scope programme gets a full
        # scorecard; only the tier is ranked. The tier is the roster's own flag,
        # or, for a merged catalog, the summed degrees against its threshold.
        _rr = roster_rec.get(_uid)
        if _rr is not None:
            _in = bool(_rr.get("in_ranking_tier"))
            if not _in and _uid in shares and tier_threshold:
                _in = rec["completions_combined"] >= tier_threshold
            rec["in_ranking_tier"] = _in
            if not _in:
                rec["unranked"] = True
                rec["unranked_reason"] = join_reasons(
                    rec["unranked_reason"],
                    f"below the {tier_threshold or 50}-degree ranking tier")
        comp = completions.get(str(school["unitid"]))
        rec["completions"] = comp
        # Raw, not collapsed: this asks whether the catalog was READ, and the
        # corpus median it is compared against is built from raw counts.
        bad, why = harvest_plausible(rec["n_courses_raw"], comp, corpus_ratio[0])
        if bad:
            rec["unranked"] = True
            rec["unranked_reason"] = join_reasons(rec["unranked_reason"], why)
        if rec["unranked"]:
            log(f"    UNRANKED — {rec['unranked_reason']}")
        # A dry run must never write a score file. With a cold cache every axis
        # aggregates to zero, and that file is indistinguishable from a real
        # score — same marker, fresh date, quotient 0.0.
        if not a.dry_run:
            with open(os.path.join(a.out_dir, f"{school['unitid']}.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2, ensure_ascii=False)
        rows.append(rec)

    log(f"\nCORPUS  {corpus} course records, {shortlisted} shortlisted "
        f"({shortlisted / max(corpus, 1):.1%})")
    log("  [stage one is tuned for recall; a high shortlist rate is expected "
        "and stage two removes the false positives]")

    if a.dry_run:
        log("\nPER-TERM CARRY  [a term carrying >40% of an axis means the axis "
            "is measuring that word]")
        for axis in rubric["axes"]:
            counts = Counter()
            for c in all_hits.get(axis, []):
                for t in which_terms(rubric, axis, course_text(c)):
                    counts[t] += 1
            tot = sum(counts.values()) or 1
            log(f"  {axis}")
            for term, n in counts.most_common(8):
                flag = "  <-- dominates" if n / tot > 0.40 else ""
                log(f"      {n / tot:5.1%}  {n:>6}  {term}{flag}")
            dead = [t for t in rubric["axes"][axis]["lexicon"]
                    if t not in counts]
            if dead:
                log(f"      dead terms ({len(dead)}): {', '.join(dead[:12])}")
        log("\n--dry-run: nothing classified, no files written")
        return

    ranked = [r for r in rows if not r.get("unranked")]
    unranked = [r for r in rows if r.get("unranked")]
    ranked.sort(key=lambda r: -r["bottega_quotient"])
    unranked.sort(key=lambda r: r["name"])
    if unranked:
        log(f"\nUNRANKED: {len(unranked)} schools below a floor — "
            f"reported with a reason, never scored low")
        for r in unranked:
            log(f"  {r['name'][:42]:<42} {r['unranked_reason']}")
    rows = ranked + unranked
    # Axis columns come from the rubric, not a literal. v0.5 adds a fifth axis
    # and the hardcoded header silently dropped it.
    axis_names = list(rubric["axes"].keys())

    # Ranked on the PERCENTAGE, not a 0-4 quartile score. A percentage is
    # absolute: an art unit at 12% venture stays at 12% when MICA lands, and a
    # new school inserts into the list without altering one existing number.
    # That turns coverage from a blocker into a stated scope.
    #
    # No composite. The axes are not orthogonal — technology and audience are
    # capacities a venture needs, not peers of it — so averaging them
    # double-counts venture. Five percentages, ranked separately.
    # No fallback to the whole-catalog share. It put schools with no art unit
    # into an art-unit ranking at roughly a fifth of their true scale.
    def pct_of(r, axis):
        share = r["axes"][axis].get(SHARE_KEY)
        return round(share * 100, 3) if share is not None else None

    # E1 (scoring chat, after the v0.9 full run): technology and practice have
    # real distributions and are ranked; venture, self and audience are
    # censuses — 58, 70 and 65 of 104 art units at zero — and are published as
    # counts, never as ranks. The rubric decides; the default ranks every axis.
    ranked_axes = [x for x in (rubric.get("publication") or {}).get(
        "ranked_axes", axis_names) if x in axis_names]
    census_axes = [x for x in axis_names if x not in ranked_axes]
    if census_axes:
        log(f"  ranked axes: {', '.join(ranked_axes)}; census axes (counts, "
            f"no rank): {', '.join(census_axes)}")

    def count_of(r, axis):
        return r["axes"][axis].get("n_matched_art")

    # v0.10 minimum_count_to_rank: a school is ranked on an axis only with at
    # least N matched courses there; below N it keeps its count and percentage
    # and is unranked on that axis alone.
    min_n = int((rubric.get("publication") or {}).get("minimum_count_to_rank", 0) or 0)
    if min_n:
        log(f"  an axis rank needs at least {min_n} matched courses on that axis")
    axis_rank = {}
    for axis in ranked_axes:
        order = sorted((r for r in ranked if (count_of(r, axis) or 0) >= min_n),
                       key=lambda r: -pct_of(r, axis))
        for pos, r in enumerate(order, 1):
            axis_rank.setdefault(r["unitid"], {})[axis] = pos

    ip = os.path.join(a.out_dir if a.index_with_scores else a.log_dir,
                      "index.csv")
    with open(ip, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        head = ["ranked", "unitid", "name"]
        for axis in axis_names:
            head += [f"pct_{axis}", f"rank_{axis}"]
        head += ["denominator", "n_courses", "n_art_courses",
                 "description_coverage", "classification_coverage",
                 "catalog_year", "rubric_version", "unranked_reason",
                 "n_courses_raw", "n_collapsed", "n_art_courses_raw",
                 "n_art_collapsed"]
        # E2: every published percentage carries its count. Appended, so no
        # existing column moves.
        head += [f"count_{axis}" for axis in axis_names]
        # A rank without its population is the most misreadable number here:
        # venture ranks 23 of 144 while technology ranks 114. Constant per axis
        # and repeated on every row on purpose, so one row reads correctly
        # alone.
        head += [f"ranked_population_{axis}" for axis in axis_names]
        w.writerow(head)
        rank_pop = {axis: sum(1 for r2 in ranked
                              if (count_of(r2, axis) or 0) >= min_n)
                    for axis in axis_names}
        log("  ranked population per axis: "
            + ", ".join(f"{ax} {rank_pop[ax]}" for ax in axis_names))
        for r in rows:
            ok = not r.get("unranked")
            row = [ok, r["unitid"], r["name"]]
            for axis in axis_names:
                row += [pct_of(r, axis) if ok else "",
                        axis_rank.get(r["unitid"], {}).get(axis, "") if ok else ""]
            row += [SHARE_LABEL, r["n_courses"], r.get("n_art_courses"),
                    r["description_coverage"],
                    r.get("classification_coverage", 1.0),
                    r["catalog_year"], r["rubric_version"],
                    r.get("unranked_reason", ""),
                    r.get("n_courses_raw"), r.get("n_collapsed"),
                    r.get("n_art_courses_raw"), r.get("n_art_collapsed")]
            row += [count_of(r, axis) if ok else "" for axis in axis_names]
            row += [rank_pop[axis] if ok else "" for axis in axis_names]
            w.writerow(row)
    log(f"\nWrote {ip}  [{SHARE_LABEL} denominator; ranks for "
        f"{', '.join(ranked_axes)}; counts beside every percentage]")
    if ranked:
        log(f"\nCOUNT CENSUS  [ranked art units, n={len(ranked)}: how many "
            f"courses each unit offers per axis]")
        log(f"  {'axis':<11} {'zero':>5} {'1':>5} {'2':>5} {'>=3':>5} {'>=5':>5}")
        for axis in axis_names:
            cs = [count_of(r, axis) or 0 for r in ranked]
            log(f"  {axis:<11} {sum(c == 0 for c in cs):>5} "
                f"{sum(c == 1 for c in cs):>5} {sum(c == 2 for c in cs):>5} "
                f"{sum(c >= 3 for c in cs):>5} {sum(c >= 5 for c in cs):>5}"
                + ("   census" if axis in census_axes else ""))
    log("  A course may carry more than one axis — sales for artists is venture")
    log("  AND audience — so a school's percentages can sum above 100%. That is")
    log("  correct, not a defect.")

    if ranked:
        log("\nSHARE DISTRIBUTION  [RANKED SCHOOLS ONLY — this is the "
            "calibration input]")
        log(f"  {'axis':<12}{'denominator':<14}{'min':>9}{'p25':>9}"
            f"{'median':>9}{'p75':>9}{'max':>9}")
        for axis in rubric["axes"]:
            for label, key in (("whole-catalog", "share_of_catalog"),
                               ("art-subject", "share_of_art_catalog")):
                vals = sorted(r["axes"][axis][key] for r in ranked
                              if r["axes"][axis].get(key) is not None
                              and not (key == "share_of_catalog" and
                                       r.get("catalog_scope") == "art_unit_only"))
                if not vals:
                    continue
                n = len(vals)
                def q(f):
                    return vals[min(int(n * f), n - 1)]
                log(f"  {axis:<12}{label:<14}{vals[0]:>9.4f}{q(.25):>9.4f}"
                    f"{q(.5):>9.4f}{q(.75):>9.4f}{vals[-1]:>9.4f}")
        log("  [quartiles of the observed distribution are the published rule. "
            "Which")
        log("   denominator defines them is editorial; both are printed so "
            "neither is")
        log("   assumed.]")

    # Compare SHARE ranks per axis, not quotient ranks. The quotients run
    # through thresholds calibrated for whole-catalog shares, and art shares are
    # roughly five times larger, so every school pins at 4.00 and the correlation
    # runs on ties. Ranks of the underlying shares are scale-free and
    # threshold-free, which is what the question actually needs.
    both = [r for r in ranked
            if r["axes"]["venture"].get("share_of_art_catalog") is not None
            and r.get("catalog_scope") != "art_unit_only"]
    if len(both) >= 3:
        log(f"\nDENOMINATOR COMPARISON  [n={len(both)}, per-axis share ranks]")
        log("  Threshold-free by construction: quotients saturate because the "
            "seed")
        log("  thresholds were set for whole-catalog shares.")
        rhos = []
        for axis in rubric["axes"]:
            whole = [r["axes"][axis]["share_of_catalog"] for r in both]
            art = [r["axes"][axis]["share_of_art_catalog"] for r in both]
            rho = rank_corr(whole, art)
            rhos.append(rho)
            log(f"  {axis:<12} spearman(whole rank, art rank) = {rho:+.3f}")
        if rhos == rhos:                       # no NaNs
            log(f"  {'MEAN':<12} {sum(rhos) / len(rhos):+.3f}")
        log("  [near 1.0 and the denominator choice does not reorder schools —")
        log("   say so in the methodology with these numbers. Far from 1.0 on")
        log("   any axis and that axis is where the choice is load-bearing.]")

        # Movement, also on ranks rather than scores.
        def mean_rank(key):
            order = {}
            for axis in rubric["axes"]:
                vals = sorted(range(len(both)),
                              key=lambda i: -both[i]["axes"][axis][key])
                for pos, i in enumerate(vals):
                    order[i] = order.get(i, 0) + pos
            return {i: v / len(rubric["axes"]) for i, v in order.items()}

        mw, ma = mean_rank("share_of_catalog"), mean_rank("share_of_art_catalog")
        moved = sorted(range(len(both)), key=lambda i: -abs(mw[i] - ma[i]))[:10]
        log("\n  MOVED MOST BY THE DENOMINATOR  [mean rank across axes]")
        for i in moved:
            r = both[i]
            log(f"    rank {mw[i]:5.1f} -> {ma[i]:5.1f}  "
                f"(art share {r.get('art_share_of_catalog') or 0:.0%})  "
                f"{r['name'][:38]}")

        # Saturation is worth naming rather than leaving to be rediscovered.
        maxed = sum(1 for r in both
                    if all(r["axes"][ax].get("score_art") == 4
                           for ax in rubric["axes"]))
        if maxed > len(both) * 0.5:
            log(f"\n  NOTE: {maxed}/{len(both)} schools score 4 on every axis "
                f"under the art denominator.")
            log("  bottega_quotient_art is saturated and carries no information "
                "until")
            log("  thresholds are calibrated separately for that denominator.")

    if RESOLVE_XREFS:
        log(f"  XREFS: {XREF_STATS[0]} resolved, {XREF_STATS[1]} with no sibling "
            f"in the corpus, {XREF_STATS[2]} left alone because the resolved "
            f"text is not cached and resolving would cost money"
            + (f", {XREF_STATS[3]} adopted uncached and billed"
               if XREF_STATS[3] else ""))
    lp = os.path.join(a.out_dir if a.index_with_scores else a.log_dir,
                      "score_run.log")
    with open(lp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LOG) + "\n")
    log(f"Wrote {lp}")


if __name__ == "__main__":
    main()
