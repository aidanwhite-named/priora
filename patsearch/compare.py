"""청구항–인용발명 구성대비.

역할 분담
- 프로그램: PDF 에서 단락·문장 뽑기, 후보 문장 고르기, 인용발명 선택(주인용 + 결합), 양식 출력.
- LLM: "이 구성의 이 특징이 이 문장에 개시되어 있는가" 판정과 그 문장의 번역, 대응 이유 한 구절.
발췌는 프로그램이 문헌에서 직접 잘라 넣으므로 원문과 항상 일치한다.

판정은 두 번에 나눠 한다.
- 1차(개시): 특징이 문헌에 그대로 적혀 있는가. 이 결과로 주인용발명을 고른다.
- 2차(도출 근거): 1차에서 어느 문헌으로도 메우지 못한 특징만 다시 본다. 그 특징이 없을 때 생기는 문제나
  그 특징을 얻는 수단을 적은 문장이 있으면 "통상의 기술자가 쉽게 도출"하는 근거로 쓴다.
  문장을 짚지 못하면 근거로 인정하지 않는다(문헌 없이 주지관용이라고 쓰지 않는다).

터미널:
    python -m patsearch.compare --claim claims\\claim2.txt --docs a.pdf b.pdf --out compare_out
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import claims, llm, runs
from .config import ROOT, Cache, load_env, load_settings

EXCERPT_LEN = 120  # 원문 발췌 글자 수. 넘으면 … 로 자른다
MAX_REFS_CLAIM1 = 3  # 청구항 1 에 결합할 수 있는 인용발명 수 상한
# 종속항: 인용하는 항에 쓴 인용발명은 그대로 쓰고, 종속항 하나에 새 인용발명은 최대 1개까지 더한다.
MAX_NEW_REFS_PER_DEPENDENT = 1
MAX_DEP_FEATURES = 6  # 종속항 하나에서 나누는 구성 수 상한
CIRCLED = "①②③④⑤⑥⑦⑧⑨"
VERDICT_KO = {"same": "실질적 동일", "partial": "일부 차이", "none": "차이"}
SENT_PROMPT_LEN = 420
MAX_SENTENCES = 45
# 문헌 언어. 검색어는 언어마다 따로 받는다: 영문 terms, 국문 terms_ko, 중문 terms_zh, 일문 terms_ja.
LANG_KO = {"ko": "국문", "en": "영문", "zh": "중문", "ja": "일문"}
LANG_EN = {"zh": "Chinese", "ja": "Japanese"}  # 문헌에 있을 때만 그 언어의 검색어를 더 받는다
TERM_KEYS = {"ko": "terms_ko", "zh": "terms_zh", "ja": "terms_ja"}
_CJK = r"぀-ヿ㐀-鿿"  # 가나와 한자. 띄어쓰기 없이 쓰는 글자


class CompareError(Exception):
    pass


# --- 문헌 읽기 ---------------------------------------------------------------
_ABBR = re.compile(r"(?:\b(?:FIGS?|Nos?|Ser|e\.g|i\.e|etc|al|Inc|Corp|Ltd|vs|cf|approx|Dr|Mr|Ms|U\.S|Eqs?|Sec|Refs?)\s?\.)$", re.I)


def split_sentences(text: str) -> list:
    # 중문·일문은 띄어쓰기 없이 "。" 로 끝나고, 단계 나열은 "；" 로 끊는다
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z가-힣\"“(\[])|(?<=[。！？；])\s*", text)
    out = []
    for part in parts:
        if out and _ABBR.search(out[-1]):
            out[-1] += " " + part
        else:
            out.append(part)
    return [s.strip() for s in out if s.strip()]


_PAGE_NO = re.compile(r"^[-–—\s]*\d{1,4}[-–—\s]*$|^(?:page\s*)?\d{1,4}\s*(?:/|of)\s*\d{1,4}\s*(?:页|頁|쪽)?$", re.I)


def _in_rows(lines: list) -> list:
    """줄 [(x0, y0, x1, y1, 글)] 을 읽는 순서로 편다: 위에서 아래로, 같은 높이에 놓인 조각은 왼쪽부터.

    여백에 따로 찍힌 단락 번호("[0009]")나 줄 끝에서 떨어져 나온 낱말은 PDF 안에서 그 줄 뒤에 적혀 있기도 하다.
    높이로 묶어 왼쪽부터 읽어야 단락 번호가 그 단락의 첫 줄 앞에 온다.
    """
    rows = []
    for line in sorted(lines, key=lambda l: (l[1] + l[3]) / 2):
        if rows:
            ref, mid = rows[-1][0], (line[1] + line[3]) / 2
            if ref[1] <= mid <= ref[3] or line[1] <= (ref[1] + ref[3]) / 2 <= line[3]:
                rows[-1].append(line)
                continue
        rows.append([line])
    return [line[4] for row in rows for line in sorted(row, key=lambda l: l[0])]


def _page_lines(page) -> list:
    """한 쪽의 줄들을 읽는 순서로 돌려준다. 2단 편집은 왼쪽 단 → 오른쪽 단, 쪽 머리말과 쪽 번호는 버린다."""
    width, height = page.rect.width, page.rect.height
    full, left, right = [], [], []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue  # 그림 블록
        x0, y0, x1, y1 = block["bbox"]
        lines = []
        for line in block["lines"]:
            text = " ".join("".join(span["text"] for span in line["spans"]).split())
            lx0, ly0, lx1, ly1 = line["bbox"]
            if not text or ((ly0 > 0.85 * height or ly1 < 0.08 * height) and _PAGE_NO.match(text)):
                continue  # 쪽 번호("- 3 -", "14", "3/22 页")
            lines.append((lx0, ly0, lx1, ly1, text))
        if not lines or (y1 < 0.065 * height and sum(len(line[4]) for line in lines) < 120):
            continue
        if (x1 - x0) > 0.6 * width:
            full.append((y0, lines))
        elif (x0 + x1) / 2 < width / 2:
            left.append((y0, lines))
        else:
            right.append((y0, lines))

    def size(group):
        return sum(len(line[4]) for _, lines in group for line in lines)

    def flat(group):
        return _in_rows([line for _, lines in group for line in lines])

    # 2단: 좁은 블록이 양쪽에 있고, 글의 대부분이 거기에 있을 때만. 1단 문헌도 단락의 짧은 끝줄은 좁은 블록이다
    if len(left) < 2 or len(right) < 2 or size(left) + size(right) < size(full):
        return flat(full + left + right)
    first = min(y for y, _ in left + right)
    return (flat([b for b in full if b[0] <= first]) + flat(left) + flat(right)
            + flat([b for b in full if b[0] > first]))


def _join_lines(lines: list, vocab: dict) -> str:
    """줄을 이어 붙인다. 줄 끝에서 끊긴 단어("learn" + "ing")는 문헌 안에 붙은 형태가 따로 나올 때만 붙인다."""
    out = ""
    for line in lines:
        if not out:
            out = line
            continue
        if re.match(f"[{_CJK}，。、；：）】》]", line) and re.search(f"[{_CJK}，、（【《]$", out):
            out += line  # 중문·일문은 낱말 사이를 띄지 않는다
            continue
        a = re.search(r"([A-Za-z]+)(-?)$", out)
        b = re.match(r"[a-z]+", line)
        if a and b:
            joined = (a.group(1) + b.group(0)).lower()
            if a.group(2):  # 줄 끝 하이픈: 붙은 단어가 있으면 하이픈을 떼고, 없으면 하이픈만 남긴다
                out = (out[:-1] if vocab.get(joined, 0) >= 1 else out) + line
                continue
            # 붙인 형태가 문헌에 여러 번 나오거나, 앞 조각이 혼자서는 거의 안 쓰이는 조각일 때 붙인다
            seen = vocab.get(joined, 0)
            if len(joined) >= 6 and (seen >= 2 or (seen >= 1 and vocab.get(a.group(1).lower(), 0) <= 2)):
                out += line
                continue
        out += " " + line
    return out


def _display_name(path: Path, text: str) -> str:
    m = re.match(r"^(US|KR|EP|WO|JP|CN)0?(\d{6,13})([AB]\d?)", path.stem, flags=re.I)
    if m:
        return f"{m.group(1).upper()}{m.group(2)}{m.group(3).upper()}"
    return path.stem[:70]


def load_document(path: Path, doc_id: str = "", name: str = "") -> dict:
    """PDF 를 단락 목록으로 바꾼다. 단락: {loc, page, sentences}. loc 은 '0019'(단락 번호) 또는 쪽 번호."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise CompareError("PDF 를 읽으려면 PyMuPDF 가 필요합니다: pip install pymupdf") from None
    try:
        pdf = fitz.open(str(path))
    except Exception as e:
        raise CompareError(f"PDF 를 열 수 없습니다: {path.name} ({e})") from None
    page_lines = [_page_lines(p) for p in pdf]
    pdf.close()
    vocab: dict = {}
    for lines in page_lines:
        for word in re.findall(r"[a-z]{3,}", " ".join(lines).lower()):
            vocab[word] = vocab.get(word, 0) + 1
    pages = [_join_lines(lines, vocab) for lines in page_lines]
    text, starts = "", []
    for page_text in pages:
        starts.append(len(text))
        text += page_text + " "
    if len(text.strip()) < 500:
        raise CompareError(f"{path.name}: 텍스트 층이 없는 스캔 PDF 로 보입니다. 텍스트가 들어 있는 PDF 가 필요합니다.")

    def page_of(offset: int) -> int:
        page = 0
        for i, start in enumerate(starts):
            if start <= offset:
                page = i
        return page + 1

    hangul, kana, han = (len(re.findall(p, text)) for p in (r"[가-힣]", r"[぀-ヿ]", r"[㐀-鿿]"))
    lang = ("ko" if hangul > 0.15 * len(text) else "ja" if kana > 0.03 * len(text) and kana + han > 0.15 * len(text)
            else "zh" if han > 0.15 * len(text) else "en")
    # 단락 번호는 네 자리가 보통이지만 100 번째부터 다섯 자리로 적는 문헌이 있다([0099] 다음이 [00100])
    marks = list(re.finditer(r"\[\s*(\d{4,5})\s*\]", text))
    paragraphs = []
    if len(marks) >= 8:  # 단락 번호가 있는 특허 문헌
        kind = "patent"
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            body = text[m.end():end].strip()
            if i + 1 == len(marks):  # 마지막 단락 뒤에 붙은 청구범위는 잘라낸다
                body = re.split(r"What is claimed|We claim|I claim|What is claimed is|청구범위|【청구항|权利要求书|【請求項", body)[0].strip()
            sentences = split_sentences(body)
            if sentences:
                paragraphs.append({"loc": m.group(1), "page": page_of(m.start()), "sentences": sentences})
    else:  # 논문 등: 쪽 단위로 문장을 묶는다
        kind = "paper"
        for i, page_text in enumerate(pages):
            sentences = split_sentences(page_text)
            for j in range(0, len(sentences), 5):
                paragraphs.append({"loc": str(i + 1), "page": i + 1, "sentences": sentences[j:j + 5]})
    if not paragraphs:
        raise CompareError(f"{path.name}: 본문을 찾지 못했습니다")
    return {
        "id": doc_id or path.stem, "name": name or _display_name(path, text), "file": path.name,
        "kind": kind, "lang": lang, "pages": len(pages), "paragraphs": paragraphs,
    }


def locator(doc: dict, para: dict) -> str:
    return f'단락 [{para["loc"]}]' if doc["kind"] == "patent" else f'{para["page"]}쪽'


# --- 청구항 구성 ---------------------------------------------------------------
def split_claim(claim: str) -> list:
    """(A) (B) … 표지로 구성을 나눈다. 표지가 없으면 세미콜론·줄 단위로 나눠 A, B, C 를 붙인다."""
    body = " ".join(claim.split())
    marks = list(re.finditer(r"\(([A-Za-z가-힣])\)\s*", body))
    elements = []
    if len(marks) >= 2:
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
            elements.append({"id": m.group(1).upper(), "text": body[m.end():end]})
    else:
        body = re.sub(r"^청구항\s*\d+\s*[.:]?\s*", "", body)
        parts = [p for p in re.split(r"\s*;\s*", body) if len(p.strip()) > 8]
        elements = [{"id": chr(ord("A") + i), "text": p} for i, p in enumerate(parts[:9])]
    for e in elements:
        e["text"] = re.sub(r"[;,]?\s*(및|그리고)?\s*$", "", e["text"]).strip()
    if not elements:
        raise CompareError("청구항에서 구성을 나누지 못했습니다")
    return elements


