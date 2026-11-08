# Amendment 4 — single-coder census of the Haiku-flagged venture pool

**Committed after the round-2 results (commit on GitHub, results_round2/) and before R6 coded any item in this census. This is an analysis added after seeing the round-2 results; it is reported separately from, and does not replace, the pre-registered analysis.**

## Why
Round 2 estimated that the classifier misses venture courses, and the largest identifiable concentration of misses is the pool of courses Claude Haiku 4.5 marks venture and the released classifier does not (317 at the 231 reporting institutions; in round 2 both coders confirmed venture on 31 of 60 sampled). Reading the whole remaining pool replaces that stratum's estimate with a count and lets each confirmed course be added to its institution's venture count.

## Design
- **Items:** every remaining course in that pool not coded in any earlier round (expected 257), plus 50 randomly drawn ordinary art-unit courses, shuffled together so the set is blind; sealed file SHA-256 recorded in `census_draw_log.json` before coding.
- **Coder:** R6 alone, five-axis screen, codebook v3, no access to classifier verdicts.
- **Justification for one coder:** R6's venture agreement with R2 is established on 906 shared round-2 courses (κ 0.88), and his calibration on this exact pool is measured: in the round-2 sample of it, R2 also marked venture on 31 of R6's 36 venture calls (0.86).

## Reporting (fixed now)
1. **R6 count:** courses in the pool R6 marks venture.
2. **Calibrated count:** R6 count × 0.86 (with the interval from 31/36), reported beside the R6 count.
3. **Index update:** each course R6 marks venture is added to its institution's venture count as a human-confirmed addition, recorded as a published board ("venture census board") with the coder's mark per row; institution zero-counts are recomputed and reported both before and after.
4. **Recall estimate:** the round-2 recall estimate is recomputed with this stratum's estimate replaced by the calibrated count; the other strata are unchanged.
5. Decoy courses are not analysed except to report how often R6 marked venture among them.
