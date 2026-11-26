"""LLM 프롬프트. 검색 품질은 대부분 여기서 결정되므로 고치기 쉽게 한곳에 모아 둔다."""
from __future__ import annotations

import json

DECOMPOSE = """\
You analyse a patent claim to prepare a prior-art search, the way a patent searcher builds a claim chart.

CLAIM (may be in Korean):
<<<
{claim}
>>>

Break the claim into searchable elements.

Rules:
- One element per limitation. Use id "P" for the preamble and the claim's own labels (A, B, ...) for each
  step or component; if the claim has no labels, assign A, B, C in order.
- role: "key" for the one limitation (two at most) that carries the distinguishing feature, the thing a
  searcher must find in a document for it to matter; "core" for other substantive limitations;
  "context" for the preamble and anything every document in the field has (receiving data, outputting
  a result, "a method"). Be sparing with "core": a generic input or output step is "context".
- implementations: some limitations state a result, a function or a negative condition ("free of X",
  "without Y", "such that Z") instead of a concrete means. Prior art describes the means, not the result.
  For each such element list 3-6 concrete techniques by which a skilled person would typically achieve it,
  each with its own English search terms. Cover genuinely different approaches, not rewordings.
  Use an empty list for elements that are already concrete.
- For each element give 2-5 concepts. Each concept has English search terms as they appear in patent
  titles and abstracts: synonyms, broader and narrower wording, abbreviations. Write word stems with a
  trailing * where inflection matters (interpolat*). Each term is one word or a two-word phrase.
- Terms must be ones a different applicant would plausibly use, not only this claim's own wording.
- cpc: 3-8 CPC symbols most likely assigned to documents disclosing this, most specific first,
  formatted like H04N19/593.
- distinguishing_point_ko: one or two sentences on which feature a searcher should prioritise.
- Fields ending in _ko are written in Korean. Everything else in English.

Return exactly this JSON shape:
{{
  "title_en": "short technical title",
  "gist_ko": "발명의 요지 한두 문장",
  "distinguishing_point_ko": "...",
  "elements": [
    {{
      "id": "A",
      "role": "key",
      "text_ko": "청구항의 해당 문구 그대로",
      "summary_ko": "쉽게 풀어쓴 한 문장",
      "summary_en": "one sentence in English",
      "concepts": [
        {{"name_ko": "개념 이름", "terms_en": ["term1", "term2*", "two words"]}}
      ],
      "implementations": [
        {{"name_ko": "수단 이름", "summary_en": "one sentence on the technique", "terms_en": ["term1", "term2*"]}}
      ]
    }}
  ],
  "cpc": [{{"symbol": "H04N19/593", "desc_ko": "분류 설명", "confidence": "high"}}]
}}
"""

CQL_GUIDE = """\
EPO OPS CQL rules (bibliographic search: titles and abstracts only, mostly English):
- An atom is field=term. Fields: ta (title or abstract), ti (title), ab (abstract), cpc (CPC symbol).
- Combine atoms with lowercase and / or / not and parentheses. Example:
  (ta=interpolat* or ta=resampl*) and ta=filter* and cpc=H04N19/593
- Exact phrase: ta="reference sample". No wildcards inside quotes.
- Truncation: * (any number of characters) at the end of a stem with at least 3 letters. No leading wildcard.
- Proximity (use sparingly): ta=(filter* prox/distance<3 coefficient*)
- CPC: cpc=H04N19/593 matches that symbol; cpc=H04N19/593/low also matches its subgroups.
- Hard limits: at most 10 terms per field and 20 terms in the whole query. A quoted phrase counts each word.
- Do not add date conditions; the program adds them.
"""

QUERIES = """\
You write boolean search queries for a prior-art search, as an experienced patent searcher would.

CLAIM ELEMENTS (JSON):
{elements}

{cql_guide}
Write 10-14 OPS queries that together cover the claim, as a matrix from narrow to broad:
- "narrow": the key element plus the core elements, usually with a CPC restriction. The ideal single hit.
- "medium": the key element plus one other element, or the same without CPC (to catch documents classified
  elsewhere), or CPC intersections plus the key element.
- "broad": CPC plus only the key element's terms, or alternative wording for the same idea.
Guidance:
- Every query must express the key element (role "key"). Context elements are optional and are best
  covered by CPC, not keywords.
- If the key element has "implementations", documents will describe one of those means, not the claimed
  result. Write at least one query for each implementation using that implementation's own terms, plus
  one or two queries using the result wording itself. Put the implementation name in rationale_ko.
- Express one concept as an OR-group of its synonyms, and AND the groups together.
- Vary the wording between queries; do not repeat one synonym group in every query.
- Abstracts are short: a term that only appears in claims or descriptions will not match. Prefer the terms
  an abstract would use and keep AND-chains to 2-4 groups.
- Aim for roughly {min_hits}-{max_hits} hits per query. Calibration from real runs in this database: CPC plus
  4-5 AND-groups returns under 10 hits; CPC plus 2-3 groups returns 10-30. So "narrow" queries should use
  about 3 groups, "medium" 2-3 groups with wide OR-groups, and "broad" queries 1-2 keyword groups (with CPC)
  so that they reach 100 or more hits. Too few hits is the common failure, not too many.
- At least two queries must use no CPC restriction, and at least two must rely mainly on CPC.

Also write 2-3 queries for OpenAlex (scholarly papers and standards contributions). OpenAlex syntax:
plain words, quoted phrases, uppercase AND / OR / NOT, parentheses. No field names, no wildcards.
OpenAlex throttles heavy boolean searches: use at most 5 boolean operators per query (2-3 AND-groups with
few alternatives each), and make the three queries cover different wordings instead.

Return exactly this JSON shape:
{{
  "ops_queries": [
    {{"id": "Q1", "level": "narrow", "elements": ["A", "B"], "rationale_ko": "이 검색식의 의도", "cql": "..."}}
  ],
  "openalex_queries": [
    {{"id": "N1", "rationale_ko": "...", "query": "..."}}
  ]
}}
"""