FEATURES_PROMPT = """\
Split each element of this patent claim into its distinct features, for comparison with prior art.

CLAIM:
<<<
{claim}
>>>

ELEMENTS:
{elements}

A feature is one technical requirement that a prior-art document must show.
- Most elements have exactly ONE feature: the element as a whole. Use one unless the rule below applies.
- Split into two or three only when the element contains separate technical requirements that could be found
  in different documents, for example an action plus a separate condition on its result
  ("…를 생성하는 단계 - 상기 …에는 …가 없음" is two requirements: generating it, and the condition).
- Never make a feature out of a modifier: wording that refers back to an earlier element ("상기 …에 기초하여"),
  or that only describes the data ("객체를 묘사하는", "이전에 압축된"). Keep modifiers inside the feature they belong to.
  A step that is based only on things the claim has already introduced stays ONE feature:
  "상기 기준값에 기초하여 출력을 설정하는 단계" is one feature. Where the step happens (a processor, a memory)
  is never a feature of its own either.
- The one exception: a "based on / using / according to" clause that names something appearing in the claim for
  the first time there (a new input, parameter or condition, written without "상기"), as in "상기 제1 값 및 장치의
  설정 매개변수에 기초하여 기준값을 결정하는 단계". Using that new input is a separate technical requirement,
  because a document can perform the step without it. Then make two features: the step itself
  ("기준값을 결정하는 단계") and the use of the new input ("장치의 설정 매개변수에 기초하여 기준값을 결정하는 것").
For each feature:
- "phrase": a self-contained Korean noun phrase in the claim's own wording that names the requirement
  (keep the claim's ending, for example "~하는 단계"). It must make sense when read alone.
- "terms": 8-12 English words or stems an English document would use for it (put * after a stem).
- "terms_ko": 4-8 Korean words a Korean document would use for it.{more_terms}

Return JSON only:
{{"elements": [{{"id": "A", "features": [{{"phrase": "...", "terms": ["..."], "terms_ko": ["..."]{more_json}}}]}}]}}
"""

JUDGE_PROMPT = """\
Compare one patent claim element with sentences taken from one prior-art document.

CLAIM ELEMENT ({eid}): {etext}

FEATURES of this element:
{features}

PRIOR-ART SENTENCES:
{sentences}

For each feature, decide whether one of the sentences discloses it.
- Disclosed: the sentence shows the same technical content, even in different words or as a specific example
  of it. Judge the substance, the way a patent examiner reads "substantially the same". Words that only refer
  back to other parts of the claim ("상기", "said") do not need their own support.
- Not disclosed: the sentences are about something else, or only share words with the feature.
Choose a sentence that describes what the system or method does. Avoid figure captions, table rows and
reference-list entries.

Return JSON only, one entry per feature:
{{"features": [{{"n": 1, "sentence": 12, "translation_ko": "...", "why_ko": "..."}}, {{"n": 2, "sentence": null}}]}}
- "sentence": the number of the one sentence that best discloses the feature, or null if none does.
- "translation_ko": {translation_rule}
- "why_ko": one Korean clause that begins with "인용발명의" and ends with "므로", naming what in the document
  does what the feature requires. Example: "인용발명의 분할 네트워크는 입력 영상의 각 화소에 대해 라벨을 예측하므로"
"""

# 2차 판정 준비(분석 모델, 빈 특징 하나에 1회): 무엇을 찾아야 하는지 풀어 쓴다.
HINTS_PROMPT = """\
One feature of a patent claim was not found stated directly in the prior art. Say what an examiner should look
for instead, to show that a skilled person could easily arrive at the feature.

CLAIM:
<<<
{claim}
>>>

ELEMENT ({eid}): {etext}
FEATURE: {phrase}

Return JSON only:
{{"problem": "...", "means": ["...", "..."], "terms": ["..."], "terms_ko": ["..."]{more_json}}}
- "problem": one plain English sentence: what goes wrong when this feature is missing.
- "means": 3-5 short English phrases: known kinds of techniques that give the result the feature requires.
- "terms": 15-20 English words or stems (put * after a stem) that a document would use when it describes that
  problem or those techniques. Cover both: words for the failure, and words for the techniques.
- "terms_ko": 6-10 Korean words for the same.{more_terms}
"""

# 2차 판정(가벼운 모델, 문헌 × 빈 특징마다 1회)
SUGGEST_PROMPT = """\
A patent claim feature is not stated directly in a prior-art document. Check whether the document still
describes (P) the problem behind the feature, or (M) a way to get the result the feature requires.

CLAIM ELEMENT ({eid}): {etext}
FEATURE: {phrase}

P. Problem to look for: {problem}
M. Ways to get the result, for example: {means}

PRIOR-ART SENTENCES:
{sentences}

Rules:
- Pick a sentence only when it really states P or M, for the same kind of data and the same kind of task as
  the claim element. Sharing words is not enough. When in doubt, answer null.
- M must be something the document does or reports as working, not a wish or future work.
- Avoid figure captions, table rows and reference-list entries.

Return JSON only:
{{"problem": 7, "problem_ko": "...", "means": 8, "means_ko": "...", "why_ko": "..."}}
- "problem": the number of the sentence that best states P, or null.
- "means": the number of the sentence that best states M, or null.
- "problem_ko", "means_ko": {translation_rule}
- "why_ko": one Korean clause that begins with "인용발명의" and ends with "므로", saying how what the document
  does leads to the feature. Empty when "means" is null.
"""

# 2차 판정 마무리(분석 모델, 빈 특징 하나에 1회): 가벼운 모델이 짚은 문장을 걸러 내고 강약을 매긴다.
RANK_PROMPT = """\
A patent claim feature is not stated directly in the prior art. A first reader went through each document and
picked sentences that might let a skilled person arrive at the feature. Check every pick strictly.

CLAIM ELEMENT ({eid}): {etext}
FEATURE: {phrase}

P. Problem behind the feature: {problem}
M. Ways to get the result the feature requires, for example: {means}

PICKS:
{picks}

For each pick decide:
- "ok": true only if the means sentence describes a technique for the same kind of data and the same kind of
  task as the claim element, so that using it would give the result the feature requires. false if it is about
  a different kind of data or a different kind of defect, or if it only shares words with the feature.
- "strength" (only when ok):
  "direct"   - the sentence is about the very thing the feature limits (the result named in the feature itself).
  "indirect" - the sentence improves something upstream or related, and the result would follow from it.
- "reason_ko": one short Korean sentence giving the reason.

Return JSON only:
{{"picks": [{{"n": 1, "ok": true, "strength": "direct", "reason_ko": "..."}}], "best": 1}}
- "best": the number of the ok pick that gives the strongest basis, or null if no pick is ok.
"""

# 종속항(분석 모델, 종속항 하나에 1회): 인용하는 항에 더해진 구성만 뽑고, "상기 …"가 가리키는 것을 풀어 쓴다.
DEP_FEATURES_PROMPT = """\
A dependent patent claim adds limitations to the claims it refers to. List what this dependent claim adds, as
features to compare with prior art.

CLAIMS IT REFERS TO, directly or through other claims (context only):
<<<
{context}
>>>

DEPENDENT CLAIM {no} (refers to {refs}):
<<<
{text}
>>>

A feature is one added technical requirement that a prior-art document must show.
- Take only what this claim adds. Do not repeat requirements that are already in the claims it refers to.
- Make one feature for each separately stated requirement: "A는 B를 포함하고, C는 D를 포함하는" is two
  features, and a list of steps or operations is one feature per step. Most dependent claims add one or two.
  At most {limit}; if there are more, merge the closest ones.
- A term that begins with "상기" points back to the claims above. Read it the way the claim that introduces it
  defines it. When the claim refers to several claims in the alternative ("제1항 내지 제3항 중 어느 한 항"),
  use the one that introduces the term.
For each feature:
- "phrase": the requirement in the claim's own Korean wording, without "상기", written as a clause that the
  word "구성" can be attached to. End it in one of two ways only:
  (a) with the bare stem of a 하다 verb, so that "하는 구성" can follow it directly:
      "제어부는 마이크로컨트롤러를 포함", "측정값은 대상의 온도를 표시", "기준 영상을 수신";
  (b) when the verb is passive or describes a state, with its modifier form ending in "는" or "된", so that
      "구성" can follow it directly: "오차 항은 제외되는", "덮개는 본체와 연결된", "출력에 잡음이 없는".
  Leave out the closing category word of the claim ("…, 방법.", "…, 장치.").
  Keep every limitation of that requirement in the phrase, also one that the claim attaches after it
  ("…를 수신하는 단계로서, 상기 …는 …를 나타내는" is "…를 나타내는 …를 수신"). Never leave a limitation
  to "meaning" alone.
- "meaning": one Korean sentence that states the same requirement so that it can be understood without reading
  the other claims: replace each back-reference with what it refers to there.
- "terms": 8-12 English words or stems an English document would use for it (put * after a stem).
- "terms_ko": 4-8 Korean words a Korean document would use for it.{more_terms}

Return JSON only:
{{"features": [{{"phrase": "...", "meaning": "...", "terms": ["..."], "terms_ko": ["..."]{more_json}}}]}}
"""

# 종속항 판정(가벼운 모델, 문헌 × 종속항마다 1회)
DEP_JUDGE_PROMPT = """\
Compare what a dependent patent claim adds with sentences taken from one prior-art document.

CLAIMS IT REFERS TO (context only; they are compared separately, so do not look for support for them):
<<<
{context}
>>>

DEPENDENT CLAIM {no}: {text}

FEATURES this claim adds:
{features}
{known}
PRIOR-ART SENTENCES:
{sentences}

For each feature, decide whether one of the sentences discloses it.
- Disclosed: the sentence shows the same technical content for the thing the feature limits, even in different
  words or as a specific example of it. A document that names the required item as an example or option of
  its own corresponding part discloses the feature. Judge the substance, the way a patent examiner reads
  "substantially the same".
- Every limitation of the feature counts: what a value is determined from or based on, the condition under
  which a step is performed, and what the data must be. A sentence that only shows that the resulting item
  exists does not disclose how it is determined: "a threshold is stored and applied" does not disclose
  "determining the threshold based on the measured temperature". Placing, showing or using an object is not
  determining (measuring, computing) its position.
- A word that is the same but means something else does not count: the "model" of a device as its make or
  product name is not a mathematical model of the device.
- If you could explain the match only with a reservation ("…을 명시하지는 않지만", "직접 기재하지는 않으나"),
  the feature is not disclosed: answer null. Do not pick the closest sentence for the sake of having one.
- Judge the same way every time: when two features ask for the same thing, they get the same answer.
- Not disclosed: the sentences are about something else, or only share words with the feature.
Choose a sentence that describes what the system or method is or does. Avoid figure captions, table rows and
reference-list entries.

Return JSON only, one entry per feature:
{{"features": [{{"n": 1, "sentence": 12, "missing_ko": "", "translation_ko": "...", "why_ko": "..."}}, {{"n": 2, "sentence": null}}]}}
- "sentence": the number of the one sentence that best discloses the feature, or null if none does.
- "missing_ko": check the chosen sentence against the feature part by part, then name in a few Korean words any
  part the sentence does not show (a limitation, what a value is determined from, a condition). Leave it empty
  only when the sentence shows the whole feature. Words that only point back to the claims above ("상기 …")
  need no support and are never missing. A feature with something missing is treated as not disclosed.
- "translation_ko": {translation_rule}
- "why_ko": one Korean clause that begins with "인용발명은" and ends with "므로". Say what the document states
  in that sentence and why that amounts to the feature. "므로" must be its last word, with nothing after it.
  It must not say that the document lacks or does not state something.
  Example: "인용발명은 제어부의 예로 마이크로컨트롤러를 들어 제어부가 프로세서를 포함할 수 있음을 기재하므로"
"""


def extra_langs(docs: list) -> list:
    """국문·영문이 아닌 문헌의 언어들. 이 언어의 검색어를 LLM 에게 더 받아야 그 문헌에서 후보 문장을 고를 수 있다."""
    return sorted({d.get("lang") for d in docs} & set(LANG_EN))


def _more_terms(langs) -> dict:
    """프롬프트에 끼울 말. 중문·일문 문헌이 없으면 빈 글이라 프롬프트가 달라지지 않는다."""
    return {
        "more_terms": "".join(f'\n- "{TERM_KEYS[l]}": 6-10 {LANG_EN[l]} words a {LANG_EN[l]} patent document would use for it.'
                              for l in langs),
        "more_json": "".join(f', "{TERM_KEYS[l]}": ["..."]' for l in langs),
    }


def _terms(row: dict, langs) -> dict:
    keys = ["terms", "terms_ko"] + [TERM_KEYS[l] for l in langs]
    return {k: [str(t) for t in row.get(k) or []] for k in keys}


