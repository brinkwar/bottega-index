#!/usr/bin/env python3
"""Offline tests for score.py. No API calls."""
import json
import re, re, sys, tempfile, os
import score as S

fails = []
def check(label, got, want):
    if got != want: fails.append(f"{label}: got {got!r}, want {want!r}")
    else: print(f"  ok  {label}")

RUBRIC = json.load(open("rubric_v0.2.json"))
RV = RUBRIC["rubric_version"]
M = S.build_matchers(RUBRIC)

def c(code, title, desc=""):
    return {"code": code, "title": title, "description": desc, "tier": 1}

print("\n-- prefilter recall")
CANDIDATES = [
    c("ART 240", "Business of Art", "Pricing, contracts and licensing for artists."),
    c("ART 310", "Creative Coding", "Generative and algorithmic image making."),
    c("ART 150", "Creative Process", "Ideation, problem finding and deliberate practice."),
    c("ART 400", "Exhibition Practice", "Curatorial work and the artist statement."),
    c("ART 101", "Drawing I", "Observational drawing in graphite and charcoal."),
]
hits, short = S.prefilter(CANDIDATES, M)
check("venture matched", "ART 240" in [x["code"] for x in hits["venture"]], True)
check("technology matched", "ART 310" in [x["code"] for x in hits["technology"]], True)
check("self matched", "ART 150" in [x["code"] for x in hits["self"]], True)
check("audience matched", "ART 400" in [x["code"] for x in hits["audience"]], True)
check("plain studio course not shortlisted",
      "ART 101" in [x["code"] for x in short], False)
check("shortlist is deduplicated", len(short), 4)
check("candidate axes recorded on the course",
      "venture" in short[0]["_candidate_axes"], True)

print("\n-- prefilter is recall-oriented, not precise")
LOOSE = [c("ART 500", "Senior Studio",
           "Students exhibit work in the annual show and document the results.")]
_, ls = S.prefilter(LOOSE, M)
check("loose mention still shortlists (stage two removes it)", len(ls), 1)

print("\n-- word boundaries: the strings that broke v0.1")
# Pulled from real harvested descriptions. Every one of these matched under the
# old bare-substring matcher; each must now match nothing.
FALSE_POSITIVES = [
    c("ART 101", "Painting I", "Observational painting in oil."),
    c("ART 102", "Ceramics", "Wheel throwing, available to majors."),
    c("ART 103", "Figure Drawing", "Sustained study of the human figure; chair provided."),
    c("BIO 210", "Systematics", "Introduction to biological taxonomy and nomenclature."),
    c("MUS 140", "Ear Training", "Aural skills training and sight singing."),
    c("ENG 220", "Grammar", "Contraction, elision and register in written English."),
    c("SOC 130", "Urban Life", "Community formation in the modern city."),
    c("LIB 101", "Research", "Locating articles in a magazine or journal database."),
]
_, fp = S.prefilter(FALSE_POSITIVES, M)
check("no ordinary catalog prose shortlists", [x["code"] for x in fp], [])
for course in FALSE_POSITIVES:
    t = S.course_text(course)
    hit = [a for a, rx in M.items() if rx.search(t)]
    check(f"  {course['title']!r} matches nothing", hit, [])

print("\n-- KNOWN CARRY: asserted against the lexicon, not a fixed expectation")
# These were bugs in the lexicon, not the matcher, and the rubric belongs to the
# scoring chat. Rather than pin an expectation that breaks on every rubric bump,
# derive it: a bare term should still carry, a narrowed one should not. The test
# then reports which rubric it is looking at instead of failing.
CARRY = [
    ("audience", "distribution",
     c("GEN 100", "World Art",
       "Satisfies the general education distribution requirement.")),
    ("technology", "unity",
     c("ART 104", "Design Fundamentals",
       "Composition, balance and unity of the picture plane.")),
]
for axis, term, course in CARRY:
    bare = term in [t.strip().lower() for t in RUBRIC["axes"][axis]["lexicon"]]
    hit = bool(M[axis].search(S.course_text(course)))
    label = "still carries" if bare else "no longer carries (narrowed)"
    check(f"  {term!r} {label} {course['title']!r}", hit, bare)

print("\n-- word boundaries: real terms still match")
TRUE_POSITIVES = [
    ("venture", c("A", "Negotiating Commissions", "Students practice negotiation.")),
    ("venture", c("B", "Careers in Design", "Preparing for careers in the field.")),
    ("technology", c("C", "AI in the Studio", "Students use AI tools to generate imagery.")),
    ("technology", c("D", "Computational Form", "Computational design methods.")),
    ("self", c("E", "Contemplative Practice", "Contemplation and studio ritual.")),
    ("audience", c("F", "Exhibition Design", "Curating and exhibiting student work.")),
]
for axis, course in TRUE_POSITIVES:
    t = S.course_text(course)
    check(f"  {course['title']!r} -> {axis}", bool(M[axis].search(t)), True)

print("\n-- term_regex")
check("plain term is boundaried", bool(re.search(S.term_regex("tax"), "taxonomy")), False)
check("plain term matches itself", bool(re.search(S.term_regex("tax"), "the tax code")), True)
check("star means prefix", bool(re.search(S.term_regex("negotiat*"), "negotiating")), True)
check("star does not match mid-word",
      bool(re.search(S.term_regex("negotiat*"), "renegotiating")), False)
check("multiword term works",
      bool(re.search(S.term_regex("business of art"), "the business of art")), True)
check("empty term returns None", S.term_regex("*"), None)

print("\n-- cache keys track the PROMPT, not the rubric version")
import copy as _copy
SIG = S.prompt_signature(RUBRIC, "m")
check("same input, same key",
      S.cache_key("ART 240. Business of Art.", SIG),
      S.cache_key("ART 240. Business of Art.", SIG))
check("different text, different key",
      S.cache_key("a", SIG) == S.cache_key("b", SIG), False)
check("model changes the signature",
      S.prompt_signature(RUBRIC, "m2") == SIG, False)

# The whole point: lexicon edits must not invalidate paid classifications.
lex = _copy.deepcopy(RUBRIC)
lex["axes"]["audience"]["lexicon"] = [
    t for t in lex["axes"]["audience"]["lexicon"] if t != "distribution"]
lex["axes"]["technology"]["lexicon"] = [
    t for t in lex["axes"]["technology"]["lexicon"] if t != "unity"]
lex["rubric_version"] = "0.3"
check("dropping 'distribution' and 'unity' and bumping to 0.3 keeps the key",
      S.prompt_signature(lex, "m"), SIG)
check("thresholds are not prompt-affecting either",
      S.prompt_signature(
          {**RUBRIC, "axes": {a: {**sp, "thresholds": [9, 9, 9, 9]}
                              for a, sp in RUBRIC["axes"].items()}}, "m"), SIG)

# But anything the prompt reads must invalidate.
for field, value in [("question", "A different question entirely?"),
                     ("name", "Renamed Axis"),
                     ("includes", ["something new"]),
                     ("excludes", ["something new"])]:
    ch = _copy.deepcopy(RUBRIC)
    ch["axes"]["venture"][field] = value
    check(f"changing {field!r} invalidates",
          S.prompt_signature(ch, "m") == SIG, False)

print("\n-- response parsing never trusts the shape")
V = set(RUBRIC["axes"])
check("plain json parsed",
      S.parse_response('{"0": ["venture"], "1": []}', 2, V), [["venture"], []])
check("code fences stripped",
      S.parse_response('```json\n{"0": ["self"]}\n```', 1, V), [["self"]])
check("preamble tolerated",
      S.parse_response('Here you go: {"0": ["audience"]}', 1, V), [["audience"]])
check("invented category dropped",
      S.parse_response('{"0": ["venture", "nonsense"]}', 1, V), [["venture"]])
check("missing index becomes empty", S.parse_response('{"0": ["self"]}', 2, V),
      [["self"], []])
check("wrong type becomes empty", S.parse_response('{"0": "venture"}', 1, V), [[]])
check("unparseable returns None", S.parse_response("sorry, I cannot", 1, V), None)
check("empty returns None", S.parse_response("", 1, V), None)

print("\n-- scoring from share")
TH = [0.005, 0.015, 0.035, 0.07]
check("below the floor scores 0", S.score_from_share(0.001, TH), 0)
check("first threshold scores 1", S.score_from_share(0.005, TH), 1)
check("mid range scores 2", S.score_from_share(0.02, TH), 2)
check("top threshold scores 4", S.score_from_share(0.2, TH), 4)
check("exactly at a threshold counts", S.score_from_share(0.035, TH), 3)

print("\n-- aggregate")
school = {"unitid": "1", "name": "Test", "n_courses": 100,
          "description_coverage": 0.9, "platform": "courseleaf",
          "catalog_year": "2026-2027"}
res = {S.cache_key(S.course_text(x), S.prompt_signature(RUBRIC, "m")): ["venture"] for x in [CANDIDATES[0]]}
agg = S.aggregate(school, [CANDIDATES[0]], res, RUBRIC, "m")
check("matched course counted", agg["axes"]["venture"]["n_matched"], 1)
check("share computed against whole catalog",
      agg["axes"]["venture"]["share_of_catalog"], 0.01)
check("unmatched axis scores 0", agg["axes"]["self"]["score"], 0)
check("evidence carried per axis",
      agg["axes"]["venture"]["courses"][0]["code"], "ART 240")
check("rubric version recorded", agg["rubric_version"], RV)
check("prompt signature recorded", agg["prompt_signature"],
      S.prompt_signature(RUBRIC, "m"))
check("quotient is the mean of four axes",
      agg["bottega_quotient"], round(sum(
          agg["axes"][x]["score"] for x in RUBRIC["axes"]) / 4, 2))

print("\n-- coverage floors are enforced, not advisory")
check("floor is a named constant", S.COVERAGE_FLOOR, 0.70)
check("both coverages good -> ranked", S.floor_check(0.95, 1.0)[0], False)
check("thin descriptions -> unranked", S.floor_check(0.15, 1.0)[0], True)
check("reason names description coverage",
      "description coverage" in S.floor_check(0.15, 1.0)[1], True)
check("failed classification -> unranked", S.floor_check(0.99, 0.40)[0], True)
check("reason names classification coverage",
      "classification coverage" in S.floor_check(0.99, 0.40)[1], True)
check("both failing -> both reasons given",
      S.floor_check(0.10, 0.10)[1].count(";"), 1)
check("exactly at the floor is ranked", S.floor_check(0.70, 0.70)[0], False)
check("Hawaii at 0% is unranked", S.floor_check(0.0, 1.0)[0], True)

print("\n-- evidence is capped but never silently")
# Distinct titles: 600 copies of ONE course are one course under the V14
# collapse, which would test the dedupe rather than the cap.
MANY = [dict(CANDIDATES[0], code=f"ART {i}", title=f"Business of Art {i}")
        for i in range(600)]
res_many = {S.cache_key(S.course_text(x), S.prompt_signature(RUBRIC, "m")): ["venture"]
            for x in MANY}
school_many = {"unitid": "2", "name": "Big", "n_courses": 6000,
               "description_coverage": 0.9, "platform": "courseleaf",
               "catalog_year": "2026-2027"}
