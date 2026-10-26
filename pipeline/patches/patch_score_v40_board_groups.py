#!/usr/bin/env python3
"""
patch_score_v40_board_groups.py — a course-board ruling applies to the whole
counted course, not only the record it names.

The boards rule COUNTED courses (S5 exported one record per dedupe group). V39
applied each ruling to the single record named, so when a school lists the same
course twice under different codes — ArtCenter ART 426 / ART 506 "Theories of
Construction", UNR ART 450 / ART 650, WashU ART 5015/5025 and 6015/6025 — the
unnamed twin kept its own verdict and the counted course stayed counted. Stage
17 under V39 therefore removed 69 self courses, not the 73 the board ruled.

V40 expands each ruling to every record at the school sharing the named
record's dedupe key (the scorer's own collapse rule), so the ruling and the
count act on the same unit. Every expanded record is still recorded on the
scorecard. No prompt change; no signature moves. Backup score.py.pre_v40.
"""
import argparse, shutil, sys
P = "score.py"; STAMP = "SCORE_V40_BOARD_GROUPS"
OLD = '''        for c in hits:
            key = cache_key(course_text(c), sig)
            axes = list(results.get(key) or [])'''
NEW = '''        # SCORE_V40_BOARD_GROUPS: rule the counted course — every record at the
        # school in the named record's dedupe group, as the scorer collapses them.
        _grp = {dedupe_key(h) for h in hits}
        hits = [c for c in shortlist if dedupe_key(c) in _grp]
        for c in hits:
            key = cache_key(course_text(c), sig)
            axes = list(results.get(key) or [])'''
OLD2 = 'MARKER = "BOTTEGA_SCORE_V39"'; NEW2 = 'MARKER = "BOTTEGA_SCORE_V40"'

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--check", action="store_true"); ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(); s = open(P, encoding="utf-8").read()
    if STAMP in s: print(f"  {P} already carries {STAMP} — refusing"); return 1
    n1, n2 = s.count(OLD), s.count(OLD2); print(f"  anchor 1: {n1}; anchor 2: {n2}")
    if n1 != 1 or n2 != 1: print("  NOT APPLIED"); return 1
    if not a.apply: print("  CHECK ONLY. Re-run with --apply."); return 0
    shutil.copy(P, P + ".pre_v40"); open(P, "w", encoding="utf-8").write(s.replace(OLD, NEW).replace(OLD2, NEW2))
    import py_compile; py_compile.compile(P, doraise=True)
    print(f"  applied; backup {P}.pre_v40; compiles; marker now BOTTEGA_SCORE_V40"); return 0

if __name__ == "__main__": sys.exit(main())