def warn_no_terms(docs: list, holders: list, say=print) -> None:
    """문헌 언어의 검색어가 하나도 없으면 후보 문장을 고르지 못해 전부 "대응 없음"이 된다. 조용히 넘어가지 않게 알린다."""
    for doc in docs:
        key = TERM_KEYS.get(doc.get("lang"), "terms")
        if holders and not any(f.get(key) for h in holders for f in h.get("features") or []):
            say(f'  ! {doc["name"]}: {LANG_KO.get(doc.get("lang"), "이 언어")} 검색어가 없어 이 문헌은 대비하지 못합니다. '
                f"구성대비를 처음부터 다시 실행하세요")


def claim_features(claim: str, elements: list, model: str, cache, langs=()) -> list:
    listing = "\n".join(f'({e["id"]}) {e["text"]}' for e in elements)
    answer = llm.ask_json(FEATURES_PROMPT.format(claim=claim, elements=listing, **_more_terms(langs)), model=model, cache=cache)
    by_id = {str(x.get("id", "")).upper(): x for x in (answer.get("elements") or []) if isinstance(x, dict)}
    out = []
    for e in elements:
        feats = []
        for f in (by_id.get(e["id"], {}).get("features") or [])[:3]:
            phrase = str(f.get("phrase") or "").strip()
            if phrase:
                feats.append({"phrase": phrase, **_terms(f, langs)})
        if not feats:  # LLM 이 빼먹은 구성은 구성 전체를 하나의 특징으로 본다
            feats = [{"phrase": e["text"], "terms": [], "terms_ko": []}]
        out.append({"id": e["id"], "text": e["text"], "features": feats})
    return out


# --- 후보 문장 고르기 (프로그램) ------------------------------------------------
def _term_regex(term: str):
    term = term.strip().lower()
    if not term:
        return None
    if re.search(f"[가-힣{_CJK}]", term):
        return re.compile(re.escape(term.rstrip("*")))
    words = [re.escape(w.rstrip("*")) + (r"\w*" if w.endswith("*") else "") for w in term.split()]
    return re.compile(r"\b" + r"[\s\-]+".join(words) + (r"" if term.endswith("*") else r"(?:s|es)?\b"))


def _scores(doc: dict, terms: list) -> list:
    regexes = [r for r in (_term_regex(t) for t in terms) if r is not None]
    texts = [" ".join(p["sentences"]).lower() for p in doc["paragraphs"]]
    counts = [[len(r.findall(t)) for r in regexes] for t in texts]
    n = len(texts)
    weights = [math.log(1 + n / (1 + sum(1 for c in counts if c[k]))) for k in range(len(regexes))]
    return [sum(min(c[k], 3) * weights[k] for k in range(len(regexes))) for c in counts]


def _usable(sentence: str, shortest: int = 25) -> bool:
    """발췌로 쓸 만한 문장인가. 그림 설명·표·참고문헌 줄은 거른다."""
    if not shortest <= len(sentence) <= 600:
        return False
    if re.match(r"^\W*\[?\d+\]?\W+[A-Z]\.\s", sentence) or re.search(r"\bTABLE\b|\bet al\.,", sentence):
        return False
    if re.match(r"^(FIG|Fig|Figure|FIGURE|Table)\b", sentence):
        return False
    digits = sum(ch.isdigit() for ch in sentence)
    return digits <= 0.12 * len(sentence)