big = S.aggregate(school_many, MANY, res_many, RUBRIC, "m", evidence_cap=500)
v = big["axes"]["venture"]
check("n_matched reports the true count", v["n_matched"], 600)
check("evidence list is capped", len(v["courses"]), 500)
check("evidence_shown matches the list", v["evidence_shown"], 500)
check("truncation is declared", v["evidence_truncated"], True)
check("cap is recorded", v["evidence_cap"], 500)
small = S.aggregate(school, [CANDIDATES[0]], res, RUBRIC, "m")
check("no truncation when under the cap",
      small["axes"]["venture"]["evidence_truncated"], False)
check("subject carried into evidence",
      "subject" in small["axes"]["venture"]["courses"][0], True)
check("floor recorded on the record", small["coverage_floor"], 0.70)

print("\n-- art-subject denominator: prefixes are local, not corpus-wide")
SCHOOL_ART = {"unitid": "217493", "name": "RISD", "n_courses": 10,
              "description_coverage": 1.0, "platform": "coursedog",
              "catalog_year": "2026-2027",
              "courses": [
                  {"code": "ILLUS 1795", "subject": "ILLUS",
                   "title": "Crafting the Corset", "description": "x" * 60},
                  {"code": "ILLUS 2110", "subject": "ILLUS",
                   "title": "Professional Portfolio", "description": "x" * 60},
                  {"code": "HPSS 1010", "subject": "HPSS",
                   "title": "History of Art", "description": "x" * 60},
              ]}
rows = S.subject_rows(SCHOOL_ART)
check("prefixes counted", [(r["subject"], r["n"]) for r in rows],
      [("ILLUS", 2), ("HPSS", 1)])
check("samples carried", rows[0]["samples"][0], "Crafting the Corset")
check("prompt names the school", "RISD" in S.subject_prompt("RISD", rows), True)
check("prompt lists the CIP scope",
      "50.07" in S.subject_prompt("RISD", rows), True)
SP = S.subject_prompt("RISD", rows)
for term in ["music", "theatre", "dance", "arts administration", "conservatory",
             "audition", "ensemble", "choreography"]:
    check(f"  subject prompt names {term!r} as excluded", term in SP.lower(), True)
check("exclusion is stated as absolute", "without exception" in SP.lower(), True)
check("subject signature differs from the course signature",
      S.subject_signature("m") == S.prompt_signature(RUBRIC, "m"), False)

print("\n-- subject response parsing")
V = {"ILLUS", "HPSS"}
# V18: verdicts are statuses. Legacy booleans still parse, as art/out.
check("plain json", S.parse_subject_response('{"ILLUS": true, "HPSS": false}', V),
      {"ILLUS": "art", "HPSS": "out"})
check("fences stripped",
      S.parse_subject_response('```json\n{"ILLUS": true}\n```', V), {"ILLUS": "art"})
check("unknown prefix dropped",
      S.parse_subject_response('{"ILLUS": true, "NOPE": true}', V), {"ILLUS": "art"})
check("truthy coerced to bool",
      S.parse_subject_response('{"ILLUS": 1}', V), {"ILLUS": "art"})
check("unparseable -> None", S.parse_subject_response("sorry", V), None)
check("list -> None", S.parse_subject_response("[1,2]", V), None)

print("\n-- both denominators computed, neither chosen")
resA = {S.cache_key(S.course_text(c), S.prompt_signature(RUBRIC, "m")): ["venture"]
        for c in SCHOOL_ART["courses"][:1]}
agg2 = S.aggregate(SCHOOL_ART, SCHOOL_ART["courses"], resA, RUBRIC, "m",
                   art_subjects={"ILLUS"})
v = agg2["axes"]["venture"]
check("art denominator counted", agg2["n_art_courses"], 2)
check("art share of catalog", agg2["art_share_of_catalog"], 0.2)
check("whole-catalog share uses n_courses", v["share_of_catalog"], 0.1)
check("art share uses the art denominator", v["share_of_art_catalog"], 0.5)
check("both quotients present",
      agg2["bottega_quotient"] is not None
      and agg2["bottega_quotient_art"] is not None, True)
agg3 = S.aggregate(SCHOOL_ART, SCHOOL_ART["courses"], resA, RUBRIC, "m")
check("no art set -> art fields are None", agg3["n_art_courses"], None)
check("no art set -> art quotient is None", agg3["bottega_quotient_art"], None)
check("whole-catalog score unaffected by the art set",
      agg3["bottega_quotient"], agg2["bottega_quotient"])

print("\n-- implausible art shares are flagged, not scored")
check("comprehensive at 95% of 8000 courses is flagged for review",
      bool(S.art_share_check(0.95, 7600, 8000)[2]), True)
check("art college at 81% of 1293 is fine",
      S.art_share_check(0.807, 1044, 1293)[0], False)
check("large catalog rule does not fire on small ones",
      S.art_share_check(0.95, 950, 1000)[2], [])
# v0.13: ratios are review triggers, counts are published reasons.
check("a high art share is a REVIEW flag, not a reason",
      (S.art_share_check(0.95, 7600, 8000)[0],
       bool(S.art_share_check(0.95, 7600, 8000)[2])), (False, True))
check("Maryland: 86 courses at 1.5% is held back by the COUNT",
      S.art_share_check(0.015, 86, 5770)[0], True)
check("and its published reason is the floor, not a diagnosis",
      S.art_share_check(0.015, 86, 5770)[1],
      "art-subject set below the 100-course floor (86 courses)")
check("the low share is recorded as review only",
      "read the prefix list" in S.art_share_check(0.015, 86, 5770)[2][0], True)
check("no published reason says classification failed unless it did",
      "classification failed" in S.art_share_check(0.015, 86, 5770)[1], False)
check("Penn State: zero art prefixes in a 299-course catalog IS a failure",
      S.art_share_check(0.0, 0, 299)[1],
      "no art-subject prefix identified in a 299-course catalog; "
      "subject classification failed")
check("UTSA likewise: 63 prefixes, none art",
      "classification failed" in S.art_share_check(0.0, 0, 1864, 63)[1], True)
check("and it counts the prefixes, which is what was judged",
      S.art_share_check(0.0, 0, 1864, 63)[1],
      "no art-subject prefix identified among 63 subject prefixes; "
      "subject classification failed")
# Missouri: 0 of ONE prefix in 283 courses. One department captured is a harvest
# failure; nothing was classified wrongly because nothing was there to classify.
check("Missouri: 0 of 1 prefix is NOT a classification failure",
      "classification failed" in S.art_share_check(0.0, 0, 283, 1)[1], False)
check("and its reason names the prefixes, in plain English",
      S.art_share_check(0.0, 0, 283, 1)[1],
      "no art-subject prefix identified; only 1 subject prefix harvested")
check("plural when there are several", "2 subject prefixes harvested" in
      S.art_share_check(0.0, 0, 20, 2)[1], True)

print("\n-- V32: a published reason states each fact once")
check("the duplicate course count is gone",
      S.join_reasons("no art-subject prefix identified; only 9 courses harvested",
                     "only 9 courses harvested"),
      "no art-subject prefix identified; only 9 courses harvested")
check("distinct facts are all kept, in order",
      S.join_reasons("description coverage 33% below the 70% floor",
                     "below the 50-degree ranking tier"),
      "description coverage 33% below the 70% floor; below the 50-degree ranking tier")
check("no judgement is prefixed onto a count",
      S.art_unit_check({"n_art_courses": 0},
                       "art-subject set below the 100-course floor (86 courses)")[1],
      "art-subject set below the 100-course floor (86 courses)")
# A thin harvest explains its own zero: nothing classified Alabama's 20 courses
# wrongly, we never got its catalog.
check("Alabama: 20 courses harvested is NOT a classification failure",
      "classification failed" in S.art_share_check(0.0, 0, 20)[1], False)
check("and its reason names the harvest", S.art_share_check(0.0, 0, 20)[1],
      "no art-subject prefix identified; only 20 courses harvested")
check("UC Irvine likewise", "classification failed" in S.art_share_check(0.0, 0, 12)[1],
      False)
check("a healthy art college is neither held back nor flagged",
      S.art_share_check(0.807, 1044, 1293)[:1] + (S.art_share_check(0.807, 1044, 1293)[2],),
      (False, []))

print("\n-- rank correlation between the two orderings")
check("identical orders -> 1.0", round(S.rank_corr([1, 2, 3], [1, 2, 3]), 3), 1.0)
check("reversed -> -1.0", round(S.rank_corr([1, 2, 3], [3, 2, 1]), 3), -1.0)
check("too few points -> nan", S.rank_corr([1], [1]) != S.rank_corr([1], [1]), True)

print("\n-- evidence says which denominator each match counts under")
TEMPLE = {"unitid": "1", "name": "Temple-like", "n_courses": 1000,
          "description_coverage": 1.0, "platform": "x",
          "catalog_year": "2026-2027",
          "courses": [
              {"code": "ACCT 3531", "subject": "ACCT",
               "title": "Federal Taxes on Income", "description": "x" * 60},
              {"code": "ADV 3033", "subject": "ADV",
               "title": "Advertising Sales", "description": "x" * 60},
              {"code": "ARTU 2500", "subject": "ARTU",
               "title": "Professional Practice", "description": "x" * 60}]}
_sig = S.prompt_signature(RUBRIC, "m")
_res = {S.cache_key(S.course_text(x), _sig): ["venture"]
        for x in TEMPLE["courses"]}
tv = S.aggregate(TEMPLE, TEMPLE["courses"], _res, RUBRIC, "m",
                 art_subjects={"ARTU"})["axes"]["venture"]
check("whole numerator counts all three", tv["n_matched"], 3)
check("art numerator counts only the art subject", tv["n_matched_art"], 1)
check("art share uses the restricted numerator", tv["share_of_art_catalog"], 1.0)
ev = {e["code"]: e["in_art_subjects"] for e in tv["courses"]}
check("business match is flagged out of the art set", ev["ACCT 3531"], False)
check("advertising match is flagged out too", ev["ADV 3033"], False)
check("art match is flagged in", ev["ARTU 2500"], True)
check("no art set -> flag is None",
      S.aggregate(TEMPLE, TEMPLE["courses"], _res, RUBRIC, "m")
      ["axes"]["venture"]["courses"][0]["in_art_subjects"], None)

print("\n-- evidence is sampled across the list, not sliced off the front")
ALPHA = [dict(CANDIDATES[0], code=f"X {i}", title=t) for i, t in
         enumerate(["Aardvark", "Anatomy", "Basket", "Ceramics", "Drawing",
                    "Etching", "Figure", "Glass", "Weaving", "Zinc"])]
_ares = {S.cache_key(S.course_text(x), _sig): ["venture"] for x in ALPHA}
sc = {"unitid": "2", "name": "A", "n_courses": 100, "description_coverage": 1.0,
      "platform": "x", "catalog_year": "2026-2027", "courses": ALPHA}
samp = S.aggregate(sc, ALPHA, _ares, RUBRIC, "m",
                   evidence_cap=4)["axes"]["venture"]
titles = [e["title"] for e in samp["courses"]]
check("sample is capped", len(titles), 4)
check("sample starts at the first match", titles[0], "Aardvark")
check("sample ends at the last match", titles[-1], "Zinc")
check("sample is not the first four", titles != ["Aardvark", "Anatomy",
                                                 "Basket", "Ceramics"], True)
check("truncation still declared", samp["evidence_truncated"], True)
check("evidence_shown matches the sample", samp["evidence_shown"], 4)

print("\n-- the classifier instruction lives in the rubric")
import copy as _c2
base = S.build_prompt(RUBRIC, [c("A", "T", "d")])
check("default instruction used when absent",
      "classifying university course descriptions" in base, True)
