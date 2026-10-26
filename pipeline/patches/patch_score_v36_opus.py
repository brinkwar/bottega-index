#!/usr/bin/env python3
"""
patch_score_v36_opus.py — omit `temperature` for models that reject it.

Claude Opus 5 returns HTTP 400: "`temperature` is deprecated for this model."
The scorer sends temperature: 0 on every request, which is why the earlier
model-sensitivity run lost every batch. This sends temperature only to models
that accept it, leaving the pinned model's requests byte-identical.

No prompt text changes: no signature moves. Guarded; backup score.py.pre_v36.
  python3 patch_score_v36_opus.py --check | --apply
"""
import argparse, shutil, sys
P = "score.py"; STAMP = "SCORE_V36_OPUS"
OLD = '''        "max_tokens": 8000,
        "temperature": 0,
'''
NEW = '''        "max_tokens": 8000,
        # SCORE_V36_OPUS: some models reject `temperature` outright
        # ("`temperature` is deprecated for this model", HTTP 400). Send it
        # only where it is accepted; the pinned model's request is unchanged.
        **({} if str(model).startswith(("claude-opus-5", "claude-mythos", "claude-fable"))
           else {"temperature": 0}),
'''
OLD2 = 'MARKER = "BOTTEGA_SCORE_V35"'
NEW2 = 'MARKER = "BOTTEGA_SCORE_V36"'

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--check", action="store_true"); ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(); s = open(P, encoding="utf-8").read()
    if STAMP in s: print(f"  {P} already carries {STAMP} — refusing"); return 1
    n1, n2 = s.count(OLD), s.count(OLD2)
    print(f"  request-body anchor: {n1}; marker anchor: {n2}")
    if n1 != 1 or n2 != 1: print("  NOT APPLIED"); return 1
    if not a.apply: print("  CHECK ONLY. Re-run with --apply."); return 0
    shutil.copy(P, P + ".pre_v36"); open(P, "w", encoding="utf-8").write(s.replace(OLD, NEW).replace(OLD2, NEW2))
    import py_compile; py_compile.compile(P, doraise=True)
    print(f"  applied; backup {P}.pre_v36; compiles; marker now BOTTEGA_SCORE_V36"); return 0

if __name__ == "__main__": sys.exit(main())