def candidate_sentences(doc: dict, element: dict) -> list:
    """구성의 특징별로 관련 단락을 고르고, 그 안의 문장을 [{para, sent, text}] 로 돌려준다."""
    key = TERM_KEYS.get(doc["lang"], "terms")
    shortest = 10 if doc["lang"] in LANG_EN else 25  # 중문·일문은 같은 내용이 훨씬 짧다
    sentences = [
        (pi, si, s) for pi, p in enumerate(doc["paragraphs"]) for si, s in enumerate(p["sentences"]) if _usable(s, shortest)
    ]
    lowered = [s.lower() for _, _, s in sentences]
    quota = max(8, MAX_SENTENCES // max(1, len(element["features"])))
    chosen = set()
    for feature in element["features"]:
        regexes = [r for r in (_term_regex(t) for t in feature.get(key) or []) if r is not None]
        if not regexes:
            continue
        hit = [[bool(r.search(text)) for r in regexes] for text in lowered]
        df = [sum(1 for h in hit if h[k]) for k in range(len(regexes))]
        # 드문 검색어일수록 무겁게, 서로 다른 검색어를 여럿 담은 문장일수록 위로
        weight = [math.log(1 + len(lowered) / (1 + d)) for d in df]
        score = [sum(w for w, h in zip(weight, row) if h) * (1 + 0.3 * (sum(row) - 1)) if any(row) else 0.0 for row in hit]
        ranked = sorted(range(len(score)), key=lambda i: -score[i])
        chosen.update(i for i in ranked[:quota] if score[i] > 0)
    return [{"para": sentences[i][0], "sent": sentences[i][1], "text": sentences[i][2]} for i in sorted(chosen)][:MAX_SENTENCES + 15]


# --- 판정 (LLM) ---------------------------------------------------------------
def judge(doc: dict, element: dict, model: str, cache) -> list:
    """특징별 판정 [{disclosed, para, sent, translation_ko, why_ko}] 를 돌려준다."""
    empty = [{"disclosed": False} for _ in element["features"]]
    cands = candidate_sentences(doc, element)
    if not cands:
        return empty
    rule = ("leave it empty; the document is already Korean." if doc["lang"] == "ko"
            else "that sentence translated into Korean, faithfully and completely, as one sentence.")
    prompt = JUDGE_PROMPT.format(
        eid=element["id"], etext=element["text"],
        features="\n".join(f'{i + 1}. {f["phrase"]}' for i, f in enumerate(element["features"])),
        sentences="\n".join(f'[{i + 1}] {c["text"][:SENT_PROMPT_LEN]}' for i, c in enumerate(cands)),
        translation_rule=rule,
    )
    return _read_judgment(llm.ask_json(prompt, model=model, cache=cache), cands, len(element["features"]))


# 대응 이유에 "…을 명시하지는 않지만" 같은 단서가 붙으면 그대로 개시된 것이 아니다
_HEDGE = re.compile(r"(?:명시|기재|개시|언급|설명|제시)(?:하고 있지|하지|되어 있지|되지)(?:는|도)?\s*(?:않|아니|못)"
                    r"|(?:나타나|드러나)지(?:는|도)?\s*않|에 그[친치]")


def _read_judgment(answer, cands: list, count: int, strict: bool = False) -> list:
    """판정 응답을 특징별 [{disclosed, para, sent, translation_ko, why_ko}] 로 바꾼다. 문장 번호가 틀리면 미개시.

    strict: 대응 이유가 스스로 "명시하지 않는다"고 적은 판정은 개시로 보지 않는다(양식에 "…명시하지 않으므로
    …대응됩니다"처럼 앞뒤가 안 맞는 글이 나가지 않게 한다).
    """
    rows = answer.get("features") if isinstance(answer, dict) else answer
    by_n = {}
    for r in rows or []:
        try:
            by_n[int(r["n"])] = r
        except (KeyError, TypeError, ValueError):
            continue
    out = []
    for i in range(count):
        r = by_n.get(i + 1) or {}
        try:
            idx = int(r.get("sentence")) - 1
        except (TypeError, ValueError):
            idx = -1
        if not 0 <= idx < len(cands):
            out.append({"disclosed": False})
            continue
        missing = str(r.get("missing_ko") or "").strip()
        if strict and (missing or _HEDGE.search(str(r.get("why_ko") or ""))):
            # 가장 가까운 문장은 찾았지만 구성의 일부가 거기에 없다. 무엇이 없는지를 남겨 둔다
            out.append({"disclosed": False, "hedged": (missing or str(r.get("why_ko") or "").strip())[:300]})
            continue
        out.append({
            "disclosed": True, "para": cands[idx]["para"], "sent": cands[idx]["sent"],
            "translation_ko": str(r.get("translation_ko") or "").strip(),
            "why_ko": str(r.get("why_ko") or "").strip(),
        })
    return out


def judge_all(docs: list, elements: list, model: str, cache, say=print, workers: int = 4) -> dict:
    """judgments[doc_id][element_id] = 특징별 판정 목록"""
    tasks = [(d, e) for d in docs for e in elements]

    def run(task):
        doc, element = task
        try:
            return judge(doc, element, model, cache)
        except llm.LLMError as e:
            say(f'  ! {doc["name"]} ({element["id"]}) 판정 실패: {e}')
            return [{"disclosed": False, "error": str(e)[:200]} for _ in element["features"]]

    judgments = {d["id"]: {} for d in docs}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (doc, element), result in zip(tasks, pool.map(run, tasks)):
            judgments[doc["id"]][element["id"]] = result
            say(f'  {doc["name"]} ({element["id"]}) {VERDICT_KO[verdict(result)]}')
    return judgments


# --- 2차 판정: 빈 특징의 도출 근거 (LLM) -----------------------------------------
def _translation_rule(doc: dict) -> str:
    return ("leave it empty; the document is already Korean." if doc["lang"] == "ko"
            else "that sentence translated into Korean, faithfully and completely, as one sentence.")


def feature_hints(claim: str, element: dict, index: int, model: str, cache, langs=()) -> dict:
    """빈 특징 하나에 대해 '무엇을 찾을지'(문제, 수단, 검색어)를 받는다."""
    answer = llm.ask_json(HINTS_PROMPT.format(claim=claim, eid=element["id"], etext=element["text"],
                                              phrase=element["features"][index]["phrase"], **_more_terms(langs)),
                          model=model, cache=cache)
    answer = answer if isinstance(answer, dict) else {}
    return {
        "problem": str(answer.get("problem") or "").strip(),
        "means": [str(m).strip() for m in answer.get("means") or [] if str(m).strip()][:5],
        **_terms(answer, langs),
    }


def suggest(doc: dict, element: dict, index: int, hints: dict, model: str, cache) -> dict | None:
    """문헌 하나에서 도출 근거를 찾는다. 수단을 적은 문장이 있어야 근거로 인정한다.

    돌려주는 값: {problem: {para, sent, translation_ko} | None, means: {…}, why_ko, source}
    """
    cands = candidate_sentences(doc, {"features": [hints]})
    if not cands:
        return None
    prompt = SUGGEST_PROMPT.format(
        eid=element["id"], etext=element["text"], phrase=element["features"][index]["phrase"],
        problem=hints["problem"] or "(not given)", means="; ".join(hints["means"]) or "(not given)",
        sentences="\n".join(f'[{i + 1}] {c["text"][:SENT_PROMPT_LEN]}' for i, c in enumerate(cands)),
        translation_rule=_translation_rule(doc),
    )
    answer = llm.ask_json(prompt, model=model, cache=cache)
    answer = answer if isinstance(answer, dict) else {}

    def pick(key: str):
        try:
            idx = int(answer.get(key)) - 1
        except (TypeError, ValueError):
            return None
        if not 0 <= idx < len(cands):
            return None
        return {"para": cands[idx]["para"], "sent": cands[idx]["sent"],
                "translation_ko": str(answer.get(key + "_ko") or "").strip()}

    means = pick("means")
    if means is None:
        return None
    return {"problem": pick("problem"), "means": means, "why_ko": str(answer.get("why_ko") or "").strip(), "source": "llm"}


def suggest_gaps(claim: str, docs: list, elements: list, judgments: dict, gaps: list, model: str, hint_model: str,
                 cache, say=print, workers: int = 4) -> dict:
    """메우지 못한 특징마다 모든 문헌에서 도출 근거를 찾아 judgments[…]["suggested"] 에 적는다."""
    by_element = {e["id"]: e for e in elements}
    hints = {}
    for gap in gaps:
        element = by_element[gap["element"]]
        key = f'{gap["element"]}.{gap["feature"]}'
        try:
            hints[key] = feature_hints(claim, element, gap["feature"], hint_model, cache, langs=extra_langs(docs))
        except llm.LLMError as e:
            say(f"  ! ({key}) 찾을 내용 정리 실패: {e}")
            continue
        say(f'  ({gap["element"]}) {element["features"][gap["feature"]]["phrase"]}')
        say(f'    문제: {hints[key]["problem"]}')
        say(f'    수단: {"; ".join(hints[key]["means"])}')
    tasks = [
        (doc, gap) for gap in gaps if f'{gap["element"]}.{gap["feature"]}' in hints for doc in docs
        if not judgments[doc["id"]][gap["element"]][gap["feature"]].get("suggested")
    ]

    def run(task):
        doc, gap = task
        try:
            return suggest(doc, by_element[gap["element"]], gap["feature"],
                           hints[f'{gap["element"]}.{gap["feature"]}'], model, cache)
        except llm.LLMError as e:
            say(f'  ! {doc["name"]} ({gap["element"]}) 도출 근거 판정 실패: {e}')
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (doc, gap), found in zip(tasks, pool.map(run, tasks)):
            if found:
                judgments[doc["id"]][gap["element"]][gap["feature"]]["suggested"] = found
            say(f'  {doc["name"]} ({gap["element"]}) {"근거 후보 있음" if found else "근거 없음"}')
    for gap in gaps:
        key = f'{gap["element"]}.{gap["feature"]}'
        if key in hints:
            rank_suggestions(docs, by_element[gap["element"]], gap["feature"], hints[key], judgments, hint_model, cache, say)
    return hints


SUGGEST_WEIGHT = {"manual": 4, "best": 3, "direct": 2, "indirect": 1}


def rank_suggestions(docs: list, element: dict, index: int, hints: dict, judgments: dict, model: str, cache, say=print) -> None:
    """가벼운 모델이 문헌별로 짚은 근거를 분석 모델이 한 번에 보고, 근거가 안 되는 것은 빼고 강약을 매긴다.

    결과는 각 근거에 적는다: rejected(빼기), strength(direct|indirect), weight(인용발명 고를 때의 우선순위), reason_ko.
    사람이 직접 넣은 근거(source: manual)는 건드리지 않는다.
    """
    picks = []
    for doc in docs:
        found = judgments[doc["id"]][element["id"]][index].get("suggested")
        if not isinstance(found, dict) or not found.get("means"):
            continue
        if found.get("source") == "manual":
            found["weight"] = SUGGEST_WEIGHT["manual"]
            continue
        picks.append((doc, found))
    if not picks:
        return

    def text(doc, ref):
        if not ref:
            return "(none)"
        return doc["paragraphs"][ref["para"]]["sentences"][ref["sent"]][:SENT_PROMPT_LEN]

    listing = "\n".join(
        f'[{i + 1}] document: {doc["name"]}\n    problem sentence: {text(doc, found.get("problem"))}\n'
        f'    means sentence: {text(doc, found["means"])}'
        for i, (doc, found) in enumerate(picks)
    )
    prompt = RANK_PROMPT.format(eid=element["id"], etext=element["text"], phrase=element["features"][index]["phrase"],
                                problem=hints["problem"] or "(not given)", means="; ".join(hints["means"]) or "(not given)",
                                picks=listing)
    try:
        answer = llm.ask_json(prompt, model=model, cache=cache)
    except llm.LLMError as e:  # 걸러 내지 못하면 짚은 대로 두되 모두 약한 근거로 본다
        say(f"  ! 근거 걸러 내기 실패: {e}")
        for _, found in picks:
            found["weight"] = SUGGEST_WEIGHT["indirect"]
        return
    answer = answer if isinstance(answer, dict) else {}
    by_n = {}
    for row in answer.get("picks") or []:
        try:
            by_n[int(row["n"])] = row
        except (KeyError, TypeError, ValueError):
            continue
    try:
        best = int(answer.get("best"))
    except (TypeError, ValueError):
        best = 0
    for i, (doc, found) in enumerate(picks):
        row = by_n.get(i + 1) or {}
        found["reason_ko"] = str(row.get("reason_ko") or "").strip()
        if not row.get("ok"):  # 답이 빠진 근거도 인정하지 않는다
            found["rejected"] = True
            found.pop("weight", None)
            say(f'  {doc["name"]} ({element["id"]}) 제외: {found["reason_ko"] or "판정 없음"}')
            continue
        found["rejected"] = False
        found["strength"] = "direct" if row.get("strength") == "direct" else "indirect"
        found["weight"] = SUGGEST_WEIGHT["best"] if best == i + 1 else SUGGEST_WEIGHT[found["strength"]]
        label = ("가장 강한 근거" if best == i + 1 else "채택") + (", 직접" if found["strength"] == "direct" else ", 간접")
        say(f'  {doc["name"]} ({element["id"]}) {label}: {found["reason_ko"]}')


def keep_manual(out_dir: Path, docs: list, elements: list, judgments: dict) -> None:
    """사람이 직접 넣은 도출 근거(source: manual)는 다시 실행해도 지우지 않는다. 문헌·특징이 같을 때만."""
    try:
        old_docs = json.loads((out_dir / "docs.json").read_text(encoding="utf-8"))
        old_elements = json.loads((out_dir / "elements.json").read_text(encoding="utf-8"))
        old = json.loads((out_dir / "judgments.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    phrases = lambda es: [(e["id"], [f["phrase"] for f in e["features"]]) for e in es]  # noqa: E731
    if phrases(old_elements) != phrases(elements):
        return
    old_files = {d["id"]: d.get("file") for d in old_docs}
    for doc in docs:
        if old_files.get(doc["id"]) != doc["file"]:
            continue
        for eid, features in judgments[doc["id"]].items():
            for i, feature in enumerate(features):
                try:
                    kept = old[doc["id"]][eid][i].get("suggested")
                except (KeyError, IndexError, AttributeError):
                    continue
                if isinstance(kept, dict) and kept.get("source") == "manual" and not feature.get("disclosed"):
                    feature["suggested"] = kept


# --- 인용발명 선택과 양식 (프로그램) ---------------------------------------------
def verdict(features: list) -> str:
    n = sum(1 for f in features if f.get("disclosed"))
    return "same" if n == len(features) and n else "partial" if n else "none"


def doc_score(judgment: dict) -> tuple:
    """(실질적 동일인 구성 수, 개시된 특징 수, 일부라도 대응되는 구성 수). 큰 문헌이 주인용발명 후보다.

    앞의 둘이 같으면, 구성 하나가 통째로 없는 문헌보다 구성마다 일부라도 대응되는 문헌을 앞에 둔다.
    """
    same = sum(1 for f in judgment.values() if verdict(f) == "same")
    feats = sum(1 for fs in judgment.values() for f in fs if f.get("disclosed"))
    touched = sum(1 for f in judgment.values() if verdict(f) != "none")
    return (same, feats, touched)


def _suggestion(feature: dict) -> dict | None:
    """쓸 수 있는 도출 근거: 수단 문장이 있고, 걸러 내는 단계에서 빠지지 않은 것."""
    found = feature.get("suggested")
    if isinstance(found, dict) and found.get("means") and not found.get("rejected"):
        return found
    return None


def compose(elements: list, judgments: dict, doc_ids: list, primary: str = "", extra: list | None = None,
            max_refs: int = MAX_REFS_CLAIM1) -> dict:
    """주인용과 결합 인용을 정한다.

    primary/extra 를 주면 그 순서를 따르고(화면에서 바꾼 경우), 비어 있으면 판정 결과로 고른다.
    돌려주는 gaps: 주인용에 없는 특징과 그것을 채운 인용발명(by, 없으면 None), 채운 방식(how).
    how 는 "disclosed"(그 문헌에 그대로 개시) 또는 "suggested"(그 문헌의 기재로부터 쉽게 도출).
    그대로 개시한 문헌을 먼저 쓰고, 그래도 남은 특징에만 도출 근거를 쓴다.
    """
    ranked = sorted(doc_ids, key=lambda d: doc_score(judgments[d]), reverse=True)
    primary = primary if primary in doc_ids else ranked[0]
    gaps = [
        {"element": e["id"], "feature": i, "by": None, "how": None}
        for e in elements for i, f in enumerate(judgments[primary][e["id"]]) if not f.get("disclosed")
    ]

    def covers(doc_id, gap, how):
        feature = judgments[doc_id][gap["element"]][gap["feature"]]
        if how == "disclosed":
            return bool(feature.get("disclosed"))
        return _suggestion(feature) is not None

    def gain(doc_id, how):
        """(메우는 특징 수, 근거의 강도 합). 같은 수를 메우면 더 직접적인 근거를 가진 문헌이 먼저다."""
        covered = [g for g in gaps if g["by"] is None and covers(doc_id, g, how)]
        weight = 0
        if how == "suggested":
            for g in covered:
                found = _suggestion(judgments[doc_id][g["element"]][g["feature"]])
                weight += SUGGEST_WEIGHT["manual"] if found.get("source") == "manual" else found.get("weight", 1)
        return (len(covered), weight)

    def fill(doc_id, how):
        for gap in gaps:
            if gap["by"] is None and covers(doc_id, gap, how):
                gap["by"], gap["how"] = doc_id, how

    order = [primary]
    if extra is not None:  # 화면에서 고른 순서 그대로
        order += [d for d in extra if d in doc_ids and d != primary][: max_refs - 1]
    for how in ("disclosed", "suggested"):
        for doc_id in order:  # 이미 쓰기로 한 문헌부터(주인용 자신의 도출 근거 포함)
            fill(doc_id, how)
        while extra is None and len(order) < max_refs and any(g["by"] is None for g in gaps):
            # 자동: 남은 차이를 가장 많이 메우는 문헌부터
            best = max((d for d in ranked if d not in order), key=lambda d: gain(d, how), default=None)
            if best is None or not gain(best, how)[0]:
                break
            order.append(best)
            fill(best, how)
    used = [primary] + [d for d in order[1:] if any(g["by"] == d for g in gaps)]
    return {"order": used, "gaps": gaps, "ranking": ranked}


def _josa(word: str, with_final: str, without_final: str) -> str:
    last = next((ch for ch in reversed(word) if "가" <= ch <= "힣"), "")
    if not last:
        return with_final
    return with_final if (ord(last) - 0xAC00) % 28 else without_final


def _excerpt(text: str) -> str:
    return text if len(text) <= EXCERPT_LEN else text[:EXCERPT_LEN] + "…"


def quote(doc: dict, feature: dict) -> str:
    """영문: "번역" (단락 [0019]; "원문 발췌…")   국문: "발췌" (단락 [0019])"""
    para = doc["paragraphs"][feature["para"]]
    source = para["sentences"][feature["sent"]]
    where = locator(doc, para)
    if doc["lang"] == "ko":
        return f'"{source}" ({where})'
    translation = feature.get("translation_ko") or "(번역 없음)"
    return f'"{translation}" ({where}; "{_excerpt(source)}")'


def _basis(doc: dict, source: dict) -> str:
    """도출 근거의 인용: 문제와 수단을 적은 문장. 둘이 같은 문장이면 한 번만 싣는다."""
    problem, means = source.get("problem"), source["means"]
    if problem and (problem.get("para"), problem.get("sent")) != (means.get("para"), means.get("sent")):
        return f'{quote(doc, problem)}는 문제와 {quote(doc, means)}는 해결 수단이 기재되어 있고'
    return f'{quote(doc, means)}는 구성이 기재되어 있고'


def _row_feature(features: list) -> dict | None:
    """구성 한 줄에 실을 발췌: 가장 많은 특징이 가리킨 문장."""
    disclosed = [f for f in features if f.get("disclosed")]
    if not disclosed:
        return None
    count = {}
    for f in disclosed:
        count[(f["para"], f["sent"])] = count.get((f["para"], f["sent"]), 0) + 1
    return max(disclosed, key=lambda f: count[(f["para"], f["sent"])])


def _join_ids(ids: list) -> str:
    """구성 표지 묶기: (F) / (F) 및 (G) / (B), (C) 및 (D)"""
    labels = [f"({i})" for i in ids]
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " 및 " + labels[-1]


def render(claim_no: int, elements: list, docs: list, judgments: dict, plan: dict) -> str:
    by_id = {d["id"]: d for d in docs}
    order = plan["order"]
    number = {doc_id: i + 1 for i, doc_id in enumerate(order)}
    primary = order[0]
    lines = [f"[인용발명{CIRCLED[0]}]", "[구성요소]"]
    groups = []  # 이웃한 구성이 판정도 같고 같은 문장을 가리키면 "(F) 및 (G)"처럼 한 번만 적는다
    for e in elements:
        features = judgments[primary][e["id"]]
        row = _row_feature(features)
        key = (verdict(features), (row["para"], row["sent"]) if row else None)
        if groups and groups[-1]["key"] == key:
            groups[-1]["ids"].append(e["id"])
        else:
            groups.append({"key": key, "ids": [e["id"]], "row": row})
    for group in groups:
        lines.append(f'{_join_ids(group["ids"])} {VERDICT_KO[group["key"][0]]}')
        lines.append(quote(by_id[primary], group["row"]) if group["row"] else "대응되는 기재를 찾지 못했습니다")
        lines.append("")
    lines.append("[차이점]")
    open_gaps = []
    for gap in plan["gaps"]:
        element = next(e for e in elements if e["id"] == gap["element"])
        phrase = element["features"][gap["feature"]]["phrase"]
        if gap["by"] is None:
            open_gaps.append(gap)
            lines.append(f'구성 ({gap["element"]})에 대해 인용발명 1에는 {phrase}{_josa(phrase, "이", "가")} 명시되어 있지 않고, '
                         f"제출된 다른 인용발명에서도 대응되는 기재를 찾지 못했습니다")
            continue
        n = number[gap["by"]]
        feature = judgments[gap["by"]][gap["element"]][gap["feature"]]
        source = feature["suggested"] if gap.get("how") == "suggested" else feature
        why = re.sub(r"^인용발명\s*\d*\s*의", f"인용발명 {n}의", source.get("why_ko") or "").strip()
        why = f"{why} " if why else ""
        if gap.get("how") == "suggested":  # 그대로 적혀 있지는 않지만 문제·수단의 기재로부터 쉽게 도출
            doc = by_id[gap["by"]]
            found = _basis(doc, source)
            where = "" if n == 1 else f"인용발명 {n}에는 "
            apply = "이로부터" if n == 1 else "인용발명 1에 이를 적용하여"
            lines.append(
                f'구성 ({gap["element"]})에 대해 인용발명 1에는 {phrase}{_josa(phrase, "이", "가")} 명시되어 있지 않으나, '
                f'{where}{found} {why}통상의 기술자가 {apply} 청구항의 "{phrase}"{_josa(phrase, "을", "를")} 쉽게 도출할 수 있습니다'
            )
            continue
        lines.append(
            f'구성 ({gap["element"]})에 대해 인용발명 1에는 {phrase}{_josa(phrase, "이", "가")} 명시되어 있지 않으나, '
            f'인용발명 {n}에는 {quote(by_id[gap["by"]], feature)}는 구성이 기재되어 있고 {why}청구항의 "{phrase}"에 대응됩니다'
        )
    if not plan["gaps"]:
        lines.append("없음")
    lines.append("")
    if open_gaps:
        lines.append(f"청구항 {claim_no} 발명의 일부 구성은 제출된 인용발명들에서 대응되는 기재를 찾지 못했으므로, 결론은 작성하지 않았습니다")
    else:
        lines.append(conclusion(claim_no, list(range(1, len(order) + 1)), same=not plan["gaps"]))
    return "\n".join(lines)


def _num_josa(n: int, with_final: str, without_final: str) -> str:
    """숫자를 읽은 소리(1 → 일, 2 → 이, 10 → 십)에 맞춰 조사를 고른다."""
    return _josa("십일이삼사오육칠팔구"[n % 10], with_final, without_final)


def conclusion(claim_no: int, nums: list, same: bool) -> str:
    """결론 한 줄. nums: 이 청구항에 쓴 인용발명 번호들, same: 차이점이 전혀 없는 경우."""
    first = nums[0]
    if same:
        return f'따라서 청구항 {claim_no} 발명은 인용발명 {first}{_num_josa(first, "과", "와")} 실질적으로 동일합니다'
    if len(nums) == 1:  # 차이는 있으나 그 인용발명 자신의 기재로부터 도출
        return (f'따라서 청구항 {claim_no} 발명은 통상의 기술자가 인용발명 {first}{"으로" if first % 10 in (0, 3, 6) else "로"}부터 '
                f"쉽게 발명할 수 있습니다")
    texts = [str(n) for n in nums]
    joined = " 및 ".join(texts) if len(texts) == 2 else ", ".join(texts[:-1]) + " 및 " + texts[-1]
    return (f'따라서 청구항 {claim_no} 발명은 통상의 기술자가 인용발명 {joined}{_num_josa(nums[-1], "을", "를")} '
            f"결합하여 쉽게 발명할 수 있습니다")


def matrix(elements: list, docs: list, judgments: dict) -> list:
    """화면 표시용: 문헌별·구성별 판정."""
    return [
        {"id": d["id"], "name": d["name"], "score": list(doc_score(judgments[d["id"]])),
         "verdicts": {e["id"]: verdict(judgments[d["id"]][e["id"]]) for e in elements},
         # 그대로 개시되지는 않았지만 도출 근거가 있는 특징 수
         "suggested": {e["id"]: sum(1 for f in judgments[d["id"]][e["id"]]
                                    if not f.get("disclosed") and _suggestion(f)) for e in elements}}
        for d in docs
    ]


# --- 종속항 ---------------------------------------------------------------------
# 프로그램: 항 나누기와 인용 관계(claims.py), 인용발명 배정과 번호, 양식.
# LLM: 종속항이 더한 구성 뽑기와 "상기 …" 풀어 쓰기(분석 모델), 문헌별 개시 판정·번역·대응 이유(스크리닝 모델).
DEP_FILES = ("dependents.json", "dep_judgments.json", "dep_hints.json")


def _claim_no(claim: str) -> int:
    m = re.search(r"청구항\s*(\d+)", claim)
    return int(m.group(1)) if m else 1


def _clean_phrase(phrase: str) -> str:
    return re.sub(r"상기\s*", "", " ".join(phrase.split())).strip(" \"'“”.,;")


def feature_ref(phrase: str) -> str:
    """양식에서 구성을 가리키는 말: "…를 포함"하는 구성 / "…는 제외"되는 구성."""
    p = re.sub(r"(하는|되는)\s*(?:구성|것|단계)$", r"\1", phrase.strip().rstrip(" .,;"))
    for ending, tail in (("하는", "하는"), ("하고", "하는"), ("하며", "하는"), ("한다", "하는"), ("합니다", "하는"),
                         ("되는", "되는"), ("되고", "되는"), ("되며", "되는"), ("된다", "되는"), ("됩니다", "되는"),
                         ("됨", "되는")):
        if p.endswith(ending) and len(p) > len(ending):
            return f'"{p[:-len(ending)].rstrip()}"{tail} 구성'
    if p.endswith(("는", "된")):  # "…가 없는", "…를 갖는", "…와 연결된"
        return f'"{p}" 구성'
    # "마커는 광학 추적 장치인" 처럼 "…이다"로 맺는 구성. "…를 확인"처럼 하다 동사의 어간이 "인"으로 끝나는 말은 아니다
    if p.endswith("인") and not p.endswith(("확인", "승인", "할인", "날인", "견인", "유인", "시인", "부인", "오인", "공인",
                                           "로그인", "디자인", "사인", "체크인")):
        return f'"{p}" 구성'
    return f'"{p}"하는 구성'


def dependent_contexts(claim: str, base_no: int | None, deps: list) -> dict:
    """종속항 번호 → 그 항이 거슬러 인용하는 모든 항의 본문. LLM 이 "상기 …"를 해석할 때 쓴다.

    base_no 가 None 이면 독립항 없이 종속항만 대비하는 경우다(claim 은 쓰지 않는다).
    """
    texts = {} if base_no is None else {base_no: claims.strip_mark(claim)}
    texts.update({d["no"]: d["text"] for d in deps})

    def line(dep, n):
        # 대비하지는 않았지만 글이 들어 있는 항(다른 독립항 등)은 그 글을, 글도 없는 항은 없다고 알린다
        text = texts.get(n) or (dep.get("context_texts") or {}).get(str(n))
        if not text:
            return f"청구항 {n}: (text not available - read any term this claim would introduce by its plain meaning)"
        return f'청구항 {n}: {" ".join(text.split())}'

    return {d["no"]: "\n".join(line(d, n) for n in d["ancestors"]) for d in deps}


def dependent_features(dep: dict, context: str, model: str, cache, langs=()) -> list:
    """종속항이 더한 구성 [{phrase, meaning, terms, terms_ko}]."""
    prompt = DEP_FEATURES_PROMPT.format(context=context, no=dep["no"], refs=claims.ref_label(dep["refs"]),
                                        text=dep["text"], limit=MAX_DEP_FEATURES, **_more_terms(langs))
    answer = llm.ask_json(prompt, model=model, cache=cache)
    rows = answer.get("features") if isinstance(answer, dict) else answer
    feats = []
    for f in (rows or [])[:MAX_DEP_FEATURES]:
        if not isinstance(f, dict):
            continue
        phrase = _clean_phrase(str(f.get("phrase") or ""))
        if phrase:
            feats.append({"phrase": phrase, "meaning": " ".join(str(f.get("meaning") or "").split()), **_terms(f, langs)})
    return feats


def split_dependents(deps: list, contexts: dict, model: str, cache, say=print, workers: int = 4, langs=()) -> None:
    """종속항마다 dep["features"] 를 채운다."""
    def run(dep):
        try:
            return dependent_features(dep, contexts[dep["no"]], model, cache, langs=langs)
        except llm.LLMError as e:
            say(f'  ! 청구항 {dep["no"]} 구성 나누기 실패: {e}')
            return []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for dep, feats in zip(deps, pool.map(run, deps)):
            # LLM 이 답하지 못하면 종속항 본문 전체를 하나의 구성으로 본다
            dep["features"] = feats or [{"phrase": _clean_phrase(dep["body"]), "meaning": "", "terms": [], "terms_ko": []}]
            for i, f in enumerate(dep["features"]):
                say(f'  ({dep["no"]}-{i + 1}) {f["phrase"]}' + (f'  ← {f["meaning"]}' if f["meaning"] else ""))


def judge_dependent(doc: dict, dep: dict, context: str, model: str, cache, lacking: list | None = None) -> list:
    """lacking: 인용하는 청구항을 대비할 때 이 문헌에 없다고 판정된 특징들. 종속항 판정이 그것과 어긋나지 않게 알려 준다."""
    cands = candidate_sentences(doc, dep)
    if not cands:
        return [{"disclosed": False} for _ in dep["features"]]
    known = ""
    if lacking:
        known = ("\nALREADY ESTABLISHED when the claims above were compared with this document - it does NOT disclose:\n"
                 + "\n".join(f"- {phrase}" for phrase in lacking)
                 + "\nStay consistent with that: when a feature of the dependent claim repeats the very limitation listed "
                   "here (the same input, the same condition), this document does not disclose that feature either, unless a "
                   "sentence below states it outright. Judge every other feature on its own; a part that the document calls "
                   "by another name still counts.\n")
    prompt = DEP_JUDGE_PROMPT.format(
        context=context, no=dep["no"], text=dep["text"],
        features="\n".join(f'{i + 1}. {f["phrase"]}' + (f'\n   meaning: {f["meaning"]}' if f.get("meaning") else "")
                           for i, f in enumerate(dep["features"])),
        known=known,
        sentences="\n".join(f'[{i + 1}] {c["text"][:SENT_PROMPT_LEN]}' for i, c in enumerate(cands)),
        translation_rule=_translation_rule(doc),
    )
    return _read_judgment(llm.ask_json(prompt, model=model, cache=cache), cands, len(dep["features"]), strict=True)


def lacking_features(elements: list, judgments: dict) -> dict:
    """문헌마다, 청구항 1 대비에서 그 문헌에 없다고 판정된 특징 이름들. {doc_id: [phrase]}"""
    out = {}
    for doc_id, by_element in (judgments or {}).items():
        out[doc_id] = [f["phrase"] for e in elements
                       for f, j in zip(e["features"], by_element.get(e["id"]) or []) if not j.get("disclosed")]
    return out


def judge_dependents(docs: list, deps: list, contexts: dict, model: str, cache, say=print, workers: int = 4,
                     lacking: dict | None = None) -> dict:
    """dep_judgments[doc_id][종속항 id] = 구성별 판정 목록"""
    tasks = [(d, dep) for dep in deps for d in docs]

    def run(task):
        doc, dep = task
        try:
            return judge_dependent(doc, dep, contexts[dep["no"]], model, cache, lacking=(lacking or {}).get(doc["id"]))
        except llm.LLMError as e:
            say(f'  ! {doc["name"]} (청구항 {dep["no"]}) 판정 실패: {e}')
            return [{"disclosed": False, "error": str(e)[:200]} for _ in dep["features"]]

    judgments = {d["id"]: {} for d in docs}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (doc, dep), result in zip(tasks, pool.map(run, tasks)):
            judgments[doc["id"]][dep["id"]] = result
            found = [f'{dep["no"]}-{i + 1}' for i, f in enumerate(result) if f.get("disclosed")]
            say(f'  {doc["name"]} (청구항 {dep["no"]}) ' + (", ".join(found) + " 개시" if found else "대응 없음"))
    return judgments


def assign_dependent(judged: dict, base: set, numbering: list, ranking: list,
                     max_new: int = MAX_NEW_REFS_PER_DEPENDENT) -> tuple:
    """종속항의 구성마다 근거로 쓸 인용발명을 정한다.

    judged: {doc_id: 이 종속항의 구성별 판정}, base: 인용하는 항들에 이미 쓴 인용발명,
    numbering: 지금까지 번호가 붙은 인용발명(번호순), ranking: 청구항 1 대응이 많은 순.
    인용하는 항의 인용발명에서 먼저 찾고, 남은 구성은 새 인용발명 max_new 건까지로 메운다.
    그대로 개시한 문헌을 먼저 쓰고, 그래도 남은 구성에만 도출 근거를 쓴다.
    한 구성을 여러 인용발명이 개시하면 이 항의 구성을 가장 많이 개시한 인용발명을 쓰고, 같으면 번호가 앞선 것을 쓴다
    (한 항의 구성들은 한 절차의 단계인 경우가 많아, 한 문헌에서 이어서 대응시키는 쪽이 앞뒤가 맞는다).
    돌려주는 값: ([구성마다 {doc, how} 또는 None], [새로 더한 인용발명])
    """
    count = len(next(iter(judged.values())))
    by = [None] * count
    allowed = [d for d in numbering if d in base]
    # 새로 더할 후보: 다른 항에서 이미 번호가 붙은 문헌이 먼저, 그다음 청구항 1 대응이 많은 순
    others = [d for d in numbering if d not in base] + [d for d in ranking if d not in numbering]
    new = []

    def covers(doc_id, i, how):
        feature = judged[doc_id][i]
        return bool(feature.get("disclosed")) if how == "disclosed" else _suggestion(feature) is not None

    def weight(doc_id, i):
        found = _suggestion(judged[doc_id][i])
        return SUGGEST_WEIGHT["manual"] if found.get("source") == "manual" else found.get("weight", 1)

    def gain(doc_id, how):
        open_ = [i for i in range(count) if by[i] is None and covers(doc_id, i, how)]
        return (len(open_), sum(weight(doc_id, i) for i in open_) if how == "suggested" else 0)

    def disclosed_count(doc_id):
        return sum(1 for i in range(count) if covers(doc_id, i, "disclosed"))

    for how in ("disclosed", "suggested"):
        for i in range(count):
            hits = [d for d in allowed if by[i] is None and covers(d, i, how)]
            if hits:  # 개시는 이 항의 구성을 가장 많이 개시한 인용발명(같으면 번호가 앞선 것), 도출 근거는 가장 강한 근거
                best = (max(hits, key=lambda d: (disclosed_count(d), -allowed.index(d))) if how == "disclosed"
                        else max(hits, key=lambda d: weight(d, i)))
                by[i] = {"doc": best, "how": how}
        while len(new) < max_new and any(b is None for b in by):
            best = max((d for d in others if d not in allowed), key=lambda d: gain(d, how), default=None)
            if best is None or not gain(best, how)[0]:
                break
            for i in range(count):
                if by[i] is None and covers(best, i, how):
                    by[i] = {"doc": best, "how": how}
            new.append(best)
            allowed.append(best)
    return by, new


def plan_dependents(base_no: int, plan: dict, deps: list, dep_judgments: dict) -> dict:
    """청구항 1 의 인용발명에서 출발해 종속항을 번호순으로 배정한다. 인용발명 번호는 전체에서 하나로 이어진다.

    항마다 docs(그 항까지 쓴 인용발명), open(결론을 쓸 수 없음), same(인용발명 1 과 차이 없음)을 따진다.
    여러 항을 택일로 인용하면("제1항 내지 제3항 중 어느 한 항") 인용하는 항들의 인용발명을 합쳐서 본다.
    base_no 가 None 이면(독립항 없이 종속항만) 출발점의 인용발명이 없고, 모든 항이 detached 다.
    """
    numbering = list(plan["order"])
    state = {base_no: {"docs": set(plan["order"]), "open": any(g["by"] is None for g in plan["gaps"]),
                       "same": not plan["gaps"], "detached": base_no is None}}
    items = []
    for dep in deps:
        parents = [state[r] for r in dep["refs"] if r in state]
        # detached: 인용하는 항의 대비 기록이 없다(사람이 따로 대비한 항 등). 그 항에 어떤 인용발명을 썼는지 모르므로
        # 청구항 1 의 인용발명에서 출발해 이 항이 더한 구성만 배정하고, 결론은 쓰지 않는다.
        detached = all(p["detached"] for p in parents)  # 인용하는 항이 하나도 기록에 없으면 True
        base = set().union(*(p["docs"] for p in parents)) if parents else set(state[base_no]["docs"])
        judged = {d: dep_judgments[d][dep["id"]] for d in plan["ranking"]}
        # 이어 쓸 인용발명이 하나도 없는 항(종속항만 대비할 때)은 독립항처럼 여러 건을 쓸 수 있게 한다
        by, new = assign_dependent(judged, base, numbering, plan["ranking"],
                                   max_new=MAX_NEW_REFS_PER_DEPENDENT if base else MAX_REFS_CLAIM1)
        numbering += [d for d in new if d not in numbering]
        missing = any(b is None for b in by)
        used = base | {b["doc"] for b in by if b}
        state[dep["no"]] = {
            "docs": used,
            "open": missing or (bool(parents) and all(p["open"] for p in parents)),
            "same": (not missing and not detached and any(p["same"] for p in parents)
                     and all(b["how"] == "disclosed" and b["doc"] == numbering[0] for b in by)),
            "detached": detached,
        }
        items.append({"dep": dep, "by": by, "new": new, "missing": missing, **state[dep["no"]]})
    return {"numbering": numbering, "claims": items}


def suggest_dependents(claim: str, base_no: int, docs: list, deps: list, dep_judgments: dict, plan: dict, model: str,
                       hint_model: str, cache, say=print, skip=()) -> dict:
    """어느 인용발명으로도 메우지 못한 종속항 구성에 대해 도출 근거(문제·수단을 적은 문장)를 찾는다.

    skip: 저장된 판정을 그대로 쓰는 항 번호들. 이미 찾아 본 항이라 다시 찾지 않는다.
    """
    contexts = dependent_contexts(claim, base_no, deps)
    hints = {}
    for item in plan_dependents(base_no, plan, deps, dep_judgments)["claims"]:
        dep = item["dep"]
        gaps = [{"element": dep["id"], "feature": i} for i, b in enumerate(item["by"]) if b is None]
        if not gaps or dep["no"] in skip:
            continue
        # 가벼운 모델이 "상기 …"를 몰라도 되도록, 구성 이름 대신 풀어 쓴 뜻을 보여 준다
        probe = {"id": dep["id"], "text": dep["text"],
                 "features": [{**f, "phrase": f.get("meaning") or f["phrase"]} for f in dep["features"]]}
        whole = f'{contexts[dep["no"]]}\n청구항 {dep["no"]}: {dep["text"]}'
        hints.update(suggest_gaps(whole, docs, [probe], dep_judgments, gaps, model, hint_model, cache, say=say))
        for gap in gaps:  # 대응 이유가 스스로 "…까지는 나타나지 않는다"고 적은 근거는 쓰지 않는다
            for doc in docs:
                found = dep_judgments[doc["id"]][dep["id"]][gap["feature"]].get("suggested")
                if (isinstance(found, dict) and found.get("source") != "manual" and not found.get("rejected")
                        and _HEDGE.search(found.get("why_ko") or "")):
                    found["rejected"] = True
                    found.pop("weight", None)
                    found["reason_ko"] = "대응 이유가 스스로 단서를 달아(그 구성이 나타나지 않는다고 적음) 근거로 쓰지 않습니다"
                    say(f'  {doc["name"]} ({dep["id"]}) 제외: {found["reason_ko"]}')
    return hints


def _why(text: str, number: int) -> str:
    """LLM 이 쓴 대응 이유("인용발명은 …므로")를 양식에 끼울 수 있게 다듬는다.

    번호를 넣고 조사를 번호 소리에 맞춘다(인용발명 2는, 인용발명 3은). "므로" 뒤에 덧붙은 말은 잘라 내고,
    "므로"가 아예 없으면 문장이 이어지지 않으므로 쓰지 않는다.
    """
    text = re.sub(r"^(인용발명\s*\d*\s*)의의(?=\s)", r"\1의", " ".join((text or "").split()))  # LLM 이 가끔 "인용발명의의"로 쓴다
    cut = text.rfind("므로")
    if cut < 0:
        return ""
    text = text[:cut + 2]

    def head(m):
        particle = {"은": ("은", "는"), "는": ("은", "는"), "이": ("이", "가"), "가": ("이", "가")}.get(m.group(1))
        return f"인용발명 {number}" + (_num_josa(number, *particle) if particle else m.group(1))

    return re.sub(r"^인용발명\s*\d*\s*(은|는|이|가|의|에)", head, text) + " "


def render_dependent(item: dict, docs: list, dep_judgments: dict, numbering: list) -> str:
    """종속항 하나의 양식.

    청구항 2
    제1항에 있어서 청구항 2의 구성에 대해 인용발명 1에는 "번역" (단락 [0022]; "원문…")는 구성이 기재되어 있고
    인용발명 1은 …므로 청구항의 "…를 포함"하는 구성에 대응되고,

    인용발명 2에는 "…" (단락 [0031])는 구성이 기재되어 있고 인용발명 2는 …므로 청구항의 "…"하는 구성에 대응됩니다

    따라서 청구항 2 발명은 …

    구성마다 한 문단이고 문단 사이는 한 줄 띈다. "청구항 2의 구성에 대해"는 첫 문단에만 쓴다.
    그 항의 마지막 구성만 "…니다"로 맺고, 앞의 구성들은 "…고," "…며,"를 번갈아 쓴다.
    """
    by_id = {d["id"]: d for d in docs}
    dep, no = item["dep"], item["dep"]["no"]
    lines = [f"청구항 {no}"]
    last = len(dep["features"]) - 1
    for i, (feature, pick) in enumerate(zip(dep["features"], item["by"])):
        lead = f'{dep["preamble"]} ' if dep["preamble"] else ""
        head = f"{lead}청구항 {no}의 구성에 대해 " if i == 0 else ""

        def end(final: str, a: str, b: str) -> str:  # 맺음말: 마지막 구성 / 앞의 구성(번갈아)
            return final if i == last else (a if i % 2 == 0 else b)

        if i:
            lines.append("")
        ref = feature_ref(feature["phrase"])
        if pick is None:
            lines.append(f"{head}제출된 인용발명들에서 청구항의 {ref}에 대응되는 기재를 "
                         + end("찾지 못했습니다", "찾지 못했고,", "찾지 못했으며,"))
            continue
        n = numbering.index(pick["doc"]) + 1
        doc = by_id[pick["doc"]]
        judged = dep_judgments[pick["doc"]][dep["id"]][i]
        if pick["how"] == "suggested":  # 그대로 적혀 있지는 않지만 문제·수단의 기재로부터 쉽게 도출
            source = judged["suggested"]
            found = _basis(doc, source)
            lines.append(f"{head}인용발명들에는 청구항의 {ref}이 명시되어 있지 않으나, 인용발명 {n}에는 {found} "
                         f'{_why(source.get("why_ko"), n)}통상의 기술자가 이로부터 청구항의 {ref}을 쉽게 도출할 수 '
                         + end("있습니다", "있고,", "있으며,"))
            continue
        lines.append(f"{head}인용발명 {n}에는 {quote(doc, judged)}는 구성이 기재되어 있고 "
                     f'{_why(judged.get("why_ko"), n)}청구항의 {ref}에 ' + end("대응됩니다", "대응되고,", "대응되며,"))
    lines.append("")
    if item["missing"]:
        lines.append(f"청구항 {no} 발명의 일부 구성은 제출된 인용발명들에서 대응되는 기재를 찾지 못했으므로, 결론은 작성하지 않았습니다")
    elif item["detached"]:
        which = claims.ref_label(dep.get("unrecorded") or dep["refs"])
        lines.append(f"청구항 {no} 발명이 인용하는 {which}의 구성대비 기록이 없으므로, 결론은 작성하지 않았습니다")
    elif item["open"]:
        lines.append(f"청구항 {no} 발명이 인용하는 청구항의 일부 구성에 대응되는 기재를 찾지 못했으므로, 결론은 작성하지 않았습니다")
    else:
        lines.append(conclusion(no, sorted(numbering.index(d) + 1 for d in item["docs"]), same=item["same"]))
    return "\n".join(lines)


def build_dependents(base_no: int, plan: dict, dependents: dict, docs: list, dep_judgments: dict) -> dict:
    """저장된 종속항 판정으로 배정과 양식을 만든다. LLM 을 부르지 않는다."""
    deps = dependents.get("claims") or []
    notes = list(dependents.get("notes") or [])
    if any(dep["id"] not in dep_judgments.get(d["id"], {}) for d in docs for dep in deps):
        notes.append("인용발명이 바뀌어 종속항 판정이 맞지 않습니다. 종속항 구성대비를 다시 실행하세요")
        return {"dependents": [], "dep_text": "", "dep_notes": notes, "numbering": list(plan["order"])}
    planned = plan_dependents(base_no, plan, deps, dep_judgments)
    numbering = planned["numbering"]
    out = []
    for item in planned["claims"]:
        dep = item["dep"]
        out.append({
            "claim_no": dep["no"], "refs": dep["refs"], "ref_label": claims.ref_label(dep["refs"]),
            "features": [
                {"label": f'{dep["no"]}-{i + 1}', "phrase": f["phrase"], "meaning": f.get("meaning", ""),
                 "number": numbering.index(pick["doc"]) + 1 if pick else None, "how": pick["how"] if pick else None}
                for i, (f, pick) in enumerate(zip(dep["features"], item["by"]))
            ],
            "new": [numbering.index(d) + 1 for d in item["new"]],
            "numbers": sorted(numbering.index(d) + 1 for d in item["docs"]),
            "open": item["open"], "detached": item["detached"],
            "text": render_dependent(item, docs, dep_judgments, numbering),
        })
    return {"dependents": out, "dep_text": "\n\n".join(d["text"] for d in out), "dep_notes": notes, "numbering": numbering}


def load_dependents(out_dir: Path) -> tuple:
    """저장된 (종속항 목록, 종속항 판정). 없으면 (None, None)."""
    try:
        return (json.loads((out_dir / "dependents.json").read_text(encoding="utf-8")),
                json.loads((out_dir / "dep_judgments.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None, None


def kept_dependents(out_dir: Path, docs: list, deps: list, contexts: dict, claim: str, base_no: int | None) -> tuple:
    """종속항을 나눠서 더 넣을 때: 이미 대비해 둔 항 가운데 글과 맥락이 그대로인 항은 저장된 판정을 그대로 쓴다.

    그런 항의 dep["features"] 를 저장된 구성으로 채우고 (그 항 번호들, 저장된 판정, 저장된 도출 근거 단서)를 돌려준다.
    문헌이 바뀌었거나 저장된 판정이 없으면 아무 항도 고르지 않는다(모두 새로 판정).
    """
    stored, judged = load_dependents(out_dir)
    if not stored or not judged or any(d["id"] not in judged for d in docs):
        return set(), {}, {}
    try:
        hints = json.loads((out_dir / "dep_hints.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        hints = {}
    old = {d["no"]: d for d in stored.get("claims") or []}
    old_contexts = dependent_contexts(claim, base_no, list(old.values()))
    kept = set()
    for dep in deps:
        prev = old.get(dep["no"])
        if (prev and prev.get("features") and prev["text"] == dep["text"] and old_contexts.get(dep["no"]) == contexts[dep["no"]]
                and all(len(judged[d["id"]].get(dep["id"]) or []) == len(prev["features"]) for d in docs)):
            dep["features"] = prev["features"]
            kept.add(dep["no"])
    return kept, judged, hints


def _judge_fresh(out_dir: Path, claim: str, base_no: int | None, docs: list, deps: list, contexts: dict, plan_of, model: str,
                 hint_model: str, cache, say, keep: bool, lacking: dict | None, steps: tuple) -> tuple:
    """종속항의 구성 나누기 → 판정 → 도출 근거 찾기. (dep_judgments, hints)

    keep: 이미 대비해 둔 항 가운데 글이 그대로인 항은 저장된 판정을 쓰고 새로 넣었거나 고친 항만 LLM 에 보낸다.
    plan_of: dep_judgments 를 받아 출발점(청구항 1 의 인용발명)을 돌려주는 함수. steps: 단계 이름 셋.
    """
    kept, old_judged, old_hints = kept_dependents(out_dir, docs, deps, contexts, claim, base_no) if keep else (set(), {}, {})
    fresh = [d for d in deps if d["no"] not in kept]
    if kept:
        say("  이미 대비한 청구항 " + ", ".join(str(d["no"]) for d in deps if d["no"] in kept)
            + " 은 저장된 판정을 그대로 씁니다" + ("" if fresh else ". 새로 대비할 항이 없습니다"))
    say(steps[0])
    split_dependents(fresh, contexts, hint_model, cache, say=say, langs=extra_langs(docs))
    warn_no_terms(docs, fresh, say)
    say(steps[1])
    judged = judge_dependents(docs, fresh, contexts, model, cache, say=say, lacking=lacking)
    dep_judgments = {d["id"]: {dep["id"]: (old_judged if dep["no"] in kept else judged)[d["id"]][dep["id"]] for dep in deps}
                     for d in docs}
    say(steps[2])
    hints = {k: v for k, v in old_hints.items() if any(k.split(".")[0] == d["id"] for d in deps if d["no"] in kept)}
    found = suggest_dependents(claim, base_no, docs, deps, dep_judgments, plan_of(dep_judgments), model, hint_model, cache,
                               say=say, skip=kept)
    if not found:
        say("  해당 없음")
    hints.update(found)
    return dep_judgments, hints


def alone_plan(docs: list, dep_judgments: dict) -> dict:
    """독립항 없이 종속항만 대비할 때의 출발점. 청구항 1 의 인용발명이 없고, 문헌은 종속항 구성을 많이 개시한 순이다."""
    def score(doc_id):
        return sum(1 for feats in (dep_judgments.get(doc_id) or {}).values() for f in feats if f.get("disclosed"))

    return {"order": [], "gaps": [], "ranking": sorted((d["id"] for d in docs), key=score, reverse=True)}


def build_alone(dependents: dict, docs: list, dep_judgments: dict) -> dict:
    """종속항만 대비한 결과. 청구항 1 쪽 항목은 비워 둔다. LLM 을 부르지 않는다."""
    unify_translations(dep_judgments)
    plan = alone_plan(docs, dep_judgments or {})
    result = {"alone": True, "claim_no": None, "order": [], "ranking": plan["ranking"], "gaps": [], "matrix": [], "text": ""}
    result.update(build_dependents(None, plan, dependents, docs, dep_judgments or {}))
    result["legend"] = legend(result["numbering"], docs)
    return result


def unify_translations(*judgment_sets) -> None:
    """같은 문장을 여러 구성이 가리키면 번역을 하나로 맞춘다(먼저 나온 번역).

    번역은 판정할 때마다 LLM 이 따로 하므로, 그대로 두면 같은 문장이 항마다 다른 말("내부 보정"/"내재 보정")로 실린다.
    """
    first = {}

    def refs():
        for judged in judgment_sets:
            for doc_id, by in (judged or {}).items():
                for features in by.values():
                    for feature in features:
                        found = feature.get("suggested") if isinstance(feature.get("suggested"), dict) else {}
                        for ref in (feature, found.get("problem"), found.get("means")):
                            if isinstance(ref, dict) and "para" in ref and "sent" in ref:
                                yield (doc_id, ref["para"], ref["sent"]), ref

    for key, ref in refs():
        if ref.get("translation_ko"):
            first.setdefault(key, ref["translation_ko"])
    for key, ref in refs():
        if key in first:
            ref["translation_ko"] = first[key]


def build_result(claim: str, elements: list, docs: list, judgments: dict, primary: str = "", extra=None,
                 dependents: dict | None = None, dep_judgments: dict | None = None) -> dict:
    claim_no = _claim_no(claim)
    unify_translations(judgments, dep_judgments)
    plan = compose(elements, judgments, [d["id"] for d in docs], primary=primary, extra=extra)
    result = {
        "claim_no": claim_no, "order": plan["order"], "ranking": plan["ranking"], "gaps": plan["gaps"],
        "matrix": matrix(elements, docs, judgments),
        "text": render(claim_no, elements, docs, judgments, plan),
    }
    if dependents and dependents.get("claims"):
        result.update(build_dependents(claim_no, plan, dependents, docs, dep_judgments or {}))
    result["legend"] = legend(result.get("numbering") or result["order"], docs)
    return result


# --- 실행 --------------------------------------------------------------------
def read_docs(pdfs: list, say=print) -> list:
    docs = []
    for doc_id, name, path in pdfs:
        doc = load_document(Path(path), doc_id=doc_id, name=name)
        unit = "단락" if doc["kind"] == "patent" else "묶음"
        say(f'  {doc["name"]}: {doc["pages"]}쪽, {len(doc["paragraphs"])}개 {unit}, {LANG_KO.get(doc["lang"], doc["lang"])}')
        docs.append(doc)
    return docs


def run_alone(text: str, pdfs: list, out_dir: Path, model: str, cache, say=print, split_model: str | None = None,
              reuse_docs: bool = False, keep: bool = False) -> dict:
    """독립항 없이 종속항만 대비한다.

    인용하는 항의 글도 대비 기록도 없으므로, 항마다 더한 구성만 뽑아 인용발명 어디에 있는지 찾는다. 결론은 쓰지 않는다.
    reuse_docs: 문헌은 그대로 두고 종속항만 바꿔 다시 돌릴 때, PDF 를 다시 읽지 않고 저장해 둔 문헌을 쓴다.
    keep: 종속항을 나눠서 더 넣을 때. 이미 대비한 항은 저장된 판정을 그대로 쓰고 새 항만 판정한다(reuse_docs 일 때만).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    hint_model = model if split_model is None else split_model
    say("[1/5] 인용발명 PDF 읽기")
    docs = None
    if reuse_docs:
        try:
            docs = json.loads((out_dir / "docs.json").read_text(encoding="utf-8"))
            say(f"  저장해 둔 문헌 {len(docs)}건을 그대로 씁니다")
        except (OSError, ValueError):
            docs = None
    if docs is None:
        docs = read_docs(pdfs, say)
    if not docs:
        raise CompareError("인용발명 PDF 가 없습니다")
    say("[2/5] 종속항 나누기와 인용 관계 읽기")
    try:
        deps, notes = claims.read_dependents(text, None)
    except claims.ClaimError as e:
        raise CompareError(str(e)) from None
    for dep in deps:
        say(f'  청구항 {dep["no"]} ← {claims.ref_label(dep["refs"])}')
    for note in notes:
        say(f"  ! {note}")
    if not deps:
        raise CompareError("대비할 종속항이 없습니다. " + " / ".join(notes))
    contexts = dependent_contexts("", None, deps)
    dep_judgments, hints = _judge_fresh(
        out_dir, "", None, docs, deps, contexts, lambda judged: alone_plan(docs, judged), model, hint_model, cache, say,
        keep and reuse_docs, None,
        ("[3/5] 종속항이 더한 구성 나누기", "[4/5] 구성별 판정 (그대로 개시되어 있는가)", "[5/5] 메우지 못한 구성의 도출 근거 찾기"))
    dependents = {"alone": True, "base_no": None, "claims": deps, "notes": [claims.ALONE_NOTE] + notes}
    # 청구항 1 쪽 파일은 빈 값으로 둔다. 화면과 양식 다시 만들기가 같은 파일들을 읽는다.
    for name, value in (("docs.json", docs), ("elements.json", []), ("judgments.json", {d["id"]: {} for d in docs}),
                        ("hints.json", {}), ("dependents.json", dependents), ("dep_judgments.json", dep_judgments),
                        ("dep_hints.json", hints)):
        (out_dir / name).write_text(json.dumps(value, ensure_ascii=False, indent=None if name == "docs.json" else 2),
                                    encoding="utf-8")
    (out_dir / "dependents.txt").write_text(text.strip() + "\n", encoding="utf-8")
    result = build_alone(dependents, docs, dep_judgments)
    names = {d["id"]: d["name"] for d in docs}
    for i, doc_id in enumerate(result["numbering"]):
        say(f"  인용발명 {i + 1}: {names[doc_id]}")
    save_result(out_dir, result)
    return result


def run_compare(claim: str, pdfs: list, out_dir: Path, model: str, cache, say=print, split_model: str | None = None,
                dependents: str = "") -> dict:
    """pdfs: [(doc_id, 표시 이름 또는 "", 경로)]

    model: 구성별 판정에 쓰는 가벼운 모델(호출 수 = 문헌 수 × 구성 수).
    split_model: 청구항을 특징으로 나누는 한 번의 호출에 쓰는 모델. 여기가 틀리면 뒤가 다 틀려서 따로 둔다.
    dependents: 종속항 텍스트. 있으면 청구항 대비가 끝난 뒤 이어서 종속항을 대비한다.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    say("[1/5] 인용발명 PDF 읽기")
    docs = read_docs(pdfs, say)
    say("[2/5] 청구항 구성 나누기")
    hint_model = model if split_model is None else split_model
    elements = claim_features(claim, split_claim(claim), hint_model, cache, langs=extra_langs(docs))
    for e in elements:
        say(f'  ({e["id"]}) ' + " / ".join(f["phrase"] for f in e["features"]))
    warn_no_terms(docs, elements, say)
    say("[3/5] 구성별 판정 (그대로 개시되어 있는가)")
    judgments = judge_all(docs, elements, model, cache, say=say)
    keep_manual(out_dir, docs, elements, judgments)
    say("[4/5] 메우지 못한 특징의 도출 근거 찾기")
    open_gaps = [g for g in compose(elements, judgments, [d["id"] for d in docs])["gaps"] if g["by"] is None]
    hints = {}
    if open_gaps:
        hints = suggest_gaps(claim, docs, elements, judgments, open_gaps, model, hint_model, cache, say=say)
    else:
        say("  해당 없음")
    say("[5/5] 인용발명 선택과 양식 작성")
    result = build_result(claim, elements, docs, judgments)
    names = {d["id"]: d["name"] for d in docs}
    for i, doc_id in enumerate(result["order"]):
        say(f"  인용발명 {i + 1}: {names[doc_id]}")
    (out_dir / "docs.json").write_text(json.dumps(docs, ensure_ascii=False), encoding="utf-8")
    (out_dir / "elements.json").write_text(json.dumps(elements, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "judgments.json").write_text(json.dumps(judgments, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "hints.json").write_text(json.dumps(hints, ensure_ascii=False, indent=2), encoding="utf-8")
    for name in DEP_FILES:  # 이전 문헌으로 한 종속항 판정은 더 이상 맞지 않는다
        (out_dir / name).unlink(missing_ok=True)
    save_result(out_dir, result)
    if dependents.strip():
        result = run_dependents(claim, dependents, out_dir, model, cache, say=say, split_model=split_model)
    return result


def run_dependents(claim: str, text: str, out_dir: Path, model: str, cache, say=print, split_model: str | None = None,
                   keep: bool = False) -> dict:
    """저장된 청구항 구성대비 결과 위에 종속항을 대비한다. PDF 를 다시 읽지 않는다.

    text: "[CLAIM 2]제1항에 있어서, …" 처럼 항 번호를 붙인 종속항들.
    keep: 종속항을 나눠서 더 넣을 때. 이미 대비한 항 가운데 글이 그대로인 항은 저장된 판정을 그대로 쓰고,
          새로 넣었거나 고친 항만 판정한다.
    """
    try:
        docs = json.loads((out_dir / "docs.json").read_text(encoding="utf-8"))
        elements = json.loads((out_dir / "elements.json").read_text(encoding="utf-8"))
        judgments = json.loads((out_dir / "judgments.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CompareError("저장된 청구항 구성대비 결과가 없습니다. 먼저 청구항 구성대비를 실행하세요.") from None
    hint_model = model if split_model is None else split_model
    base_no = _claim_no(claim)
    primary, extra = saved_order(out_dir)
    plan = compose(elements, judgments, [d["id"] for d in docs], primary=primary, extra=extra)

    say("[종속항 1/4] 인용 관계 읽기")
    try:
        deps, notes = claims.read_dependents(text, base_no)
    except claims.ClaimError as e:
        raise CompareError(str(e)) from None
    for dep in deps:
        chain = [n for n in dep["ancestors"] if n not in dep["refs"]]
        if dep.get("detached"):
            say(f'  청구항 {dep["no"]} ← {claims.ref_label(dep["refs"])} (대비 기록 없음: 이 항이 더한 구성만 대비)')
            continue
        say(f'  청구항 {dep["no"]} ← {claims.ref_label(dep["refs"])}'
            + (f' (거슬러 올라가면 {claims.ref_label(chain)}도 인용)' if chain else ""))
    for note in notes:
        say(f"  ! {note}")
    if not deps:
        raise CompareError("대비할 종속항이 없습니다. " + " / ".join(notes))
    contexts = dependent_contexts(claim, base_no, deps)
    dep_judgments, hints = _judge_fresh(
        out_dir, claim, base_no, docs, deps, contexts, lambda judged: plan, model, hint_model, cache, say, keep,
        lacking_features(elements, judgments),
        ("[종속항 2/4] 종속항이 더한 구성 나누기", "[종속항 3/4] 구성별 판정 (그대로 개시되어 있는가)",
         "[종속항 4/4] 메우지 못한 구성의 도출 근거 찾기"))
    dependents = {"base_no": base_no, "claims": deps, "notes": notes}
    (out_dir / "dependents.txt").write_text(text.strip() + "\n", encoding="utf-8")
    (out_dir / "dependents.json").write_text(json.dumps(dependents, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "dep_judgments.json").write_text(json.dumps(dep_judgments, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "dep_hints.json").write_text(json.dumps(hints, ensure_ascii=False, indent=2), encoding="utf-8")
    result = build_result(claim, elements, docs, judgments, primary=primary, extra=extra,
                          dependents=dependents, dep_judgments=dep_judgments)
    names = {d["id"]: d["name"] for d in docs}
    for i, doc_id in enumerate(result["numbering"]):
        say(f"  인용발명 {i + 1}: {names[doc_id]}")
    save_result(out_dir, result)
    return result


def saved_order(out_dir: Path) -> tuple:
    """지금 양식에 쓰인 청구항 1 의 인용발명 순서 (primary, extra). 저장된 결과가 없으면 자동 선택."""
    try:
        order = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))["order"]
        return order[0], list(order[1:])
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return "", None


def legend(numbering: list, docs: list) -> list:
    """양식 맨 위에 붙이는 대응표의 줄들("인용발명 1: CN105118055A"). 양식에 쓰인 문헌만, 번호순.

    번호는 넣은 순서가 아니라 양식에 쓰인 순서로 붙으므로(주인용이 1, 종속항만 대비하면 먼저 쓰인 문헌이 1),
    양식만 봐서는 어느 문헌인지 알 수 없다.
    """
    names = {d["id"]: d["name"] for d in docs}
    return [f"인용발명 {i + 1}: {names.get(doc_id, doc_id)}" for i, doc_id in enumerate(numbering)]


def full_text(result: dict) -> str:
    """대응표, 청구항 양식, 종속항 양식을 이어 붙인 전체 글."""
    return "\n\n".join(t for t in ("\n".join(result.get("legend") or []), result.get("text"), result.get("dep_text")) if t)


def save_result(out_dir: Path, result: dict) -> None:
    (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "result.txt").write_text(full_text(result) + "\n", encoding="utf-8")


def recompose(out_dir: Path, claim: str, primary: str = "", extra=None) -> dict:
    """저장된 판정으로 인용발명 순서만 바꿔 양식을 다시 만든다. LLM 을 부르지 않는다.

    종속항 양식도 함께 다시 만든다(인용발명 번호가 청구항 1 의 순서를 따라가므로).
    """
    docs = json.loads((out_dir / "docs.json").read_text(encoding="utf-8"))
    dependents, dep_judgments = load_dependents(out_dir)
    if dependents and dependents.get("alone"):  # 종속항만 대비한 기록: 바꿀 청구항 1 순서가 없다
        result = build_alone(dependents, docs, dep_judgments)
    else:
        elements = json.loads((out_dir / "elements.json").read_text(encoding="utf-8"))
        judgments = json.loads((out_dir / "judgments.json").read_text(encoding="utf-8"))
        result = build_result(claim, elements, docs, judgments, primary=primary, extra=extra,
                              dependents=dependents, dep_judgments=dep_judgments)
    save_result(out_dir, result)
    return result


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    load_env()
    p = argparse.ArgumentParser(prog="patsearch.compare", description="청구항과 인용발명 PDF 를 구성대비한다.")
    p.add_argument("--run", default="", help="검색 기록 폴더. 그 안의 00_claim.txt 와 compare\\docs\\*.pdf 를 쓴다")
    p.add_argument("--claim", default="", help="청구항 텍스트 파일")
    p.add_argument("--docs", nargs="*", default=[], help="인용발명 PDF 들")
    p.add_argument("--dependents", default="", help='종속항 텍스트 파일 ("[CLAIM 2]제1항에 있어서, …"). '
                   "--docs 없이 주면 저장된 청구항 결과 위에 종속항만 대비한다")
    p.add_argument("--dependents-only", action="store_true", help="청구항 대비는 저장된 결과를 쓰고 종속항만 대비한다")
    p.add_argument("--add", action="store_true", help="--dependents 로 준 종속항을 이미 넣어 둔 종속항에 더한다. "
                   "이미 대비한 항은 그대로 두고 새로 넣은 항만 판정한다 (번호가 같은 항은 새 글로 바꿔 다시 판정)")
    p.add_argument("--keep", action="store_true", help="이미 대비한 종속항 가운데 글이 그대로인 항은 저장된 판정을 그대로 쓴다")
    p.add_argument("--alone", action="store_true", help="독립항 없이 종속항만 대비한다 (--dependents 와 --docs, --claim 은 필요 없음)")
    p.add_argument("--out", default="", help="결과 폴더 (기본: 프로그램 폴더의 compare_out)")
    p.add_argument("--cli", default="", choices=["", *llm.CLIS])
    p.add_argument("--model", default=None, help="판정 모델 (기본: 설정의 구성대비 모델)")
    p.add_argument("--split-model", default=None, help="청구항을 특징으로 나누는 모델 (기본: 설정의 구성대비 모델)")
    p.add_argument("--no-cache", action="store_true")
    args = p.parse_args()

    settings = load_settings()
    cli = args.cli or settings["cli"]
    llm.set_cli(cli)
    chosen = settings["models"].get(cli, {})
    compare_model = chosen.get("compare_model", "")
    model = args.model if args.model is not None else compare_model
    split_model = args.split_model if args.split_model is not None else compare_model
    alone = args.alone
    if args.run:
        run_dir = Path(args.run)
        out_dir = run_dir / "compare"
        claim_path = run_dir / "00_claim.txt"
        names = {}
        try:
            names = json.loads((out_dir / "names.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        try:  # 화면에서 종속항만 넣어 만든 기록
            alone = alone or bool(json.loads((run_dir / "meta.json").read_text(encoding="utf-8")).get("no_base"))
        except (OSError, ValueError, AttributeError):
            pass
        pdfs = [(f.stem, names.get(f.stem, ""), f) for f in sorted((out_dir / "docs").glob("*.pdf"))]
    else:
        if alone and not args.dependents:
            p.error("--alone 은 --dependents 로 종속항 텍스트 파일을 함께 지정해야 합니다")
        if not alone and (not args.claim or not (args.docs or args.dependents or args.dependents_only)):
            p.error("--run 또는 (--claim 과 --docs) 를 지정하세요")
        claim_path = Path(args.claim) if args.claim else None
        out_dir = Path(args.out) if args.out else ROOT / "compare_out"  # 어디서 실행하든 프로그램 폴더 안에 둔다
        pdfs = [(f"d{i + 1}", "", Path(d)) for i, d in enumerate(args.docs)]
    if not alone and not claim_path.is_file():
        p.error(f"청구항 파일이 없습니다: {claim_path}")
    # 종속항: 따로 준 파일, 없으면 결과 폴더에 저장해 둔 것(화면에서 입력한 종속항)
    dep_path = Path(args.dependents) if args.dependents else out_dir / "dependents.txt"
    if args.dependents and not dep_path.is_file():
        p.error(f"종속항 파일이 없습니다: {dep_path}")
    dependents = dep_path.read_text(encoding="utf-8-sig").strip() if dep_path.is_file() else ""
    dep_only = args.dependents_only or args.add or (not args.run and not args.docs)
    merge_notes = []
    if args.add:  # 넣어 둔 종속항에 더한다
        if not args.dependents:
            p.error("--add 는 --dependents 로 더 넣을 종속항 텍스트 파일을 함께 지정해야 합니다")
        stored = out_dir / "dependents.txt"
        old = stored.read_text(encoding="utf-8-sig") if stored.is_file() else ""
        base = None if alone or not claim_path.is_file() else _claim_no(claim_path.read_text(encoding="utf-8-sig"))
        try:
            dependents, merge_notes, _ = claims.merge_dependents(old, dependents, base)
        except claims.ClaimError as e:
            p.error(str(e))
    if (dep_only or alone) and not dependents:
        p.error("종속항이 없습니다. --dependents 로 종속항 텍스트 파일을 지정하세요")
    if not dep_only and not pdfs:
        p.error("인용발명 PDF 가 없습니다")
    claim = "" if alone else claim_path.read_text(encoding="utf-8-sig").strip()
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "run.log"

    def say(msg: str) -> None:
        print(msg, flush=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")

    runs.write_status(out_dir, "running")
    say(f"── {'종속항 ' if dep_only else ''}구성대비 시작 ({llm.CLIS[cli]['label']}, 구성 나누기 {split_model or '기본값'}, "
        f"판정 {model or '기본값'}" + ("" if dep_only else f", 인용발명 {len(pdfs)}건") + ")")
    cache = Cache(enabled=not args.no_cache)
    keep = args.keep or args.add
    for note in merge_notes:
        say(f"  ! {note}")
    try:
        if alone:
            say("  독립항 없이 종속항만 대비합니다")
            result = run_alone(dependents, pdfs, out_dir, model, cache, say=say, split_model=split_model,
                               reuse_docs=dep_only, keep=keep)
        elif dep_only:
            result = run_dependents(claim, dependents, out_dir, model, cache, say=say, split_model=split_model, keep=keep)
        else:
            result = run_compare(claim, pdfs, out_dir, model, cache, say=say, split_model=split_model,
                                 dependents=dependents)
    except (CompareError, llm.LLMError) as e:
        say(f"오류: {e}")
        runs.write_status(out_dir, "error", message=str(e))
        return 1
    except Exception as e:
        import traceback

        say("예상하지 못한 오류:\n" + traceback.format_exc())
        runs.write_status(out_dir, "error", message=f"{type(e).__name__}: {e}")
        return 1
    runs.write_status(out_dir, "done")
    print("\n" + full_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