custom = _c2.deepcopy(RUBRIC)
custom["classifier"]["instruction"] = "PRIMARY SUBJECT TEST: default to none."
cp = S.build_prompt(custom, [c("A", "T", "d")])
check("rubric instruction replaces the default",
      cp.startswith("PRIMARY SUBJECT TEST"), True)
check("json format sentences survive", "Respond with JSON only" in cp, True)
check("axis definitions survive", "venture" in cp, True)
check("instruction change invalidates the cache",
      S.prompt_signature(custom, "m") == S.prompt_signature(RUBRIC, "m"), False)

print("\n-- a 7-course harvest is a failed read, not a small school")
# Real cases from the corpus: Sacramento 7, UC-Irvine 13, Miami 3, Kansas 10.
# All cleared the coverage floors, because coverage on seven courses is 100%.
MED = 20.0        # corpus median courses per degree conferred
check("floor constants are named",
      (S.MIN_PLAUSIBLE_COURSES, S.HARVEST_RATIO_FLOOR), (50, 0.0))
check("Sacramento-shaped: 7 courses", S.harvest_plausible(7, 900, MED)[0], True)
check("Miami-shaped: 3 courses", S.harvest_plausible(3, 400, MED)[0], True)
check("reason names the count",
      "7 courses" in S.harvest_plausible(7, 900, MED)[1], True)
check("a real harvest passes", S.harvest_plausible(4000, 900, MED)[0], False)
check("no completions -> absolute floor still applies",
      S.harvest_plausible(7, None, MED)[0], True)

print("\n-- the ratio rule is OFF by default, and here is why")
# Arizona is 18,016 courses over 215 CIP-50 degrees (83.8). RISD is 1,293 over
# ~700 (1.8). An art college confers many degrees from a small focused catalog,
# so a median across both types flags RISD as a failed harvest — the
# coverage-bias failure arriving through the rule written to prevent it.
check("RISD-shaped ratio is 50x below Arizona-shaped",
      round((18016 / 215) / (1293 / 700)), 45)
check("with the rule off, RISD passes",
      S.harvest_plausible(1293, 700, MED)[0], False)
try:
    S.HARVEST_RATIO_FLOOR = 0.20
    check("with the rule on, RISD is wrongly flagged",
          S.harvest_plausible(1293, 700, MED)[0], True)
    check("and Sacramento is flagged for the right reason",
          "per degree" in S.harvest_plausible(7, 900, MED)[1], True)
    check("Penn State at 299 against 500 is caught when enabled",
          S.harvest_plausible(299, 500, MED)[0], True)
finally:
    S.HARVEST_RATIO_FLOOR = 0.0
check("switch restored", S.HARVEST_RATIO_FLOOR, 0.0)

print("\n-- venture/practice exclusivity, enforced in code not hoped for")
EXR = {"axis_exclusivity": {"pairs": [["venture", "practice"]]}}
res_x = {"a": ["venture", "practice"], "b": ["venture"], "c": ["practice"],
         "d": ["venture", "audience"], "e": []}
n = S.apply_exclusivity(res_x, EXR)
check("one course carried both", n, 1)
check("practice wins the tie", res_x["a"], ["practice"])
check("venture alone is untouched", res_x["b"], ["venture"])
check("practice alone is untouched", res_x["c"], ["practice"])
check("venture with another axis is untouched", res_x["d"], ["venture", "audience"])
check("empty is untouched", res_x["e"], [])
check("no rule in the rubric still defaults to venture/practice",
      S.apply_exclusivity({"z": ["venture", "practice"]}, {}), 1)
check("re-running is idempotent", S.apply_exclusivity(res_x, EXR), 0)

