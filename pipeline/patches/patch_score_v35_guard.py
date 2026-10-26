#!/usr/bin/env python3
"""
patch_score_v35_guard.py — the maker rule's performing-arts guard reads its
word list and its rule from the rubric (v0.49 maker_rule.performing_arts_guard)
instead of the hard-coded _PERF pattern.

Rule (rubric v0.49): a prefix is refused when the MAJORITY of its titles match
the guard's terms. Majority = strictly more than half. Terms compile as
word-start, case-insensitive matches (\\b + term), which is what 'choreograph'
needs; the choice is recorded in the log line of every refusal.

If the rubric carries no performing_arts_guard, the old behaviour (hard-coded
pattern, share >= 0.30) is kept, so older rubrics reproduce exactly.

No prompt is touched: no signature moves. Guarded; backup score.py.pre_v35.
  python3 patch_score_v35_guard.py --check
  python3 patch_score_v35_guard.py --apply
"""
import argparse, shutil, sys
P = "score.py"; STAMP = "SCORE_V35_GUARD"
OLD = '''        perf = sum(1 for t in titles if _PERF.search(t)) / len(titles)
        if perf >= MAKER_PERF_GUARD:
'''
NEW = '''        # SCORE_V35_GUARD: the guard's terms and rule are published in the
        # rubric (v0.49 maker_rule.performing_arts_guard); majority, not presence.
        _g = block.get("performing_arts_guard") or {}
        if _g.get("terms"):
            _gp = __import__("re").compile(
                r"\\b(?:" + "|".join(__import__("re").escape(t) for t in _g["terms"]) + ")",
                __import__("re").I)
            perf = sum(1 for t in titles if _gp.search(t)) / len(titles)
            _refuse = perf > 0.5
        else:
            perf = sum(1 for t in titles if _PERF.search(t)) / len(titles)
            _refuse = perf >= MAKER_PERF_GUARD
        if _refuse:
'''
OLD2 = '''MARKER = "BOTTEGA_SCORE_V34"'''
NEW2 = '''MARKER = "BOTTEGA_SCORE_V35"'''

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--check", action="store_true"); ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(); s = open(P, encoding="utf-8").read()
    if STAMP in s: print(f"  {P} already carries {STAMP} — refusing"); return 1
    n1, n2 = s.count(OLD), s.count(OLD2)
    print(f"  guard anchor: {n1} match(es); marker anchor: {n2} match(es)")
    if n1 != 1 or n2 != 1: print("  NOT APPLIED"); return 1
    if not a.apply: print("  CHECK ONLY. Re-run with --apply."); return 0
    shutil.copy(P, P + ".pre_v35")
    s = s.replace(OLD, NEW).replace(OLD2, NEW2)
    open(P, "w", encoding="utf-8").write(s)
    import py_compile; py_compile.compile(P, doraise=True)
    print(f"  applied; backup {P}.pre_v35; compiles; marker now BOTTEGA_SCORE_V35"); return 0

if __name__ == "__main__": sys.exit(main())
