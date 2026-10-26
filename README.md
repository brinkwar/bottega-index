# bottega-index

Code for **The Bottega Index, 2026 edition** — a census of what American art
and design programmes teach, built by reading every course in their published
catalogues.

**Data:** [10.5281/zenodo.23573815](https://doi.org/10.5281/zenodo.23573815) — the index, per-institution
scorecards, composition, boards, the full methods package and the audit log
(CC BY 4.0). This repository holds the code (MIT). Each GitHub release is
archived by Zenodo under its own software DOI.

## What is here

| path | contents |
|---|---|
| `pipeline/score.py` | the scorer that produced the edition (`BOTTEGA_SCORE_V40`): subject and course classification, art-unit determination, maker rule, dedupe, cross-reference resolution, course-level boards, scoring |
| `pipeline/harvest.py`, `pipeline/discover.py` | catalogue discovery and harvesting (CourseLeaf, acalog, Coursedog, SmartCatalog, Clean Catalog, PDF) |
| `pipeline/test_score.py` | the test suite |
| `pipeline/rubric_v0.56.json` | the production rubric; renders course signature `32a672745485881f` |
| `pipeline/patches/` | the guarded patches V35–V40, in order, each documenting what it changed and why |
| `study/` | the reliability study: sample draw, coder-tool builder and template, κ and precision |

## Run the tests

Python 3.9 or later. The pipeline uses only the standard library.

```
cd pipeline
python3 test_score.py
```

It ends with `All checks passed.` No API key or network access is needed.

## Reproducing the edition

The edition was scored with:

```
cd pipeline
python3 score.py --rubric rubric_v0.56.json --art-unit-full \
  --course-board stage17_course_board.csv --maker-rule --dedupe-articles \
  --withhold-blank --resolve-xrefs --resolve-uncached \
  --out-dir data/scores_v25_stage17b
```

`stage17_course_board.csv` is in the data deposit (`index/`). Three things to
know before trying:

- **Catalogue text is not redistributed**, here or in the deposit. Re-running
  means re-harvesting, and catalogues change between academic years, so a fresh
  harvest reproduces the method, not the 2026 numbers. The published numbers
  are in the deposit and are the authority.
- **Classification calls the Anthropic API** (`claude-sonnet-4-6`, a pinned
  snapshot, temperature 0) and needs `ANTHROPIC_API_KEY` set in your
  environment. Keys are never stored in this repository. Price any run with
  `--dry-run` first.
- **The prompt is identified by its signature.** Under `rubric_v0.56.json` the
  course signature must read `32a672745485881f`; a different value means a
  different prompt.

## The reliability study (`study/`)

Requires pandas (`pip install -r requirements.txt`).

| script | usage |
|---|---|
| `draw_sample.py` | `python3 draw_sample.py index.csv jsons_dir disputed_158.csv lexicon_129.csv composition_courses.csv out_dir [seed]` — draws the four-stratum sample and calibration set (default seed 20260917), writes the public item files and the sealed verdict file with its SHA-256 |
| `build_tool.py` | `python3 build_tool.py tool_template.html items.csv calibration\|main codebook_url out.html` — builds a coder page from `tool_template.html`; embeds only item id, code, title and description |
| `kappa.py` | `python3 kappa.py sealed_verdicts.csv coding_<set>_<coder>.json [...] > report.md` — Fleiss' κ, pairwise Cohen's κ with PABAK, coder–classifier agreement, per axis and stratum |
| `s5_precision.py` | `python3 s5_precision.py s5_sealed.csv coding_supplementary_R1.json [...]` — precision of the self and audience positives with bootstrap 95% CIs |

Built coder pages embed course descriptions and are deliberately not committed
(`.gitignore`); only the template is public.

## Citing

Cite the data as: Warren, B. (2026). *The Bottega Index, 2026 edition*
(Version 1.0.0) [Data set]. Art Morph Institute.
https://doi.org/10.5281/zenodo.23573815 — and the code via `CITATION.cff`.

## Licence

Code: MIT (`LICENSE`). Data: CC BY 4.0, in the deposit.
