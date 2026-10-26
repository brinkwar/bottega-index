#!/usr/bin/env python3
"""
patch_score_v37_artunit_full.py — adds --art-unit-full.

With the flag, every described course INSIDE a school's art unit is sent to the
classifier, whether or not the lexicon prefilter matched it. Courses outside the
art unit are still governed by the prefilter, exactly as now.

Why: stage one's recall becomes irrelevant for every published number. The
art-unit denominator figures — which are the index — no longer depend on a
lexicon having the right words, so "recall 78.8% (95% CI 63-91%)" is replaced by
"inside the art unit, no course was excluded by the prefilter." The
whole-catalogue denominator comparison still uses the prefilter and is reported
as such.

The prompt does not change: no signature moves, and every verdict already in the
cache is reused. Without the flag the scorer behaves exactly as before.

Guarded; backup score.py.pre_v37.
  python3 patch_score_v37_artunit_full.py --check | --apply
"""
import argparse, shutil, sys

P = "score.py"
STAMP = "SCORE_V37_ARTUNIT_FULL"

OLD_FLAG = '''    p.add_argument("--maker-rule", action="store_true",'''
NEW_FLAG = '''    p.add_argument("--art-unit-full", action="store_true",
                   help="classify every described course inside the art unit, "
                        "not only the lexicon prefilter's shortlist; courses "
                        "outside the art unit still go through the prefilter")
    p.add_argument("--maker-rule", action="store_true",'''

OLD_LOOP = '''        hits, shortlist = prefilter(school["courses"], matchers)
        shortlisted += len(shortlist)
'''
NEW_LOOP = '''        hits, shortlist = prefilter(school["courses"], matchers)
        if getattr(a, "art_unit_full", False):
            # SCORE_V37_ARTUNIT_FULL: inside the art unit the prefilter does not
            # decide what is seen; every described course is classified. Keyed on
            # the same dedupe key the scorer counts with, so a course already
            # shortlisted is not added twice.
            _art = art_by_school.get(str(school["unitid"])) or set()
            if _art:
                _have = {dedupe_key(c) for c in shortlist}
                _extra = [c for c in school["courses"]
                          if in_art(c, _art)
                          and not no_published_description(c)
                          and dedupe_key(c) not in _have]
                if _extra:
                    shortlist = list(shortlist) + _extra
                    log(f"    ART-UNIT FULL: +{len(_extra)} art-unit courses the "
                        f"prefilter did not match")
        shortlisted += len(shortlist)
'''

OLD_MARKER = 'MARKER = "BOTTEGA_SCORE_V36"'
NEW_MARKER = 'MARKER = "BOTTEGA_SCORE_V37"'

OLD_LOG = '''    if getattr(a, "dedupe_articles", False):'''
NEW_LOG = '''    if getattr(a, "art_unit_full", False):
        log("  ART-UNIT FULL — inside each art unit every described course is "
            "classified; the prefilter governs only the whole-catalogue scope")
    if getattr(a, "dedupe_articles", False):'''

EDITS = [(OLD_FLAG, NEW_FLAG), (OLD_LOOP, NEW_LOOP), (OLD_MARKER, NEW_MARKER), (OLD_LOG, NEW_LOG)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    s = open(P, encoding="utf-8").read()
    if STAMP in s:
        print(f"  {P} already carries {STAMP} — refusing"); return 1
    ok = True
    for i, (old, _) in enumerate(EDITS, 1):
        n = s.count(old)
        print(f"  anchor {i}: {n} match(es)")
        ok = ok and n == 1
    if not ok:
        print("  NOT APPLIED — an anchor is missing or ambiguous"); return 1
    if not a.apply:
        print("  CHECK ONLY. Re-run with --apply."); return 0
    shutil.copy(P, P + ".pre_v37")
    for old, new in EDITS:
        s = s.replace(old, new, 1)
    open(P, "w", encoding="utf-8").write(s)
    import py_compile; py_compile.compile(P, doraise=True)
    print(f"  applied {len(EDITS)} edits; backup {P}.pre_v37; compiles; marker now BOTTEGA_SCORE_V37")
    return 0


if __name__ == "__main__":
    sys.exit(main())
