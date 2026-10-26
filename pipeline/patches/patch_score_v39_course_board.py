#!/usr/bin/env python3
"""
patch_score_v39_course_board.py — adds --course-board FILE.

A course-level board rules individual courses on one axis: REMOVE takes the
axis off the course, ADD puts it on. It is applied AFTER classification and
exclusivity and BEFORE counting, so:

  * the classifier's cached verdicts are never changed — the cache still says
    what the model said;
  * the board is not in any prompt, so no signature moves;
  * every override is recorded on the school's scorecard
    (`course_board_applied`) and counted in the run log — never silent;
  * a board row that matches no course at its school is reported as
    UNMATCHED, and the run says so loudly at the end.

Board file: CSV with unitid, code, title, axis, action (REMOVE|ADD), board.
Rows are matched on unitid + code (whitespace-normalised) + title.

Practice is not hand-adjusted by any board here (rubric v0.55): a REMOVE on
venture takes venture off and assigns nothing in its place.

Guarded; backup score.py.pre_v39.
  python3 patch_score_v39_course_board.py --check | --apply
"""
import argparse, shutil, sys

P = "score.py"
STAMP = "SCORE_V39_COURSE_BOARD"

HELPER = '''
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


def main():'''

EDITS = [
    ('\n\ndef main():', HELPER),
    ('''    p.add_argument("--art-unit-full", action="store_true",''',
     '''    p.add_argument("--course-board", default="",
                   help="CSV of course-level rulings (unitid, code, title, axis, "
                        "action REMOVE|ADD, board), applied after classification "
                        "and recorded on each scorecard")
    p.add_argument("--art-unit-full", action="store_true",'''),
    ('''    art_by_school = load_art_subjects(a.subject_dir)
    report_maker_log()
    subject_flags = load_subject_flags(a.subject_dir)
''',
     '''    art_by_school = load_art_subjects(a.subject_dir)
    report_maker_log()
    subject_flags = load_subject_flags(a.subject_dir)
    global COURSE_BOARD
    if getattr(a, "course_board", ""):
        COURSE_BOARD = load_course_board(a.course_board)
        log(f"  COURSE BOARD — {sum(len(v) for v in COURSE_BOARD.values())} rulings at "
            f"{len(COURSE_BOARD)} schools from {a.course_board}; applied after "
            f"classification, recorded on each scorecard")
'''),
    ('''        rec = aggregate(school, shortlist, results, rubric, model,
                        evidence_cap=a.evidence_cap,''',
     '''        _cb = apply_course_board(school, shortlist, results,
                                 prompt_signature(rubric, model),
                                 COURSE_BOARD.get(str(school["unitid"])))
        if _cb:
            log(f"    course board: {len(_cb)} ruling(s) applied")
        rec = aggregate(school, shortlist, results, rubric, model,
                        evidence_cap=a.evidence_cap,'''),
    ('''        if rec["n_collapsed"] or rec.get("n_art_collapsed"):''',
     '''        if _cb:
            rec["course_board_applied"] = _cb
        if rec["n_collapsed"] or rec.get("n_art_collapsed"):'''),
    ('MARKER = "BOTTEGA_SCORE_V38"', 'MARKER = "BOTTEGA_SCORE_V39"'),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true"); ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    s = open(P, encoding="utf-8").read()
    if STAMP in s:
        print(f"  {P} already carries {STAMP} — refusing"); return 1
    ok = True
    for i, (old, _) in enumerate(EDITS, 1):
        n = s.count(old); print(f"  anchor {i}: {n} match(es)"); ok = ok and n == 1
    if not ok:
        print("  NOT APPLIED — an anchor is missing or ambiguous"); return 1
    if not a.apply:
        print("  CHECK ONLY. Re-run with --apply."); return 0
    shutil.copy(P, P + ".pre_v39")
    for old, new in EDITS:
        s = s.replace(old, new, 1)
    open(P, "w", encoding="utf-8").write(s)
    import py_compile; py_compile.compile(P, doraise=True)
    print(f"  applied {len(EDITS)} edits; backup {P}.pre_v39; compiles; marker now BOTTEGA_SCORE_V39")
    return 0


if __name__ == "__main__":
    sys.exit(main())
