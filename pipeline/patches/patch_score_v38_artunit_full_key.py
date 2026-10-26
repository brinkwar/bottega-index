#!/usr/bin/env python3
"""
patch_score_v38_artunit_full_key.py — corrects --art-unit-full's duplicate test.

V37 decided "this art-unit course is already in the shortlist" using the DEDUPE
key (title + first 200 characters of description). Catalogues reuse that text
across subjects: CSU San Bernardino has 197 records reading "Continuous
Enrollment for Graduate Candidacy" under ACCT, BIOL, MATH, ART and others. When
the prefilter shortlisted a non-art twin, the art-unit course was skipped — but
the classifier keys on the FULL text, which differs after 200 characters, so
that course was never classified.

Effect at stage 16: 4 distinct texts at 3 schools (10 records). All are
administrative placeholders that mark no axis, so no published count changed;
the fix exists so the claim "every described art-unit course was classified"
needs no exception.

The test now compares what the classifier compares: cache_key(course_text).

No prompt change: no signature moves. Guarded; backup score.py.pre_v38.
  python3 patch_score_v38_artunit_full_key.py --check | --apply
"""
import argparse, shutil, sys
P = "score.py"; STAMP = "SCORE_V38_FULLKEY"
OLD = '''                _have = {dedupe_key(c) for c in shortlist}
                _extra = [c for c in school["courses"]
                          if in_art(c, _art)
                          and not no_published_description(c)
                          and dedupe_key(c) not in _have]'''
NEW = '''                # SCORE_V38_FULLKEY: compare on what the classifier keys on —
                # the full course text. The dedupe key (title + first 200 chars)
                # is shared across subjects by boilerplate entries, which made
                # V37 skip art-unit courses whose non-art twin was shortlisted.
                _sig_now = prompt_signature(rubric, model)
                _have = {cache_key(course_text(c), _sig_now) for c in shortlist}
                _extra = [c for c in school["courses"]
                          if in_art(c, _art)
                          and not no_published_description(c)
                          and cache_key(course_text(c), _sig_now) not in _have]'''
OLD2 = 'MARKER = "BOTTEGA_SCORE_V37"'
NEW2 = 'MARKER = "BOTTEGA_SCORE_V38"'

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--check", action="store_true"); ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(); s = open(P, encoding="utf-8").read()
    if STAMP in s: print(f"  {P} already carries {STAMP} — refusing"); return 1
    n1, n2 = s.count(OLD), s.count(OLD2)
    print(f"  anchor 1: {n1}; anchor 2: {n2}")
    if n1 != 1 or n2 != 1: print("  NOT APPLIED"); return 1
    if not a.apply: print("  CHECK ONLY. Re-run with --apply."); return 0
    shutil.copy(P, P + ".pre_v38"); open(P, "w", encoding="utf-8").write(s.replace(OLD, NEW).replace(OLD2, NEW2))
    import py_compile; py_compile.compile(P, doraise=True)
    print(f"  applied; backup {P}.pre_v38; compiles; marker now BOTTEGA_SCORE_V38"); return 0

if __name__ == "__main__": sys.exit(main())
