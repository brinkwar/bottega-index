# Round 2 results (pre-registered analysis)

Coders: ['R2', 'R6']

## 1. Venture precision (T1, n=357)
both coders: 326/357 = 0.913 [0.879, 0.938]; either coder: 349/357
- R2: 327/357 = 0.916
- R6: 348/357 = 0.975

## 2. Recall among classifier negatives (T2a + T2b), Horvitz–Thompson with inclusion probabilities
- T: estimated missed ≈ 1990 [848, 3314]  (haiku_audience: 1/60, haiku_practice: 0/30, haiku_self: 0/70, haiku_technology: 6/30, haiku_venture: 1/60, neither: 3/200)
- P: estimated missed ≈ 1878 [1066, 2880]  (haiku_audience: 1/60, haiku_practice: 13/30, haiku_self: 0/70, haiku_technology: 3/30, haiku_venture: 11/60, neither: 1/200)
- V: estimated missed ≈ 1099 [233, 2232]  (haiku_audience: 7/60, haiku_practice: 0/30, haiku_self: 0/70, haiku_technology: 0/30, haiku_venture: 31/60, neither: 3/200)
- S: estimated missed ≈ 117 [50, 201]  (haiku_audience: 0/60, haiku_practice: 0/30, haiku_self: 7/70, haiku_technology: 0/30, haiku_venture: 0/60, neither: 0/200)
- A: estimated missed ≈ 110 [56, 174]  (haiku_audience: 10/60, haiku_practice: 0/30, haiku_self: 0/70, haiku_technology: 0/30, haiku_venture: 2/60, neither: 0/200)
(Recall per axis = released count ÷ (released count + estimated missed). A stratum with zero hits contributes zero to the point estimate; its Wilson upper bound × pool is reported in the write-up as the upper bound on misses there.)

## 3. Composition (T3, n=150)
coder–coder κ 0.81 [0.74, 0.88]; raw agreement 0.860
both-agree label vs classifier: 93/129
- R2 vs classifier κ 0.55
- R6 vs classifier κ 0.56

## 4. Outside-unit venture matches (T4, n=100)
precision (both Y): 92/100 = 0.920 [0.850, 0.959]
- orient (confirmed courses where coders agree, n=86): arts/creative 34, general business 52, mixed 0, cannot tell 0; coder agreement 86/92
- restr (confirmed courses where coders agree, n=90): states restriction 25, states open 6, silent 59; coder agreement 90/92
  The restriction share is a lower bound on restricted access; silence is not read as openness.

## Oracle (T5, n=58; decided rows 55)
coder–coder κ 0.81; R2 vs author κ 0.29, agreement 0.673; R6 vs author κ 0.44, agreement 0.745
both-agree label matches author on 37/50

## Industry-role board (T6, n=59; decided rows 59)
coder–coder κ 0.72; R2 vs author κ 0.08, agreement 0.542; R6 vs author κ 0.15, agreement 0.576
both-agree label matches author on 29/51

## Art unit (T7, n=80)
coder–coder κ 0.94; R2 vs pipeline κ 0.82; R6 vs pipeline κ 0.82

## 8. Test–retest (T8)
- R2: T 0.97, P 0.93, V 0.97, S 1.00, A 1.00
- R6: no round-1 record (Amendment 1: T8 reported for R2 only)