REFINE = """\
You are tuning boolean queries for a prior-art search against EPO OPS. Below is every query run so far
with its hit count or error. Queries marked NEEDS_FIX must be rewritten.

CLAIM ELEMENTS (JSON):
{elements}

{cql_guide}
RESULTS SO FAR:
{results}

Target: {min_hits}-{max_hits} hits per query.
For each NEEDS_FIX query write one replacement:
- status "empty" (0 hits): broaden. Drop the least important AND-group, add synonyms, remove a phrase or
  proximity condition, widen the CPC (use /low or a parent group), or drop the CPC.
- status "few" (hits below the target): its results are already kept, so write a broader sibling that reaches
  further: remove one AND-group, widen OR-groups with more synonyms, or loosen the CPC. Aim for the middle of
  the target range, not just above the minimum.
- status "too_broad": narrow. Add an AND-group from another element, add a CPC, replace a generic stem with
  a phrase, or use proximity.
- status "error": fix the syntax using the error message. If unsure, fall back to plain field=term atoms
  joined with and / or.
Use the hit counts of the other queries as evidence about which terms are productive.
Keep the replacement's purpose the same as the original. If a query cannot be saved, set "cql" to null.

Return exactly this JSON shape:
{{"revisions": [{{"parent": "Q3", "cql": "...", "change_ko": "무엇을 왜 바꿨는지"}}]}}
"""

SCREEN = """\
You screen prior-art candidates against a patent claim using only their titles and abstracts.

CLAIM:
<<<
{claim}
>>>

CLAIM ELEMENTS:
{elements}

For every candidate, score each element:
  0 = the abstract gives no indication of this element
  1 = the abstract mentions part of this element's specific feature, so the full text needs checking
  2 = the abstract explicitly discloses this element or a clear equivalent
Be strict. Being in the same technical field is not a match: score each element on its own specific feature.
If the abstract shows the element's result is obtained by a different mechanism than the claim requires
(for example a value selected from predefined options where the claim derives it from measured data),
score 0 for that element, not 1. Your reason must be consistent with the scores you give.
An abstract is only a summary, so do not penalise missing detail more than that.

Some elements list "means": concrete techniques that would achieve a limitation the claim states only as a
result. They are the searcher's hypotheses, not claim text. For such an element:
  2 = the abstract discloses the claimed result itself, or one of the means applied in the same setting and
      for the same purpose as the element (same kind of input, same kind of processing)
  1 = one of the means appears but in a different setting or for a different purpose, or the result is only
      loosely implied
  0 = neither the result nor any of the means
Write reason_ko in Korean, one sentence, naming what matches (including which means, if any) and what is
missing.

CANDIDATES (JSON):
{candidates}

Return exactly this JSON shape, one entry per candidate, same "i" values:
{{"results": [{{"i": 0, "scores": {{{score_keys}}}, "reason_ko": "..."}}]}}
"""


def compact_elements(decomp: dict) -> str:
    slim = {
        "gist": decomp.get("gist_ko", ""),
        "distinguishing_point": decomp.get("distinguishing_point_ko", ""),
        "elements": [
            {
                "id": e["id"],
                "role": e.get("role", "core"),
                "summary_en": e.get("summary_en", ""),
                "concepts": [c.get("terms_en", []) for c in e.get("concepts", [])],
                "implementations": [
                    {"name": i.get("name_ko", ""), "summary": i.get("summary_en", ""), "terms": i.get("terms_en", [])}
                    for i in e.get("implementations", [])
                ],
            }
            for e in decomp.get("elements", [])
        ],
        "cpc": [c.get("symbol", "") for c in decomp.get("cpc", [])],
    }
    return json.dumps(slim, ensure_ascii=False, indent=1)


def screening_elements(decomp: dict) -> str:
    lines = []
    for e in decomp.get("elements", []):
        lines.append(f'- {e["id"]} ({e.get("role", "core")}): {e.get("summary_en") or e.get("summary_ko", "")}')
        for i in e.get("implementations", []):
            lines.append(f'    means: {i.get("summary_en") or i.get("name_ko", "")}')
    return "\n".join(lines)
