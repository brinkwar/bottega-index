# Pre-registration — Bottega Index validation round 2

**Committed to https://github.com/brinkwar/bottega-index before any round-2 coding began. The commit timestamp is the registration date. Any deviation is reported as a deviation.**

## Purpose
To estimate, with human coders and under the released definitions (codebook v3, rubric v0.56), the quantities the round-1 study did not: venture precision on the full population; classifier recall among courses it marked on no axis, at corpus scale; agreement on the twelve composition categories; precision of venture matches outside art units, and their orientation and stated access; independent agreement with the author's oracle and industry-role board rulings; agreement on art-unit membership; and coder test–retest stability.

## Coders and blinding
Two coders from round 1 (R2, R4), paid a flat fee per completed set (not per item). Each codes every item independently, blind to the classifier's verdicts, to institution names, to each other, and to which sample an item came from. T1, T2a, T2b, T5, T6 and T8 are interleaved in one seeded random order on the five-axis screen and split into three sets, so neither set nor position reveals a classifier verdict or a sample; courses in more than one sample (48) appear once and are coded once. Oracle (T5) and board (T6) answers are the coder's five-axis marks: V → venture, P → practice, E → either, neither V nor P → neither. Outside-unit courses (T4), composition (T3) and subjects (T7) have their own screens. Four short practice sets (one per screen type; not analysed) precede the main sets; main sets unlock only after the author has reviewed practice exports against codebook v3. Review may correct misunderstandings of the codebook; it does not reveal answers to any main-set item.

## Samples (drawn by the pipeline from stage 17b, base seed 20261027; sealed file `round2_sealed.csv`, SHA-256 `953cb8fa6deb8337cf4942e884a65832cf4b55d830bce9577e1b03f877c2fbdd`, withheld from this repository until coding is complete because it contains the classifier's verdicts; pool sizes and inclusion probabilities in the sealed file. The draw is byte-identical when regenerated under different Python hash seeds.)
| | content | n |
|---|---|---|
| T1 | every released venture course at the 231 reporting institutions, including 62 coded in round 1 under codebook v2 (re-coded here under v3; their round-1 codes are compared separately) | 357 |
| T2a | courses a second model (Claude Haiku 4.5) marks on ≥1 axis that production marks on none, stratified by axis | 250 |
| T2b | courses neither model marks | 200 |
| T3 | random described art-unit courses | 150 |
| T4 | random outside-unit venture matches (of 1,710) | 100 |
| T5 | random rows of the 158-row disputed oracle, stratified by expected verdict (only 90 rows were eligible after excluding courses coded in round 1; the either/scope stratum had 3) | 58 |
| T6 | industry-role board rows: 30 remove, 20 keep, 9 borderline (all eligible) | 59 |
| T7 | art-unit prefixes (≥4 courses), up to ten random titles each; 60 in, 20 out | 80 |
| T8 | round-1 items coded by R2 and R4, away from every redefined boundary | 30 |

## Reference label
For binary questions, an item is positive when **both** coders mark it; disagreements are reported as such, and each estimate is also given with "either coder" as reference (an upper bound). No author adjudication of round-2 disagreements enters any estimate.

## Estimands (all with 95% intervals; Wilson for proportions, 10,000-resample bootstrap for agreement statistics, stratified by sample)
1. **Venture precision (T1):** share of the 357 that both coders mark V; and per coder.
2. **Recall among classifier negatives (T2a, T2b):** per axis, estimated missed positives in the negative pool = Σ over the two strata of (rate × pool size); recall = released count ÷ (released count + estimated missed). Reported per axis with interval; if a stratum yields zero, the interval's upper bound is reported as the upper bound on misses.
3. **Composition (T3):** two-coder Cohen's κ and Krippendorff's α over twelve categories; agreement of each coder and of the both-agree label with the classifier's category; per-category agreement where n ≥ 10.
4. **Outside-unit (T4):** precision for "teaches entrepreneurship"; among confirmed courses, the share addressed to arts/creative enterprise vs general business vs mixed; the share whose description *states* a restriction, *states* openness, or is silent. The restriction figure is reported as a lower bound on restricted access; silence is never read as openness.
5. **Oracle (T5):** each coder's four-way label (venture / practice / either / neither, derived from the five-axis marks as above) against the author's post-ruling oracle verdict; rows the oracle marks either or scope are reported separately (κ, raw agreement, confusion matrix).
6. **Industry-role board (T6):** agreement with the board's ruling, where a coder's V supports KEEP and a coder's P or neither supports REMOVE; borderline rows reported separately.
7. **Art unit (T7):** agreement between coders and with the pipeline's in/out decision.
8. **Test–retest (T8):** each coder's agreement with their own round-1 answers, per axis.

## Decision rules fixed in advance
- Reported figures in Papers A, B and C will be revised to the round-2 estimates where they replace a round-1 figure; no released count is changed by round-2 results. Released labels remain the classifier's, after the published boards.
- If T1 venture precision (both-agree) is below 0.80, Paper A reports venture counts with the precision-adjusted figure beside them.
- If estimated missed venture positives exceed 25% of the released venture count at the point estimate, Paper A states venture counts as lower bounds in the abstract.
- If T4 precision is below 0.70, the 1,710 figure is reported only as a keyword match count, not as entrepreneurship courses.
- If T5 or T6 agreement with the author falls below κ = 0.60, the corresponding rulings are described as contested in all three papers.
- Every result is reported whatever its direction, including results bearing on the author's hypotheses that outside-unit courses are general-business in orientation and restricted in access.

## Analysis
`round2_analysis.py` in the repository, committed with this document, computes every estimand above from the sealed file and the coders' exports. It is run once, after both coders' exports are complete.