print("\n-- long prefix lists are chunked, not truncated")
check("chunk size is named", S.SUBJECT_CHUNK, 80)
check("The New School's 324 prefixes need five chunks",
      -(-324 // S.SUBJECT_CHUNK), 5)
check("a short list is one chunk", -(-30 // S.SUBJECT_CHUNK), 1)
BIG = [{"subject": f"SUB{i}", "n": 10, "samples": ["A course"]}
       for i in range(324)]
pr = S.subject_prompt("Big School", BIG[:S.SUBJECT_CHUNK])
check("a chunk prompt carries only its own prefixes",
      pr.count("SUB"), S.SUBJECT_CHUNK)
check("and still states the exclusion", "without exception" in pr.lower(), True)

print("\n-- composition: one bucket per course, exhaustive")
import os as _os
if _os.path.exists("rubric_v0.6.json"):
    R6 = json.load(open("rubric_v0.6.json"))
    C6 = R6["composition"]
    check("twelve buckets", len(C6["buckets"]), 12)
    for b in ("studio", "history_theory", "liberal_arts", "practice", "venture",
              "technology", "self", "audience", "capstone", "independent",
              "pedagogy", "other"):
        check(f"  bucket {b!r} present", b in C6["buckets"], True)
    cp = S.composition_prompt(R6, [c("ART 101", "Painting I", "Oil on canvas.")])
    check("prompt names every bucket",
          all(b in cp for b in C6["buckets"]), True)
    check("prompt demands exactly one", "exactly one bucket" in cp, True)
    check("composition signature differs from the axis signature",
          S.composition_signature(R6, "m") == S.prompt_signature(R6, "m"), False)
    check("changing a bucket definition invalidates",
          S.composition_signature(
              {**R6, "composition": {**C6, "buckets":
                                     {**C6["buckets"], "studio": "changed"}}},
              "m") == S.composition_signature(R6, "m"), False)
    check("tripwire is named", S.OTHER_TRIPWIRE, 0.05)

print("\n-- composition response parsing")
V12 = {"studio", "capstone", "venture", "other"}
check("plain json",
      S.parse_composition_response('{"0":"studio","1":"capstone"}', 2, V12),
      ["studio", "capstone"])
check("fences stripped",
      S.parse_composition_response('```json\n{"0":"venture"}\n```', 1, V12),
      ["venture"])
check("invented bucket becomes None",
      S.parse_composition_response('{"0":"nonsense"}', 1, V12), [None])
check("missing index becomes None",
      S.parse_composition_response('{"0":"studio"}', 2, V12), ["studio", None])
check("a list of buckets is rejected, one bucket only",
      S.parse_composition_response('{"0":["studio","venture"]}', 1, V12), [None])
check("unparseable -> None", S.parse_composition_response("sorry", 1, V12), None)

print("\n-- v0.5 structure: five axes, percentages, no composite")
import copy as _c5
FIVE = _c5.deepcopy(RUBRIC)
FIVE["axes"]["practice"] = {
    "name": "Professional Readiness",
    "question": "Does this course prepare a student to be hired?",
    "includes": ["portfolio review", "interviewing", "licensure"],
    "excludes": ["business formation"],
    "lexicon": ["portfolio review", "licens*", "internship"],
    "thresholds": [0.002, 0.006, 0.015, 0.03],
}
check("a fifth axis is accepted by the matcher builder",
      len(S.build_matchers(FIVE)), 5)
check("prompt carries all five axes",
      all(a in S.build_prompt(FIVE, [c("A", "T", "d")]) for a in FIVE["axes"]),
      True)
check("adding an axis changes the prompt signature",
      S.prompt_signature(FIVE, "m") == S.prompt_signature(RUBRIC, "m"), False)
sig5 = S.prompt_signature(FIVE, "m")
agg5 = S.aggregate(SCHOOL_ART, SCHOOL_ART["courses"],
                   {S.cache_key(S.course_text(SCHOOL_ART["courses"][0]), sig5):
                    ["venture", "audience"]},
                   FIVE, "m", art_subjects={"ILLUS"})
check("all five axes scored", sorted(agg5["axes"]), sorted(FIVE["axes"]))
check("a course on two axes counts on both",
      (agg5["axes"]["venture"]["n_matched"],
       agg5["axes"]["audience"]["n_matched"]), (1, 1))
check("so percentages can sum above 100%",
      sum(agg5["axes"][a]["share_of_art_catalog"] or 0 for a in FIVE["axes"]) > 0.5,
      True)

print("\n-- the denominator is the art unit")
check("SHARE_KEY is the art share", S.SHARE_KEY, "share_of_art_catalog")
check("and it is labelled for the methodology", S.SHARE_LABEL, "art-subject")

print("\n-- entrepreneurship-titled subset")
ENT = [c("A 1", "Entrepreneurship for Artists", "x" * 60),
       c("A 2", "Arts Entrepreneurship Lab", "x" * 60),
       c("A 3", "Business of Art", "x" * 60)]
sch = {"unitid": "9", "name": "E", "n_courses": 100, "description_coverage": 1.0,
       "platform": "x", "catalog_year": "2026-2027", "courses": ENT}
sigE = S.prompt_signature(RUBRIC, "m")
aggE = S.aggregate(sch, ENT,
                   {S.cache_key(S.course_text(c), sigE): ["venture"] for c in ENT},
                   RUBRIC, "m")["axes"]["venture"]
check("all three count as venture", aggE["n_matched"], 3)
check("two are titled entrepreneurship", aggE["n_titled_entrepreneurship"], 2)
check("the codes are named", sorted(aggE["titled_entrepreneurship"]), ["A 1", "A 2"])
check("a venture course not so titled is excluded",
      "A 3" in aggE["titled_entrepreneurship"], False)

print("\n-- share ranks survive what quotient ranks do not")
# Art shares run roughly 5x whole-catalog shares. Under thresholds calibrated
# for the whole catalog, every school pins at 4 and the quotient correlation
# runs on ties. Share ranks are scale-free and carry the same ordering.
WHOLE = [0.006, 0.010, 0.020, 0.040, 0.080]
ART = [w * 5 for w in WHOLE]
TH = RUBRIC["axes"]["venture"]["thresholds"]
q_whole = [S.score_from_share(w, TH) for w in WHOLE]
q_art = [S.score_from_share(a, TH) for a in ART]
check("whole-catalog scores vary", len(set(q_whole)) > 1, True)
check("art scores saturate at the ceiling", set(q_art), {4})
check("quotient ranks are all ties, so the correlation is undefined",
      S.rank_corr(q_art, q_art) != S.rank_corr(q_art, q_art), True)
check("share ranks recover the ordering exactly",
      round(S.rank_corr(WHOLE, ART), 3), 1.0)
check("share ranks detect genuine reordering",
      round(S.rank_corr([0.01, 0.02, 0.03], [0.03, 0.02, 0.01]), 6), -1.0)
check("a uniform rescale is rank-identity",
      round(S.rank_corr(WHOLE, [w * 137 for w in WHOLE]), 3), 1.0)

print("\n-- a credential failure aborts the run, not the batch")
import urllib.error as _ue

class _FakeHTTP(_ue.HTTPError):
    def __init__(self, code):
        _ue.HTTPError.__init__(self, "u", code, "msg", {}, None)

_calls = {"n": 0}
def _fake_urlopen(code):
    def _f(req, timeout=None):
        _calls["n"] += 1
        raise _FakeHTTP(code)
    return _f

import urllib.request as _ur
_real = _ur.urlopen
try:
    _ur.urlopen = _fake_urlopen(401); _calls["n"] = 0
    try:
        S.call_api("p", "m", "k")
        check("401 raises", False, True)
    except S.AuthError:
        check("401 raises AuthError", True, True)
    check("401 is not retried", _calls["n"], 1)

    _ur.urlopen = _fake_urlopen(403); _calls["n"] = 0
    try:
        S.call_api("p", "m", "k")
    except S.AuthError:
        pass
    check("403 is not retried either", _calls["n"], 1)

    _ur.urlopen = _fake_urlopen(500); _calls["n"] = 0
    try:
        S.call_api("p", "m", "k", max_retries=3)
    except Exception as e:
        check("500 is not an AuthError", isinstance(e, S.AuthError), False)
    check("500 IS retried", _calls["n"], 3)
finally:
    _ur.urlopen = _real

check("AuthError names the fix",
      "ANTHROPIC_API_KEY" in str(S.AuthError("HTTP 401: check ANTHROPIC_API_KEY")),
      True)

print("\n-- rubric v0.2 declares its matching rule")
check("matching rule documented", "matching" in RUBRIC, True)
check("star convention stated", "*" in RUBRIC["matching"]["rule"], True)
check("author flags carried", "flagged_for_author" in RUBRIC, True)

print("\n-- rubric integrity")
check("four axes", len(RUBRIC["axes"]), 4)
for ax, spec in RUBRIC["axes"].items():
    check(f"{ax}: four thresholds", len(spec["thresholds"]), 4)
    check(f"{ax}: thresholds ascend",
          spec["thresholds"] == sorted(spec["thresholds"]), True)
    check(f"{ax}: has includes and excludes",
          bool(spec["includes"]) and bool(spec["excludes"]), True)
check("seed status is declared", RUBRIC["status"].startswith("SEED"), True)
check("limitations are recorded", len(RUBRIC["limitations"]) >= 3, True)

print("\n-- V14: within-school collapse (scoring-chat blocking item 4)")
import harvest as _H
import argparse as _ap, subprocess as _sp, shutil as _sh
_KEYS = [{"title": "  Open Critique ", "description": "Group critique." * 30},
         {"title": "open critique", "description": ("Group critique." * 30).upper()},
         {"title": "", "description": "x"}, {"title": None, "description": None},
         {"title": "T", "description": "   lead"}]
check("dedupe_key is harvest's key, byte for byte",
      [S.dedupe_key(k) for k in _KEYS], [_H.dedupe_key(k) for k in _KEYS])
check("n_distinct is harvest's n_deduped",
      S.n_distinct(_KEYS), _H.n_deduped(_KEYS))

def cc(code, title, desc, subj=None):
    return {"code": code, "title": title, "description": desc,
            "subject": subj or code.split()[0], "tier": 1}
CRIT = "Weekly open critique of work in progress across all programs. " * 4
OLIV = "Deep listening and the sonic meditations as a studio method. " * 4
CAL = [cc("AART 300C", "Open Critique", CRIT),
       cc("AART 500C", "Open Critique", CRIT),                  # grad twin
       cc("AART 474", "Pauline Oliveros for Artists", OLIV),
       cc("AART 674", "Pauline Oliveros for Artists", OLIV),    # grad twin
       cc("AART 610", "Graduate Forum", "Forum."),
       cc("APHM 610", "Graduate Forum", "Forum."),
       cc("AGRA 610", "Graduate Forum", "Forum."),              # three times
       cc("AART 101", "Drawing I", "Observation."),
       cc("HIST 300", "Visual Culture", "Images and power." * 5),  # non-art first
       cc("ARTH 300", "Visual Culture", "Images and power." * 5),  # cross-list
       cc("AART 310", "Open Critique", "A different course entirely."),
       cc("AART 999", "", ""), cc("AART 998", "", "")]
CALS = {"unitid": "111081", "name": "CalArts", "n_courses": len(CAL),
        "description_coverage": 1.0, "platform": "x", "catalog_year": "2026",
        "courses": CAL}
_s14 = S.prompt_signature(RUBRIC, "m")
SELFY = [c for c in CAL if c["title"] in ("Open Critique", "Pauline Oliveros "
         "for Artists", "Graduate Forum")]
_r14 = {S.cache_key(S.course_text(c), _s14): ["self"] for c in SELFY}
_r14[S.cache_key(S.course_text(CAL[8]), _s14)] = ["venture"]
_r14[S.cache_key(S.course_text(CAL[9]), _s14)] = ["venture"]
ART14 = {"AART", "ARTH"}
g = S.aggregate(CALS, [c for c in CAL if S.cache_key(S.course_text(c), _s14)
                       in _r14], _r14, RUBRIC, "m", art_subjects=ART14)
sv, vv = g["axes"]["self"], g["axes"]["venture"]
check("raw Self count is 8", sv["n_matched_raw"], 8)
check("collapsed Self count is 4 (critique, Oliveros, forum, the other critique)",
      sv["n_matched"], 4)
check("same title, different description is NOT collapsed",
      sorted(e["code"] for e in sv["courses"]).count("AART 310"), 1)
check("grad twin named on the representative",
      {e["code"]: e["also_listed_as"] for e in sv["courses"]}["AART 300C"],
      ["AART 500C"])
check("three-way listing collapses to one",
      {e["code"]: e["also_listed_as"] for e in sv["courses"]}["AART 610"],
      ["APHM 610", "AGRA 610"])
# duplicates: AART 500C, AART 674, APHM 610, AGRA 610, ARTH 300 -> 5
check("n_courses collapses: 13 records, 5 duplicates", (g["n_courses"],
      g["n_courses_raw"], g["n_collapsed"]), (8, 13, 5))
check("blank titles are never merged",
      S.n_distinct([cc("A 1", "", ""), cc("A 2", "", "")]), 2)
# art set is {AART, ARTH}: 10 records; twins 500C and 674 collapse -> 8.
# APHM/AGRA 610 are outside it, so the forum group's art member is AART 610.
check("art unit collapses too", (g["n_art_courses"], g["n_art_courses_raw"],
      g["n_art_collapsed"]), (8, 10, 2))
check("cross-list: one course", vv["n_matched"], 1)
check("cross-list: counts in the art unit when any member is art",
      vv["n_matched_art"], 1)
check("cross-list: the representative is the art member",
      vv["courses"][0]["code"], "ARTH 300")
check("share uses collapsed numerator and denominator",
      sv["share_of_art_catalog"], round(4 / 8, 5))
check("title-only diagnostic is looser than the rule",
      g["dedupe"]["title_only"]["n_matched_art"]["self"], 3)
check("rule is recorded on the record", g["dedupe"]["rule"], S.DEDUPE_RULE)
check("no split groups when every member matched",
      g["dedupe"]["split_groups"]["self"], 0)
_same = S.cache_key(S.course_text(CAL[0]), _s14) == S.cache_key(
    S.course_text(CAL[1]), _s14)
check("identical texts share one cache key, so twins cannot disagree", _same, True)
_SPL = [cc("AART 1", "Studio Seminar", "Same lead. " * 30 + " undergrad"),
        cc("AART 2", "Studio Seminar", "Same lead. " * 30 + " graduate")]
_spr = {S.cache_key(S.course_text(_SPL[0]), _s14): ["self"]}
_sg = S.aggregate({"unitid": "3", "name": "S", "n_courses": 2,
                   "description_coverage": 1, "platform": "x",
                   "catalog_year": "2026", "courses": _SPL},
                  _SPL, _spr, RUBRIC, "m", art_subjects={"AART"})
check("members differing past 200 chars collapse", _sg["n_art_courses"], 1)
check("and a half-matched group is reported as split",
      _sg["dedupe"]["split_groups"]["self"], 1)

# The invariant the collapse exists to protect.
import random as _rnd
_rnd.seed(7)
_ok = True
for _trial in range(200):
    pool = [cc(f"{_rnd.choice(['AART','HIST','ARTH'])} {i}",
               _rnd.choice(["A", "B", "C", "D", ""]),
               _rnd.choice(["x", "y", ""])) for i in range(12)]
    rr = {S.cache_key(S.course_text(c), _s14): ["venture"]
          for c in pool if _rnd.random() < .5}
    sh = [c for c in pool if S.cache_key(S.course_text(c), _s14) in rr]
    ag = S.aggregate({"unitid": "4", "name": "R", "n_courses": 12,
                      "description_coverage": 1, "platform": "x",
                      "catalog_year": "2026", "courses": pool},
                     sh, rr, RUBRIC, "m", art_subjects={"AART", "ARTH"})
    v_ = ag["axes"]["venture"]
    if not (v_["n_matched_art"] <= ag["n_art_courses"]
            and v_["n_matched"] <= ag["n_courses"]
            and v_["n_matched_art"] <= v_["n_matched"]):
        _ok = False
        break
check("200 random schools: every numerator within its denominator", _ok, True)

print("\n-- V14: --rubric is required")
_pr = _sp.run([sys.executable, "score.py", "--dry-run"], capture_output=True,
              text=True)
check("no --rubric exits with a usage error", _pr.returncode, 2)
check("and says why", "--rubric" in _pr.stderr, True)

print("\n-- V14: --classify-subjects honours --schools")
_td = tempfile.mkdtemp()
_cd = os.path.join(_td, "courses")
os.makedirs(_cd)
for _u in ("111", "222"):
    json.dump({"unitid": _u, "name": f"School {_u}", "n_courses": 1,
               "courses": [cc("AART 1", "Drawing", "d")]},
              open(os.path.join(_cd, f"{_u}.json"), "w"))
S.LOG.clear()
S.run_classify_subjects(_ap.Namespace(
    courses=_cd, subject_dir=os.path.join(_td, "subj"),
    subject_cache=os.path.join(_td, "sc.jsonl"), schools="222", limit=0,
    dry_run=True), "m")
_would = [l for l in S.LOG if "would classify" in l]
check("one school requested, one considered", len(_would), 1)
check("and it is the requested one", "School 222" in _would[0], True)

print("\n-- V14: composition shares the collapsed art unit")
if _os.path.exists("rubric_v0.6.json"):
    _R = json.load(open("rubric_v0.6.json"))
    _csig = S.composition_signature(_R, "m")
    _cd2 = os.path.join(_td, "courses2")
    _sd2 = os.path.join(_td, "subj2")
    os.makedirs(_cd2); os.makedirs(_sd2)
    SH = "Directed independent study with a faculty member. " * 3
    COMP = [cc("AART 199", "Independent Study", SH),
            cc("APHM 199", "Independent Study", SH),
            cc("AGRA 199", "Independent Study", SH),
            cc("AART 101", "Drawing I", "Observation."),
            cc("HIST 1", "History", "h")]
    json.dump({"unitid": "111081", "name": "CalArts", "n_courses": 5,
               "courses": COMP}, open(os.path.join(_cd2, "111081.json"), "w"))
    json.dump({"unitid": "111081", "art_subjects": [
        {"subject": x, "included": True} for x in ("AART", "APHM", "AGRA")]},
        open(os.path.join(_sd2, "111081.json"), "w"))
    _cc = os.path.join(_td, "cc.jsonl")
    _args = _ap.Namespace(courses=_cd2, subject_dir=_sd2, schools="", limit=0,
                          composition_dir=os.path.join(_td, "comp"),
                          composition_cache=_cc, batch_size=20, workers=1,
                          dry_run=True)
    S.LOG.clear()
    S.run_classify_composition(_args, "m", _R)
    _dl = [l for l in S.LOG if "would classify" in l][0]
    check("dry run prices unique texts, not records",
          "(4 records, 2 uncached texts)" in _dl, True)
    check("dry run reports the collapsed art unit", " 2 art-unit courses" in _dl,
          True)
    S.append_cache(_cc, [
        {"key": S.cache_key(S.course_text(COMP[0]), _csig), "axes": "independent"},
        {"key": S.cache_key(S.course_text(COMP[3]), _csig), "axes": "studio"}])
    _args.dry_run = False
    S.LOG.clear()
    S.run_classify_composition(_args, "m", _R)
    _rec = json.load(open(os.path.join(_td, "comp", "111081.json")))
    check("art unit is collapsed", (_rec["n_art_courses"],
          _rec["n_art_courses_raw"], _rec["n_art_collapsed"]), (2, 4, 2))
    check("buckets are collapsed", (_rec["buckets"]["independent"],
          _rec["buckets"]["studio"]), (1, 1))
    check("raw buckets keep the registrar-coding fact",
          _rec["buckets_raw"]["independent"], 3)
    check("percentages are of the collapsed unit",
          _rec["bucket_pct"]["independent"], 50.0)
    check("course list is one row per course, twins named",
          [(r["code"], r["also_listed_as"]) for r in _rec["courses"]],
          [("AART 199", ["APHM 199", "AGRA 199"]), ("AART 101", [])])
    check("no split buckets", _rec["n_bucket_split"], 0)
_sh.rmtree(_td, ignore_errors=True)

print("\n-- V15: the art unit must be trustworthy to be ranked")
check("flagged subject set is unranked",
      S.art_unit_check({"n_art_courses": 900}, "art denominator 42 below 100")[0],
      True)
check("and the classifier's reason is carried",
      "42 below 100" in S.art_unit_check({"n_art_courses": 900}, "art denominator "
                                         "42 below 100")[1], True)
check("no art denominator is unranked, never ranked on the whole catalog",
      S.art_unit_check({"n_art_courses": None}, None),
      (True, "no art-subject denominator"))
check("zero art courses likewise", S.art_unit_check({"n_art_courses": 0}, None)[0],
      True)
check("a sound art unit passes", S.art_unit_check({"n_art_courses": 300}, None),
      (False, ""))

if _os.path.exists("rubric_v0.6.json"):
    import csv as _csv
    _R6 = json.load(open("rubric_v0.6.json"))
    _e = tempfile.mkdtemp()
    for d in ("courses", "subj", "out"):
        os.makedirs(os.path.join(_e, d))
    _sig6 = S.prompt_signature(_R6, _R6["classifier"]["model"])
    _rows = []
    for u, nm in (("1", "Sound"), ("2", "Flagged"), ("3", "NoSet")):
        cs = [cc(f"ART {i}", f"Drawing {u}-{i}", "Observational drawing. " * 3)
              for i in range(60)]
        json.dump({"unitid": u, "name": nm, "n_courses": 60, "courses": cs,
                   "description_coverage": 1.0, "platform": "x",
                   "catalog_year": "2026"},
                  open(os.path.join(_e, "courses", f"{u}.json"), "w"))
        _rows += [{"key": S.cache_key(S.course_text(c), _sig6), "axes": []}
                  for c in cs]
    S.append_cache(os.path.join(_e, "c.jsonl"), _rows)
    for u, flag in (("1", False), ("2", True)):
        json.dump({"unitid": u, "art_subjects": [{"subject": "ART",
                   "included": True}], "implausible": flag,
                   "implausible_reason": "art denominator 60 below 100" if flag
                   else ""}, open(os.path.join(_e, "subj", f"{u}.json"), "w"))
    _argv = sys.argv
    sys.argv = ["score.py", "--rubric", "rubric_v0.6.json",
                "--courses", os.path.join(_e, "courses"),
                "--subject-dir", os.path.join(_e, "subj"),
                "--cache", os.path.join(_e, "c.jsonl"),
                "--out-dir", os.path.join(_e, "out"), "--roster", "/nonexistent",
                "--harvest-lock", os.path.join(_e, ".harvest.lock")]
    S.LOG.clear()
    S.main()
    sys.argv = _argv
    _ix = {r["name"]: r for r in _csv.DictReader(
        open(os.path.join(_e, "out", "index.csv"), encoding="utf-8"))}
    check("sound art unit is ranked", _ix["Sound"]["ranked"], "True")
    check("and carries an art-unit percentage", _ix["Sound"]["pct_venture"], "0.0")
    check("flagged set is reported, not ranked", _ix["Flagged"]["ranked"], "False")
    # V32: the reason is the fact the subject pass recorded, with no judgement
    # word wrapped around it.
    check("with the reason, stated as a fact",
          _ix["Flagged"]["unranked_reason"], "art denominator 60 below 100")
    check("no subject set is reported, not ranked", _ix["NoSet"]["ranked"],
          "False")
    check("and never given a whole-catalog percentage",
          _ix["NoSet"]["pct_venture"], "")
    _sh.rmtree(_e, ignore_errors=True)

print("\n-- V15: a subject dry run writes nothing")
_t2 = tempfile.mkdtemp()
os.makedirs(os.path.join(_t2, "courses"))
_sch = {"unitid": "333", "name": "Cached", "n_courses": 1,
        "courses": [cc("AART 1", "Drawing", "d")]}
json.dump(_sch, open(os.path.join(_t2, "courses", "333.json"), "w"))
_blob = S.subject_blob(S.subject_rows(_sch))
S.append_cache(os.path.join(_t2, "sc.jsonl"), [{
    "key": S.cache_key(_blob, S.subject_signature("m")), "axes": {"AART": True}}])
S.run_classify_subjects(_ap.Namespace(
    courses=os.path.join(_t2, "courses"), subject_dir=os.path.join(_t2, "subj"),
    subject_cache=os.path.join(_t2, "sc.jsonl"), schools="", limit=0,
    dry_run=True), "m")
check("cached verdict, dry run: no subject file written",
      os.listdir(os.path.join(_t2, "subj")), [])
_sh.rmtree(_t2, ignore_errors=True)

print("\n-- V16: score will not read while a harvest writes")
import fcntl as _fc
_l = tempfile.mkdtemp()
_lp = os.path.join(_l, ".harvest.lock")
_hold = open(_lp, "a+")
_fc.flock(_hold, _fc.LOCK_EX)
_hold.write("pid 1 test harvest"); _hold.flush()
_r = _sp.run([sys.executable, "score.py", "--rubric", "rubric_v0.2.json",
              "--dry-run", "--harvest-lock", _lp], capture_output=True, text=True)
check("score refuses while a harvest holds the lock", _r.returncode, 3)
check("and names the holder", "test harvest" in _r.stderr, True)
_fc.flock(_hold, _fc.LOCK_UN); _hold.close()
check("released: score takes the shared lock",
      S.acquire_read_lock(_lp)[0], True)
_r2 = _sp.run([sys.executable, "-c",
               f"import score as S; print(S.acquire_read_lock({_lp!r})[0])"],
              capture_output=True, text=True)
check("two score runs share it", _r2.stdout.strip(), "True")
_r3 = _sp.run([sys.executable, "-c",
               f"import harvest as H; print(H.acquire_lock({_lp!r})[0])"],
              capture_output=True, text=True)
check("a harvest cannot start under a score run", _r3.stdout.strip(), "False")
S._READ_LOCK.close(); S._READ_LOCK = None
_sh.rmtree(_l, ignore_errors=True)

print("\n-- V17: the signature is the prompt the model receives")
import copy as _c17
if _os.path.exists("rubric_v0.6.json"):
    _R17 = json.load(open("rubric_v0.6.json"))
    _fmt = S.build_prompt(_R17, [])
    check("scaffold names every rubric axis, practice included",
          "(technology, venture, practice, self, audience)" in _fmt, True)
    check("no stale four-name list survives", "venture, self, audience)" in
          _fmt.replace("practice, self", ""), False)
    _a = S.prompt_signature(_R17, "m")
    _lex = _c17.deepcopy(_R17); _lex["axes"]["self"]["lexicon"].append("zzz")
    check("a lexicon edit still costs nothing", S.prompt_signature(_lex, "m"), _a)
    _ins = _c17.deepcopy(_R17); _ins["classifier"]["instruction"] += " More."
    check("an instruction edit invalidates", S.prompt_signature(_ins, "m") == _a,
          False)
    _exc = _c17.deepcopy(_R17); _exc["axes"]["self"]["excludes"].append("studios")
    check("an excludes edit invalidates", S.prompt_signature(_exc, "m") == _a,
          False)
    check("the model is in the signature",
          S.prompt_signature(_R17, "claude-sonnet-4-6") == _a, False)
    _real = S.build_prompt(_R17, [c("A 1", "T", "d")])
    check("the hashed template is the real prompt's prefix",
          _real.startswith(_fmt.split("\n\nCOURSES:")[0]), True)
    _bk = _c17.deepcopy(_R17); _bk["composition"]["buckets"]["studio"] += " x"
    check("a bucket definition edit invalidates composition",
          S.composition_signature(_bk, "m") == S.composition_signature(_R17, "m"),
          False)

print("\n-- V17: the model is pinned by the rubric")
_m = tempfile.mkdtemp()
_p17 = _sp.run([sys.executable, "score.py", "--rubric", "rubric_v0.2.json",
                "--model", "some-other-model", "--dry-run",
                "--harvest-lock", os.path.join(_m, ".l")],
               capture_output=True, text=True)
check("a --model that differs from the rubric is refused", _p17.returncode, 2)
check("and says the model is part of the instrument",
      "part of the instrument" in _p17.stderr, True)
check("records carry the override flag",
      S.aggregate(school, [CANDIDATES[0]], res, RUBRIC, "m")["model_overridden"],
      False)
_sh.rmtree(_m, ignore_errors=True)

print("\n-- V17: subject prompt names what the diff let through")
_SP17 = S.subject_prompt("X", []).lower()
for _term in ["welding", "plumbing", "carpentry", "horticulture",
              "life sciences", "journalism", "family and consumer",
              "extension", "pre-college", "partnering", "dance technique"]:
    check(f"  excluded: {_term!r}", _term in _SP17, True)
for _term in ["communication design", "medical illustration", "photography"]:
    check(f"  protected in scope: {_term!r}", _term in _SP17, True)
check("subject signature moved with the prompt",
      S.subject_signature("claude-haiku-4-5-20251001") == "e14bacf99d449ab3", False)

print("\n-- V18: subject scope comes from the rubric; the CIP list from the roster")
if _os.path.exists("rubric_v0.8.json"):
    _R8 = json.load(open("rubric_v0.8.json"))
    _sc = _R8["subject_scope"]
    _P8 = S.subject_prompt("Temple University", [], _sc)
    for _fam in _sc["excluded_families"]:
        check(f"  rubric family rendered: {_fam[:36]!r}", _fam in _P8, True)
    check("protected line rendered verbatim", _sc["protected"] in _P8, True)
    check("not-subjects rule rendered (TUJ)", "TUJ" in _P8, True)
    check("delivery rule rendered", _sc["excluded_delivery"] in _P8, True)
    check("positive rule rendered", _sc["positive_rule"] in _P8, True)
    check("in-scope list is the roster's, not the rubric's prose",
          "50.13 Community/Environmental/Socially-Engaged Art" in _P8
          and "50.13 theatre design" not in _P8, True)
    check("arts management named out although v0.8 omits it",
          "50.10 Arts, Entertainment and Media Management" in _P8, True)
    check("theatre design and technology named as 50.05, out",
          "theatre design and technology, 50.0502" in _P8, True)
    check("four statuses offered",
          all(f'"{x}"' in _P8 for x in S.SUBJECT_STATUSES), True)
    check("a scope edit moves the subject signature",
          S.subject_signature("m", _sc) == S.subject_signature("m", None), False)

    check("status: extension", S.parse_subject_response(
          '{"XYTH": "extension"}', {"XYTH"}), {"XYTH": "extension"})
    check("status: administrative", S.parse_subject_response(
          '{"TUJ": "administrative"}', {"TUJ"}), {"TUJ": "administrative"})
    check("unknown status is out, never art", S.parse_subject_response(
          '{"X": "maybe"}', {"X"}), {"X": "out"})

    _t8 = tempfile.mkdtemp()
    os.makedirs(os.path.join(_t8, "courses"))
    _cal = {"unitid": "111081", "name": "CalArts", "n_courses": 6, "courses": [
        cc("AART 1", "Drawing", "d"), cc("AART 2", "Painting", "d"),
        cc("XYTH 805", "Discover Studio Art - 1 Week", "d"),
        cc("XYTH 806", "Summer Intensive", "d"),
        cc("TUJ 1", "Japan Campus Seminar", "d"), cc("HIST 1", "History", "d")]}
    json.dump(_cal, open(os.path.join(_t8, "courses", "111081.json"), "w"))
    _v8 = {"AART": "art", "XYTH": "extension", "TUJ": "administrative", "HIST": "out"}
    _sg8 = S.subject_signature("m", _sc)
    S.append_cache(os.path.join(_t8, "sc.jsonl"), [
        {"key": S.prefix_key(_cal["name"], r, _sg8), "axes": _v8[r["subject"]]}
        for r in S.subject_rows(_cal)])
    S.run_classify_subjects(_ap.Namespace(
        courses=os.path.join(_t8, "courses"), subject_dir=os.path.join(_t8, "subj"),
        subject_cache=os.path.join(_t8, "sc.jsonl"), schools="", limit=0,
        dry_run=False), "m", _R8)
    _sub = json.load(open(os.path.join(_t8, "subj", "111081.json")))
    check("only the art prefix is included",
          sorted(x["subject"] for x in _sub["art_subjects"] if x["included"]),
          ["AART"])
    check("extension reported with its course count", _sub["excluded_delivery"],
          {"prefixes": ["XYTH"], "n_courses": 2})
    check("campus code reported separately", _sub["excluded_not_subjects"],
          {"prefixes": ["TUJ"], "n_courses": 1})
    check("status carried per prefix",
          {x["subject"]: x["status"] for x in _sub["art_subjects"]}["XYTH"],
          "extension")
    check("the art denominator excludes both", _sub["n_art_courses"], 2)
    check("record says the scope came from the rubric",
          _sub["subject_scope_from_rubric"], True)
    _sh.rmtree(_t8, ignore_errors=True)

print("\n-- V19: editorial subject overrides, applied after classification")
if _os.path.exists("rubric_v0.8.json"):
    _R9 = json.load(open("rubric_v0.8.json"))
    _R9["subject_overrides"] = {"_note": "ignored", "140951": {"ADVE": "out"}}
    _t9 = tempfile.mkdtemp()
    os.makedirs(os.path.join(_t9, "courses"))
    _sc9 = {"unitid": "140951", "name": "SCAD", "n_courses": 3, "courses": [
        cc("ADVE 101", "Art Direction", "d"), cc("ILLU 101", "Illustration", "d"),
        cc("ILLU 102", "Illustration II", "d")]}
    json.dump(_sc9, open(os.path.join(_t9, "courses", "140951.json"), "w"))
    _sg9 = S.subject_signature("m", _R9["subject_scope"])
    S.append_cache(os.path.join(_t9, "sc.jsonl"), [
        {"key": S.prefix_key(_sc9["name"], r, _sg9), "axes": "art"}
        for r in S.subject_rows(_sc9)])
    check("overrides do not move the subject signature",
          S.subject_signature("m", _R9["subject_scope"]),
          S.subject_signature("m", json.load(open("rubric_v0.8.json"))["subject_scope"]))
    S.run_classify_subjects(_ap.Namespace(
        courses=os.path.join(_t9, "courses"), subject_dir=os.path.join(_t9, "subj"),
        subject_cache=os.path.join(_t9, "sc.jsonl"), schools="", limit=0,
        dry_run=False), "m", _R9)
    _s9 = json.load(open(os.path.join(_t9, "subj", "140951.json")))
    check("override removes the prefix from the art unit",
          sorted(x["subject"] for x in _s9["art_subjects"] if x["included"]),
          ["ILLU"])
    check("and is recorded with the model's own verdict",
          _s9["overrides_applied"], {"ADVE": {"model": "art", "override": "out"}})
    check("the denominator follows", _s9["n_art_courses"], 2)
    _sh.rmtree(_t9, ignore_errors=True)

print("\n-- V20: random, sized, seeded sampling; the titles are in the cache key")
_VM = {"unitid": "164748", "name": "Emerson", "courses":
       [cc(f"VM {i}", f"Media Studies {i}", "d") for i in range(5)] +
       [cc(f"VM {100 + i}", f"Film Production {i}", "d") for i in range(145)]}
_r = S.subject_rows(_VM)[0]
check("150 distinct titles -> 25 sampled", (_r["n_titles"], _r["n_sampled"]), (150, 25))
check("not the first five in catalog order",
      all(x.startswith("Media Studies") for x in _r["samples"][:5]), False)
check("seeded: a rerun draws the same titles",
      S.subject_rows(_VM)[0]["samples"], _r["samples"])
check("seed is per school: another school draws differently",
      S.subject_rows(dict(_VM, unitid="999999"))[0]["samples"] == _r["samples"], False)
_SH = {"unitid": "1", "courses": [cc(f"AART {i}", "Independent Study", "d")
                                   for i in range(41)] + [cc("AART 900", "Drawing", "d")]}
_rs = S.subject_rows(_SH)[0]
check("41 identical shells are one title", (_rs["n"], _rs["n_titles"]), (42, 2))
check("small prefixes are read whole", _rs["n_sampled"], 2)
check("size rule", [S.sample_size(x) for x in (3, 10, 40, 150, 2000)],
      [3, 10, 13, 25, 30])
_b1 = S.subject_blob(S.subject_rows(_VM))
_b2 = S.subject_blob(S.subject_rows(dict(_VM, unitid="999999")))
check("different samples, different cache key", _b1 == _b2, False)
check("the prompt says how the sample was drawn",
      "25 of 150 distinct titles, sampled at random" in
      S.subject_prompt("Emerson", S.subject_rows(_VM)), True)
if _os.path.exists("rubric_v0.9.json"):
    _P9 = S.subject_prompt("SCAD", [], json.load(open("rubric_v0.9.json"))["subject_scope"])
    _pre, _, _post = _P9.partition("every CIP 50 family outside the list above")
    check("v0.9's advertising NOTE is not printed as an exclusion bullet",
          "- NOTE" in _P9, False)
    check("but it reaches the model, after the exclusion list",
          "advertising DESIGN is CIP 50.0402" in _post, True)

print("\n-- V21: composition classifies EVERY batch, and says when it did not")
if _os.path.exists("rubric_v0.9.json"):
    import threading as _th
    _R21 = json.load(open("rubric_v0.9.json"))
    _real_prompt, _real_call = S.composition_prompt, S.call_api
    _calls, _fail_once, _lk = [], [False], _th.Lock()

    def _fake_prompt(rubric, courses):
        return f"N={len(courses)}"

    def _fake_call(prompt, model, key, max_retries=4):
        n = int(prompt.split("=")[1])
        with _lk:
            _calls.append(n)
            if _fail_once[0] and n == 20:
                _fail_once[0] = False
                raise ValueError("simulated refusal")
        return json.dumps({str(i): "studio" for i in range(n)})

    def _comp_run(n_courses, fail):
        _calls.clear(); _fail_once[0] = fail
        d = tempfile.mkdtemp()
        for sub in ("courses", "subj", "comp"):
            os.makedirs(os.path.join(d, sub))
        cs = [cc(f"AART {i}", f"Studio {i}", f"Distinct studio course {i}.")
              for i in range(n_courses)]
        json.dump({"unitid": "7", "name": "Big Art Unit", "n_courses": n_courses,
                   "courses": cs}, open(os.path.join(d, "courses", "7.json"), "w"))
        json.dump({"unitid": "7", "art_subjects": [{"subject": "AART",
                   "included": True}]}, open(os.path.join(d, "subj", "7.json"), "w"))
        _env = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "test-no-network"
        S.composition_prompt, S.call_api = _fake_prompt, _fake_call
        try:
            S.run_classify_composition(_ap.Namespace(
                courses=os.path.join(d, "courses"),
                subject_dir=os.path.join(d, "subj"), schools="", limit=0,
                composition_dir=os.path.join(d, "comp"),
                composition_cache=os.path.join(d, "cc.jsonl"), batch_size=0,
                workers=3, dry_run=False), "m", _R21)
        finally:
            S.composition_prompt, S.call_api = _real_prompt, _real_call
            if _env is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = _env
        rec = json.load(open(os.path.join(d, "comp", "7.json")))
        _sh.rmtree(d, ignore_errors=True)
        return rec

    _full = _comp_run(45, fail=False)
    check("45 courses go out as 20 + 20 + 5, not 20 + nothing",
          sorted(_calls), [5, 20, 20])
    check("every course classified", (_full["n_classified"], _full["n_art_courses"]),
          (45, 45))
    check("coverage recorded", _full["classification_coverage"], 1.0)
    check("and publishable", _full["coverage_ok"], True)
    _part = _comp_run(45, fail=True)
    check("a failed batch leaves courses unclassified, counted",
          (_part["n_classified"], _part["n_unclassified"]), (25, 20))
    check("coverage says so", _part["classification_coverage"], round(25 / 45, 4))
    check("below the floor the composition is not publishable",
          _part["coverage_ok"], False)

print("\n-- V21: ranks only for the rubric's ranked axes; a count beside every %")
if _os.path.exists("rubric_v0.6.json"):
    _Rp = json.load(open("rubric_v0.6.json"))
    _Rp["publication"] = {"ranked_axes": ["technology", "practice"]}
    _e2 = tempfile.mkdtemp()
    for d in ("courses", "subj", "out"):
        os.makedirs(os.path.join(_e2, d))
    json.dump(_Rp, open(os.path.join(_e2, "r.json"), "w"))
    _sgp = S.prompt_signature(_Rp, _Rp["classifier"]["model"])
    _rows2 = []
    for u in ("1", "2"):
        cs = [cc(f"ART {i}", f"Drawing {u}-{i}", "Observational drawing. " * 3)
              for i in range(120)]
        json.dump({"unitid": u, "name": f"S{u}", "n_courses": 120, "courses": cs,
                   "description_coverage": 1.0, "platform": "x",
                   "catalog_year": "2026"},
                  open(os.path.join(_e2, "courses", f"{u}.json"), "w"))
        _rows2 += [{"key": S.cache_key(S.course_text(c), _sgp), "axes": []}
                   for c in cs]
        json.dump({"unitid": u, "art_subjects": [{"subject": "ART",
                   "included": True}], "implausible": False},
                  open(os.path.join(_e2, "subj", f"{u}.json"), "w"))
    S.append_cache(os.path.join(_e2, "c.jsonl"), _rows2)
    _argv = sys.argv
    sys.argv = ["score.py", "--rubric", os.path.join(_e2, "r.json"),
                "--courses", os.path.join(_e2, "courses"),
                "--subject-dir", os.path.join(_e2, "subj"),
                "--cache", os.path.join(_e2, "c.jsonl"),
                "--out-dir", os.path.join(_e2, "out"), "--roster", "/nonexistent",
                "--harvest-lock", os.path.join(_e2, ".l")]
    S.LOG.clear()
    S.main()
    sys.argv = _argv
    _ix2 = list(_csv.DictReader(open(os.path.join(_e2, "out", "index.csv"),
                                     encoding="utf-8")))
    check("ranked axis carries a rank", _ix2[0]["rank_technology"] != "", True)
    check("census axis carries no rank", {r["rank_venture"] for r in _ix2}, {""})
    check("census axis still carries its percentage", _ix2[0]["pct_venture"], "0.0")
    check("every axis carries its count", _ix2[0]["count_venture"], "0")
    check("the log prints the count census",
          any("COUNT CENSUS" in l for l in S.LOG), True)
    _sh.rmtree(_e2, ignore_errors=True)

print("\n-- V22: a hand-collected art-unit file is kept out of whole-catalog statistics")
_m = dict(school, catalog_scope="art_unit_only")
check("scope carried onto the record",
      S.aggregate(_m, [CANDIDATES[0]], res, RUBRIC, "m")["catalog_scope"], "art_unit_only")
check("harvested files default to whole catalog",
      S.aggregate(school, [CANDIDATES[0]], res, RUBRIC, "m")["catalog_scope"], "whole_catalog")

print("\n-- V23: one verdict per prefix; adding a course moves only its own prefix")
if _os.path.exists("rubric_v0.10.json"):
    import threading as _th23
    _R10 = json.load(open("rubric_v0.10.json"))
    _sg = S.subject_signature("m", _R10["subject_scope"])
    _asked, _lk23 = [], _th23.Lock()
    _real_call23 = S.call_api

    def _fake_subj(prompt, model, key, max_retries=4):
        got = re.findall(r"^([A-Z]{2,8})  \(", prompt, re.M)
        with _lk23:
            _asked.extend(got)
        # a fickle reader: DSGN in on some calls, out on others
        return json.dumps({p: ("out" if p == "DSGN" and len(_asked) > 6 else
                               "art" if p in ("ART", "DSGN", "PHOT") else "out")
                           for p in got})

    def _lmu(extra_hist=0):
        cs = ([cc(f"ART {i}", f"Studio {i}", "d") for i in range(40)] +
              [cc(f"DSGN {i}", f"Design {i}", "d") for i in range(30)] +
              [cc(f"PHOT {i}", f"Photo {i}", "d") for i in range(20)] +
              [cc(f"HIST {i}", f"History {i}", "d") for i in range(25 + extra_hist)])
        return {"unitid": "117946", "name": "Loyola Marymount University",
                "n_courses": len(cs), "courses": cs}

    def _run(d, school, seed_from=""):
        json.dump(school, open(os.path.join(d, "courses", "117946.json"), "w"))
        _env = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "test-no-network"
        S.call_api = _fake_subj
        try:
            S.run_classify_subjects(_ap.Namespace(
                courses=os.path.join(d, "courses"), subject_dir=os.path.join(d, "subj"),
                subject_cache=os.path.join(d, "sc.jsonl"), schools="", limit=0,
                dry_run=False, subject_seed_from=seed_from), "m", _R10)
        finally:
            S.call_api = _real_call23
            if _env is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = _env
        return json.load(open(os.path.join(d, "subj", "117946.json")))

    _d23 = tempfile.mkdtemp()
    os.makedirs(os.path.join(_d23, "courses"))
    _first = _run(_d23, _lmu())
    check("first run asks every prefix once", sorted(_asked), ["ART", "DSGN", "HIST", "PHOT"])
    _asked.clear()
    _second = _run(_d23, _lmu(extra_hist=1))
    check("one course added to HIST: only HIST is asked again", _asked, ["HIST"])
    check("DSGN keeps its verdict although the reader has turned fickle",
          {x["subject"]: x["status"] for x in _second["art_subjects"]}["DSGN"], "art")
    check("so the art unit does not move", _second["n_art_courses"], _first["n_art_courses"])
    check("the record says where each verdict came from",
          _second["verdict_sources"], {"cached": 3, "fresh": 1})
    _sh.rmtree(_d23, ignore_errors=True)

    # retroactive: a whole-school verdict from before the change seeds unchanged prefixes
    _d24 = tempfile.mkdtemp()
    for sub in ("courses", "snap", "subj"):
        os.makedirs(os.path.join(_d24, sub))
    _old = _lmu()
    json.dump(_old, open(os.path.join(_d24, "snap", "117946.json"), "w"))
    S.append_cache(os.path.join(_d24, "sc.jsonl"), [{
        "key": S.cache_key(S.subject_blob(S.subject_rows(_old)), _sg),
        "axes": {"ART": "art", "DSGN": "art", "PHOT": "art", "HIST": "out"}}])
    # the re-keyed whole-school verdict that moved the unit: DSGN flipped out
    _new = _lmu(extra_hist=1)
    json.dump({"unitid": "117946", "art_subjects": [
        {"subject": r["subject"], "n": r["n"], "samples": r["samples"],
         "status": "out" if r["subject"] in ("DSGN", "HIST") else "art"}
        for r in S.subject_rows(_new)]}, open(os.path.join(_d24, "subj", "117946.json"), "w"))
    _asked.clear()
    _rest = _run(_d24, _new, seed_from=os.path.join(_d24, "snap"))
    _st = {x["subject"]: x["status"] for x in _rest["art_subjects"]}
    check("nothing is asked: every prefix is seeded", _asked, [])
    check("unchanged DSGN takes its EARLIEST verdict, from the snapshot", _st["DSGN"], "art")
    check("changed HIST takes the current file's verdict", _st["HIST"], "out")
    check("sources recorded", sorted(_rest["verdict_sources"]), ["snapshot snap", "subject file"])
    _sh.rmtree(_d24, ignore_errors=True)

print("\n-- V23: duplicate catalogs, the tier rule, and the three-course minimum")
_dd = tempfile.mkdtemp()
_same = [cc(f"ART {i}", f"Studio {i}", "Shared description.") for i in range(5)]
for u, n in (("185590", "Montclair State University"), ("183822", "Bloomfield College")):
    json.dump({"unitid": u, "name": n, "courses": _same,
               "catalog_url": "http://catalog.montclair.edu"},
              open(os.path.join(_dd, f"{u}.json"), "w"))
json.dump({"unitid": "1", "name": "Other", "courses": [cc("ART 1", "X", "y")],
           "catalog_url": "https://other.edu"}, open(os.path.join(_dd, "1.json"), "w"))
_rr = {"185590": {"webaddr": "www.montclair.edu/", "completions": {"total": 265}},
       "183822": {"webaddr": "www.bloomfield.edu/", "completions": {"total": 52}},
       "1": {"webaddr": "other.edu", "completions": {"total": 100}}}
_dup, _shr = S.find_duplicate_catalogs(_dd, sorted(os.listdir(_dd)), _rr)
check("Bloomfield counted under Montclair", _dup, {"183822": "185590"})
check("the publisher is the host's own institution", _shr, {"185590": ["183822"]})
_rr2 = dict(_rr); _rr2["185590"] = {"webaddr": "", "completions": {"total": 265}}
check("without a domain match, the larger institution publishes",
      S.find_duplicate_catalogs(_dd, sorted(os.listdir(_dd)), _rr2)[0], {"183822": "185590"})
_sh.rmtree(_dd, ignore_errors=True)

if _os.path.exists("rubric_v0.10.json"):
    _R10b = json.load(open("rubric_v0.10.json"))
    _e3 = tempfile.mkdtemp()
    for d in ("courses", "subj", "out"):
        os.makedirs(os.path.join(_e3, d))
    json.dump(_R10b, open(os.path.join(_e3, "r.json"), "w"))
    _sg3 = S.prompt_signature(_R10b, _R10b["classifier"]["model"])
    _rows3, _roster3 = [], {"tier_threshold": 50, "institutions": []}
    for u, nm, tier, n_tech in (("1", "Three Tech", True, 3), ("2", "Two Tech", True, 2),
                                ("3", "Stanford University", False, 5)):
        cs = [cc(f"ART {i}", f"Creative Coding {u}-{i}", "Creative coding for artists. " * 3)
              for i in range(n_tech)] + \
             [cc(f"ART {100 + i}", f"Drawing {u}-{i}", "Observational drawing. " * 3)
              for i in range(120)]
        json.dump({"unitid": u, "name": nm, "n_courses": len(cs), "courses": cs,
                   "description_coverage": 1.0, "platform": "x", "catalog_year": "2026"},
                  open(os.path.join(_e3, "courses", f"{u}.json"), "w"))
        _rows3 += [{"key": S.cache_key(S.course_text(c), _sg3),
                    "axes": ["technology"] if "Coding" in c["title"] else []} for c in cs]
        json.dump({"unitid": u, "art_subjects": [{"subject": "ART", "included": True}],
                   "implausible": False}, open(os.path.join(_e3, "subj", f"{u}.json"), "w"))
        _roster3["institutions"].append({"unitid": u, "name": nm, "in_ranking_tier": tier,
                                          "completions": {"total": 100 if tier else 36}})
    json.dump(_roster3, open(os.path.join(_e3, "roster.json"), "w"))
    S.append_cache(os.path.join(_e3, "c.jsonl"), _rows3)
    _argv = sys.argv
    sys.argv = ["score.py", "--rubric", os.path.join(_e3, "r.json"),
                "--courses", os.path.join(_e3, "courses"),
                "--subject-dir", os.path.join(_e3, "subj"),
                "--cache", os.path.join(_e3, "c.jsonl"),
                "--out-dir", os.path.join(_e3, "out"),
                "--roster", os.path.join(_e3, "roster.json"),
                "--harvest-lock", os.path.join(_e3, ".l")]
    S.LOG.clear()
    S.main()
    sys.argv = _argv
    _ix3 = {r["name"]: r for r in _csv.DictReader(open(os.path.join(_e3, "out", "index.csv"),
                                                        encoding="utf-8"))}
    check("3 technology courses: ranked on technology", _ix3["Three Tech"]["rank_technology"], "1")
    check("2 technology courses: count shown, no rank", (_ix3["Two Tech"]["count_technology"],
          _ix3["Two Tech"]["rank_technology"]), ("2", ""))
    check("and still ranked overall", _ix3["Two Tech"]["ranked"], "True")
    check("below-tier school: full scorecard, reported not ranked",
          (_ix3["Stanford University"]["ranked"], _ix3["Stanford University"]["count_technology"]),
          ("False", ""))
    check("with the stated reason",
          "below the 50-degree ranking tier" in _ix3["Stanford University"]["unranked_reason"], True)
    _stan = json.load(open(os.path.join(_e3, "out", "3.json")))
    check("its scorecard carries its counts all the same",
          _stan["axes"]["technology"]["n_matched_art"], 5)
    _sh.rmtree(_e3, ignore_errors=True)

print("\n-- V24: a CIP-04 scope entry never touches the subject prompt")
if _os.path.exists("rubric_v0.11.json") and _os.path.exists("rubric_v0.10.json"):
    _R10s = json.load(open("rubric_v0.10.json"))
    _R11 = json.load(open("rubric_v0.11.json"))
    _trim, _fu = S.split_scope(_R11["subject_scope"])
    check("the architecture entry is held out of the prompt", len(_fu), 1)
    check("and it is the CIP 04 one", "CIP 04" in _fu[0], True)
    check("the subject signature is unchanged from v0.10",
          S.subject_signature("m", _trim), S.subject_signature("m", _R10s["subject_scope"]))
    check("the untrimmed rubric WOULD have changed it",
          S.subject_signature("m", _R11["subject_scope"])
          == S.subject_signature("m", _R10s["subject_scope"]), False)
    check("the prompt never names architecture",
          "architecture" in S.subject_prompt("X", [], _trim).lower(), False)
    check("the follow-up prompt does, and protects interior design",
          ("architecture" in S.scope_prompt("X", [], _fu[0]).lower()
           and "interior design" in S.scope_prompt("X", [], _fu[0]).lower()), True)

    print("\n-- V24: the follow-up removes a prefix and leaves the rest alone")
    import threading as _th24
    _real24 = S.call_api
    def _fake_scope(prompt, model, key, max_retries=4):
        pf = re.findall(r"^([A-Z]{2,8})  \(", prompt, re.M)
        return json.dumps({p: ("exclude" if p in ("ARCH", "LARC") else "keep") for p in pf})
    _d24 = tempfile.mkdtemp()
    os.makedirs(os.path.join(_d24, "subj"))
    json.dump({"unitid": "216339", "name": "Temple University", "n_courses": 11662,
               "art_subjects": [
                   {"subject": "ARTH", "n": 163, "included": True, "status": "art",
                    "samples": ["American Art to 1900"]},
                   {"subject": "ARCH", "n": 133, "included": True, "status": "art",
                    "samples": ["Architectural Design I", "Structural Analysis"]},
                   {"subject": "LARC", "n": 54, "included": True, "status": "art",
                    "samples": ["Landscape Architecture Studio"]},
                   {"subject": "IDES", "n": 60, "included": True, "status": "art",
                    "samples": ["Interior Design Studio"]},
                   {"subject": "HIST", "n": 400, "included": False, "status": "out",
                    "samples": ["Modern Europe"]}]},
              open(os.path.join(_d24, "subj", "216339.json"), "w"))
    _env = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "test-no-network"
    S.call_api = _fake_scope
    try:
        S.run_classify_scope(_ap.Namespace(
            subject_dir=os.path.join(_d24, "subj"),
            subject_cache=os.path.join(_d24, "sc.jsonl"), schools="", dry_run=False),
            "m", _R11)
    finally:
        S.call_api = _real24
        if _env is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = _env
    _rec24 = json.load(open(os.path.join(_d24, "subj", "216339.json")))
    _st24 = {x["subject"]: x["included"] for x in _rec24["art_subjects"]}
    check("architecture and landscape architecture removed",
          (_st24["ARCH"], _st24["LARC"]), (False, False))
    check("art history and interior design kept", (_st24["ARTH"], _st24["IDES"]),
          (True, True))
    check("a prefix already out is not asked about", _st24["HIST"], False)
    check("removal recorded", _rec24["scope_excluded"], ["ARCH", "LARC"])
    check("the art unit is recomputed", _rec24["n_art_courses"], 223)
    _sh.rmtree(_d24, ignore_errors=True)

print("\n-- V25: v0.12 post_classification_scope, both directions")
if _os.path.exists("rubric_v0.12.json"):
    _R12 = json.load(open("rubric_v0.12.json"))
    _rules = S.followup_rules(_R12)
    check("two rules, direction read from the rubric's own naming",
          [(k, d) for k, d, _ in _rules],
          [("architecture_out", "exclude"), ("film_media_in", "include")])
    check("v0.12 leaves nothing in excluded_families to follow",
          S.split_scope(_R12["subject_scope"])[1], [])
    check("the subject prompt is still v0.10's",
          S.subject_signature("m", S.split_scope(_R12["subject_scope"])[0]),
          S.subject_signature("m", json.load(open("rubric_v0.10.json"))["subject_scope"]))
    _fe = dict(_rules)["film_media_in"] if False else [t2 for k, d, t2 in _rules
                                                       if k == "film_media_in"][0]
    for nm, samp, want in (
            ("Georgia Gwinnett FILM", ["Documentary Filmmaking", "Film History",
                                       "Film Theory"], True),
            ("Oberlin CIME", ["American Cinema 1977-1990",
                              "Experiments in Moving Image and Sound"], True),
            ("Denver MFJS", ["Advanced Multimedia Journalism", "News Reporting"], False),
            ("Old Dominion THEA", ["Acting Five", "Acting for the Camera",
                                   "Animation"], False),
            ("TCU RTVF", ["AM Stations Operations", "Announcing and Voice"], False),
            ("a history prefix", ["Modern Europe", "Ancient Rome"], False)):
        check(f"  boundary: {nm}", S.shape_matches(samp, _fe), want)

    print("\n-- V25: the film rule admits, the architecture rule removes")
    _real25 = S.call_api
    def _fake25(prompt, model, key, max_retries=4):
        pf = re.findall(r"^([A-Z]{2,8})  \(", prompt, re.M)
        # Read the ANSWER FORMAT, not prose: the exclude prompt's own text now
        # contains the word "includes" ("subject matter sometimes includes
        # buildings"), which fooled a keyword sniff and made this fixture answer
        # the wrong vocabulary.
        if '"include" or "keep_out"' in prompt:
            return json.dumps({p: "include" for p in pf})
        return json.dumps({p: ("exclude" if p in ("ARCH", "LARC") else "keep")
                           for p in pf})
    _d25 = tempfile.mkdtemp()
    os.makedirs(os.path.join(_d25, "subj"))
    json.dump({"unitid": "139951", "name": "Georgia Gwinnett College", "n_courses": 1018,
               "art_subjects": [
                   {"subject": "ARTS", "n": 60, "included": True, "status": "art",
                    "samples": ["Drawing I", "Painting"]},
                   {"subject": "ARCH", "n": 30, "included": True, "status": "art",
                    "samples": ["Architectural Design I", "Structural Analysis"]},
                   {"subject": "FILM", "n": 22, "included": False, "status": "out",
                    "samples": ["Documentary Filmmaking", "Film History", "Film Theory"]},
                   {"subject": "COMM", "n": 80, "included": False, "status": "out",
                    "samples": ["News Reporting", "Broadcast News Writing"]},
                   {"subject": "NC", "n": 9, "included": False, "status": "extension",
                    "samples": ["Animation Pre-College", "Film Camp"]}]},
              open(os.path.join(_d25, "subj", "139951.json"), "w"))
    _env = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "test-no-network"
    S.call_api = _fake25
    try:
        S.run_classify_scope(_ap.Namespace(
            subject_dir=os.path.join(_d25, "subj"),
            subject_cache=os.path.join(_d25, "sc.jsonl"), schools="", dry_run=False),
            "m", _R12)
    finally:
        S.call_api = _real25
        if _env is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = _env
    _r25 = json.load(open(os.path.join(_d25, "subj", "139951.json")))
    _s25 = {x["subject"]: x["included"] for x in _r25["art_subjects"]}
    check("film admitted", _s25["FILM"], True)
    check("architecture removed", _s25["ARCH"], False)
    check("journalism never asked, still out", _s25["COMM"], False)
    check("an extension prefix is untouched by either rule", _s25["NC"], False)
    check("studio art unchanged", _s25["ARTS"], True)
    check("both directions recorded",
          (_r25.get("scope_included"), _r25.get("scope_excluded")), (["FILM"], ["ARCH"]))
    check("art unit recomputed: 60 studio + 22 film", _r25["n_art_courses"], 82)

    print("\n-- V29: a scope rule cannot overturn an override")
    _d29 = tempfile.mkdtemp()
    os.makedirs(os.path.join(_d29, "subj"))
    json.dump({"unitid": "140951", "name": "Savannah College of Art and Design",
               "n_courses": 2033,
               "overrides_applied": {"ELDS": {"model": "art", "override": "art"}},
               "art_subjects": [
                   {"subject": "ELDS", "n": 14, "included": True, "status": "art",
                    "samples": ["BIM for Interior Design",
                                "Digital Prototyping for Building Design"]},
                   {"subject": "ARCH", "n": 40, "included": True, "status": "art",
                    "samples": ["Architectural Design I", "Structural Analysis"]}]},
              open(os.path.join(_d29, "subj", "140951.json"), "w"))
    _env = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "test-no-network"
    _real29 = S.call_api
    S.call_api = _fake25
    try:
        S.run_classify_scope(_ap.Namespace(
            subject_dir=os.path.join(_d29, "subj"),
            subject_cache=os.path.join(_d29, "sc.jsonl"), schools="", dry_run=False),
            "m", _R12)
    finally:
        S.call_api = _real29
        if _env is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = _env
    _s29 = {x["subject"]: x["included"] for x in
            json.load(open(os.path.join(_d29, "subj", "140951.json")))["art_subjects"]}
    check("the overridden prefix survives the architecture rule", _s29["ELDS"], True)
    check("and an ordinary architecture prefix is still removed", _s29["ARCH"], False)
    _sh.rmtree(_d29, ignore_errors=True)
    print("\n-- V27: the scope signature is the rendered prompt")
    _e0 = "architecture and landscape architecture (CIP 04)"
    _s_ex = S.scope_signature("m", _e0, "exclude")
    check("direction changes the signature", S.scope_signature("m", _e0, "include") == _s_ex,
          False)
    _orig_sp = S.scope_prompt
    S.scope_prompt = lambda n, r, e, d="exclude": _orig_sp(n, r, e, d) + " EDITED"
    try:
        check("a wording change changes the signature",
              S.scope_signature("m", _e0, "exclude") == _s_ex, False)
    finally:
        S.scope_prompt = _orig_sp
    check("and restoring it restores the signature",
          S.scope_signature("m", _e0, "exclude"), _s_ex)
    _ip = S.scope_prompt("X", [], "film, video and animation (CIP 50.06)", "include")
    check("V27: admission needs a MAJORITY too", "only if MOST of its courses" in _ip, True)
    for _fam in ("ethnic, area and cultural studies", "languages and literature",
                 "journalism", "theatre"):
        check(f"  V27: {_fam} named as kept out", _fam in _ip.lower(), True)

    _ep = S.scope_prompt("X", [], "architecture and landscape architecture (CIP 04)",
                         "exclude")
    check("V26: exclusion needs a MAJORITY, not a mention",
          "only if MOST of its courses" in _ep, True)
    for _fam in ("art history", "fashion", "interior design", "graphic",
                 "studio art", "photography"):
        check(f"  V26: {_fam} named as kept", _fam in _ep.lower(), True)
    _sh.rmtree(_d25, ignore_errors=True)

print("\n-- V33: the subject prompt can be pinned to an earlier rubric")
if _os.path.exists("rubric_v0.14.json") and _os.path.exists("rubric_v0.13.json"):
    _R13 = json.load(open("rubric_v0.13.json")); _R14 = json.load(open("rubric_v0.14.json"))
    _s13 = S.subject_signature("m", S.split_scope(_R13["subject_scope"])[0])
    _s14 = S.subject_signature("m", S.split_scope(_R14["subject_scope"])[0])
    check("v0.14 DOES change the subject prompt (the trap)", _s13 == _s14, False)
    check("pinning to v0.13 restores it", _s13,
          S.subject_signature("m", S.split_scope(_R13["subject_scope"])[0]))
    _rules = {k: d for k, d, _ in S.followup_rules(_R14)}
    check("a rule naming no direction is NOT applied",
          "arts_business_under_education_prefixes" in _rules, False)
    check("and the directioned rules still are",
          sorted(_rules), ["architecture_out", "film_media_in"])

if fails:
    print("\nFAILURES:")
    for f in fails: print("  " + f)
    sys.exit(1)
print("\nAll checks passed.")
