"""청구항 묶음을 항별로 나누고, 종속항이 어느 항을 인용하는지 읽는다. 전부 프로그램이 하며 LLM 을 쓰지 않는다.

여러 항을 한꺼번에 붙여 넣어도 된다. 항을 나누는 단서는 다음 순서로 찾는다.

1. 항 번호 표지: "[CLAIM 2]", "[청구항 2]", "【청구항 2】", "<청구항 2>", 줄 첫머리의 "청구항 2." / "청구항 2" /
   "청구항 2 제1항에 있어서 …". 줄을 바꾸지 않고 "… 방법. 청구항 3. 제2항에 있어서 …" 처럼 이어 붙여도 된다.
2. 표지가 없으면 줄 첫머리의 번호: "2. 제1항에 있어서 …", "3) 제2항에 있어서 …".
3. 번호가 아예 없으면 종속항 앞머리("제1항에 있어서", "제1항 내지 제3항 중 어느 한 항에 있어서")가 나오는
   자리마다 나누고, 순서대로 번호를 붙인다.
"""
from __future__ import annotations

import re


class ClaimError(Exception):
    pass


_OPEN, _CLOSE = r"[\[［〔<〈《(（【]", r"[\]］〕>〉》)）】]"
# 줄 첫머리의 "청구항 2" 뒤에 바로 본문이 오는 경우. "청구항 1 내지 3 중 …", "청구항 1 에 있어서" 는 표지가 아니다.
_NOT_BODY = r"(?:내지|또는|혹은|및|중|항|기재|따른|따르는|의한|or\b|to\b|and\b|[에의은는을를과와이가][\s,])"
_MARK = re.compile(
    rf"{_OPEN}\s*(?:claim|청구항)\s*(\d{{1,3}})\s*{_CLOSE}"
    rf"|^[ \t]*(?:청구항|claim)[ \t]*(\d{{1,3}})[ \t]*(?:[.:：]|$|(?=[ \t]+(?!{_NOT_BODY})\S))",  # "청구항 1에 있어서" 는 표지가 아니다
    re.I | re.M,
)
_BARE = re.compile(r"^[ \t]*(\d{1,3})[ \t]*[.)](?!\d)\s*(?=\S)", re.M)  # "2. 제1항에 있어서"
# 종속항 앞머리. 번호가 없는 글을 나눌 때 쓴다.
_HEAD = re.compile(r"[ \t]*(?:제\s*\d{1,3}\s*항(?!목)|청구항\s*\d{1,3})[^\n]{0,160}?(있어서|따른|따르는|기재된|기재의|의한)")
_DELETED = re.compile(r"[\[(（<]?\s*삭\s*제\s*[\])）>]?\s*\.?")
# 인용 항 번호: "제1항", "제 1 항", "청구항 1", "claim 1". "제1 전극" 같은 말은 항 번호가 아니다.
_TOKEN = re.compile(r"(청구항\s*|claims?\s*|제\s*)?(\d{1,3})(\s*항)?", re.I)
_RANGE = re.compile(r"\s*(?:내지|부터|에서|~|∼|〜|-|–|to|through)\s*", re.I)
_LIST = re.compile(r"\s*(?:,|및|또는|혹은|이나|,\s*및|,\s*또는|or|and|,\s*or|,\s*and)\s*", re.I)


def strip_mark(text: str) -> str:
    """맨 앞의 항 번호 표지("청구항 1." 등)를 뗀다."""
    text = text.strip()
    m = _MARK.match(text)
    return text[m.end():].strip() if m else text


def _prepare(text: str) -> str:
    """한 줄에 이어 붙인 항들을 줄 단위로 떼어 놓는다. 항이 끝나는 마침표 뒤에서만 뗀다."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"([.。])[ \t]*(?=(?:청구항|claim)[ \t]*\d{1,3}[ \t]*[.:：])", r"\1\n", text, flags=re.I)
    head = r"(?:제\s*\d{1,3}\s*항|청구항\s*\d{1,3})[^\n.]{0,160}?있어서"
    text = re.sub(rf"([.。])[ \t]*(?=\d{{1,3}}[ \t]*[.)][ \t]*{head})", r"\1\n", text)  # "… 방법. 3. 제2항에 있어서 …"

    def cut(m):
        line = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
        return m.group(0) if re.fullmatch(r"\s*\d{1,3}\s*", line) else m.group(1) + "\n"  # "2. 제1항에 …" 의 마침표는 번호의 것

    return re.sub(rf"([.。])[ \t]*(?={head})", cut, text)


def _bare_marks(text: str) -> list:
    """줄 첫머리의 "2." 가운데 항 번호로 볼 수 있는 것만 고른다. 본문 속 "1. … 2. …" 나열은 거른다."""
    out, last = [], None
    for m in _BARE.finditer(text):
        n = int(m.group(1))
        first = last is None and not text[:m.start()].strip()
        if first or _HEAD.match(text, m.end()) or (last is not None and n == last + 1):
            out.append(m)
            last = n
    return out


def _head_starts(text: str) -> list:
    """번호가 없는 글에서 종속항이 시작하는 자리들."""
    starts = []
    for m in re.finditer(r"^", text, re.M):
        head = _HEAD.match(text, m.start())
        if not head:
            continue
        before = text[:m.start()]
        # "… 있어서"는 그 자체로 앞머리다. "제1항에 따른 …" 은 본문 줄일 수도 있어, 앞 항이 끝난 자리에서만 받는다.
        if head.group(1) == "있어서" or not before.strip() or before.rstrip().endswith((".", "。")) or before.endswith("\n\n"):
            starts.append(m.start())
    return starts


def parse_claims(text: str, base_no: int = 1, notes: list | None = None) -> list:
    """항을 나눈다. [{no, text, raw}] 를 번호순으로 돌려준다. text 는 한 줄로 편 글, raw 는 줄바꿈을 살린 글.

    base_no: 번호가 없는 글에 번호를 붙일 때의 기준. 인용이 없는 앞부분은 base_no, 종속항은 base_no+1 부터.
    notes: 알림을 덧붙일 목록(번호를 붙였음, 삭제된 항을 건너뜀 등).
    """
    notes = notes if notes is not None else []
    text = _prepare(text)
    marks = [(m.start(), m.end(), int(next(g for g in m.groups() if g))) for m in _MARK.finditer(text)]
    if not marks:
        marks = [(m.start(), m.end(), int(m.group(1))) for m in _bare_marks(text)]
    lead = ""
    if marks:
        lead = text[:marks[0][0]].strip()
        # 번호가 붙은 항 뒤에 번호 없는 종속항이 이어 붙은 경우("청구항 1. … 방법.  제1항에 있어서, …"):
        # 앞 항에 이어지는 번호를 붙인다. 그 번호를 다른 표지가 이미 쓰고 있으면 본문으로 보고 나누지 않는다.
        used, added, numbered = {no for _, _, no in marks}, [], []
        for s in _head_starts(text):
            seg = next((mk for mk in reversed(marks) if mk[0] <= s), None)
            if seg is None or not text[seg[1]:s].strip():
                continue  # 첫 표지 앞의 글이거나, 표지 바로 뒤에 오는 그 항 자신의 앞머리
            after = [a for a in added if a[0] > seg[0]]
            no = (after[-1][2] if after else seg[2]) + 1
            if no in used:
                continue
            used.add(no)
            added.append((s, s, no))
            numbered.append(no)
        if added:
            marks = sorted(marks + added)
            notes.append("번호가 없는 항은 앞 항에 이어 청구항 " + ", ".join(map(str, numbered))
                         + " 로 번호를 붙였습니다. 번호가 다르면 항마다 앞에 번호를 적어 주세요")
    else:
        starts = _head_starts(text)
        if not starts:
            raise ClaimError('항을 나누지 못했습니다. 항마다 앞에 "청구항 2." 또는 "[청구항 2]" 처럼 번호를 붙이거나, '
                             '"제1항에 있어서, …" 로 시작하는 종속항을 넣어 주세요.')
        marks = [(s, s, base_no + 1 + i) for i, s in enumerate(starts)]
        if text[:starts[0]].strip():
            marks.insert(0, (0, 0, base_no))
        first, last = base_no + 1, base_no + len(starts)
        notes.append("항 번호가 없어 나온 순서대로 " + (f"청구항 {first}" if first == last else f"청구항 {first}~{last}")
                     + " 로 번호를 붙였습니다. 번호가 다르면 항마다 앞에 번호를 적어 주세요")
    if lead and len(lead) > 20:  # 첫 표지 앞의 글: 번호 없이 붙여 넣은 독립항으로 본다
        if any(no == base_no for _, _, no in marks):
            notes.append("첫 항 번호 앞에 있는 글은 어느 항인지 알 수 없어 뺐습니다")
        else:
            marks.insert(0, (0, 0, base_no))
    out, seen = [], set()
    for i, (_, body_start, no) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        raw = text[body_start:end].strip()
        body = " ".join(raw.split())
        if not body:
            continue
        if _DELETED.fullmatch(body):
            notes.append(f"청구항 {no}: 삭제된 항이라 건너뛰었습니다")
            continue
        if no in seen:
            raise ClaimError(f"청구항 {no} 이 두 번 들어 있습니다")
        seen.add(no)
        out.append({"no": no, "text": body, "raw": raw})
    if not out:
        raise ClaimError("항 번호만 있고 내용이 없습니다")
    return sorted(out, key=lambda c: c["no"])


def _guess_base(text: str) -> int:
    """독립항 없이 번호도 없는 종속항만 있을 때 번호를 붙일 기준: 첫 종속항이 인용하는 가장 뒤의 항."""
    text = _prepare(text)
    starts = _head_starts(text)
    refs = _numbers(_HEAD.match(text, starts[0]).group(0)) if starts else []
    return max(refs) if refs else 1


def read_dependents(text: str, base_no: int | None) -> tuple:
    """종속항 글을 항별로 나누고 인용 관계까지 읽는다. (종속항 목록, 알림 목록)

    base_no 가 None 이면 대비해 둔 독립항이 없는 경우다. 모든 항을 "인용하는 항의 대비 기록 없음"으로 읽는다.
    """
    notes = []
    items = parse_claims(text, base_no=_guess_base(text) if base_no is None else base_no, notes=notes)
    deps, more = resolve(items, base_no)
    return deps, notes + more


ALONE_NOTE = ("독립항이 없어 종속항만 대비합니다. 항마다 더한 구성만 인용발명에서 찾고, 인용하는 항의 대비 기록이 없으므로 "
              "결론은 쓰지 않습니다. 독립항을 맨 앞에 함께 넣으면 \"상기 …\"를 그 글에 비추어 읽고 결론까지 씁니다")


def split_set(text: str) -> dict:
    """한꺼번에 붙여 넣은 청구항 묶음을 대비할 독립항과 종속항으로 나눈다.

    {base_no, base(00_claim.txt 에 쓸 글), dependents(dependents.txt 에 쓸 글, 없으면 ""), deps, notes}
    독립항 하나만 넣은 글이면 그대로 돌려준다. 독립항은 가장 앞 번호의 항이다.
    가장 앞의 항이 종속항이면 독립항 없이 종속항만 넣은 것이다. 이때 base_no 는 None, base 는 "" 이다.
    """
    text = text.strip()
    notes = []
    try:
        items = parse_claims(text, base_no=1, notes=notes)
    except ClaimError:
        items = []
    if not items:  # 번호도 종속항 앞머리도 없다: 독립항 하나
        return {"base_no": 1, "base": text, "dependents": "", "deps": [], "notes": []}
    first, rest = items[0], items[1:]
    if split_reference(first["text"])[1]:  # 독립항 없이 종속항만. 번호 없는 글은 인용하는 항에 맞춰 번호를 다시 붙인다
        notes = []
        items = parse_claims(text, base_no=_guess_base(text), notes=notes)
        deps, more = resolve(items, None)
        if not deps:
            raise ClaimError("대비할 종속항이 없습니다. " + " / ".join(notes + more))
        return {"base_no": None, "base": "", "dependents": "\n".join(f'[청구항 {c["no"]}] {c["text"]}' for c in items),
                "deps": deps, "notes": [ALONE_NOTE] + notes + more}
    deps, more = resolve(rest, first["no"]) if rest else ([], [])
    return {
        "base_no": first["no"],
        "base": f'청구항 {first["no"]}.\n{first["raw"]}',
        "dependents": "\n".join(f'[청구항 {c["no"]}] {c["text"]}' for c in rest),
        "deps": deps,
        "notes": notes + more,
    }


def _numbers(head: str) -> list:
    """앞머리("제1항 내지 제3항 중 어느 한 항에 있어서")에서 인용하는 항 번호를 뽑는다."""
    refs, prev = [], None  # prev: (번호, 끝 위치)
    for m in _TOKEN.finditer(head):
        marked = bool(m.group(3)) or (m.group(1) or "").lower().startswith(("청구항", "claim"))
        between = head[prev[1]:m.start()] if prev else None
        ranged = between is not None and bool(_RANGE.fullmatch(between))
        listed = between is not None and bool(_LIST.fullmatch(between))
        if not (marked or ranged or listed):
            continue
        n = int(m.group(2))
        if ranged and n > prev[0]:
            refs.extend(range(prev[0] + 1, n + 1))
        else:
            refs.append(n)
        prev = (n, m.end())
    return sorted(set(refs))


def ref_label(refs: list) -> str:
    """[1, 2, 3] → "제1항 내지 제3항", [1, 3] → "제1항, 제3항" """
    if len(refs) >= 3 and refs == list(range(refs[0], refs[-1] + 1)):
        return f"제{refs[0]}항 내지 제{refs[-1]}항"
    return ", ".join(f"제{r}항" for r in refs)


def canonical_preamble(refs: list) -> str:
    if len(refs) == 1:
        return f"제{refs[0]}항에 있어서"
    if len(refs) == 2:
        return f"제{refs[0]}항 또는 제{refs[1]}항에 있어서"
    if refs == list(range(refs[0], refs[-1] + 1)):
        return f"제{refs[0]}항 내지 제{refs[-1]}항 중 어느 한 항에 있어서"
    return ", ".join(f"제{r}항" for r in refs[:-1]) + f" 또는 제{refs[-1]}항 중 어느 한 항에 있어서"


def split_reference(text: str) -> tuple:
    """종속항을 (앞머리, 인용하는 항 번호들, 나머지)로 나눈다. 인용이 없으면 ("", [], text)."""
    m = re.match(r"(.{0,200}?있어서)\s*[,，]?\s*", text)
    if m:
        refs = _numbers(m.group(1))
        if refs:  # 앞머리는 청구항에 적힌 그대로 쓴다
            return " ".join(m.group(1).split()), refs, text[m.end():].strip()
    # "제1항에 따른 방법으로서, …", "제1항의 방법을 수행하는 장치로서, …"
    m = re.match(r"(.{0,200}?)(?:[,，:]|$)", text)
    refs = _numbers(m.group(1)) if m else []
    if refs:
        return canonical_preamble(refs), refs, text
    return "", [], text


def resolve(claims: list, base_no: int | None) -> tuple:
    """종속항마다 인용 관계를 정리한다. (종속항 목록, 알림 목록)을 돌려준다.

    base_no: 이미 구성대비를 마친 청구항(보통 1). None 이면 대비해 둔 청구항이 없다(종속항만 대비).
    종속항: {id, no, refs(직접 인용하는 항), ancestors(거슬러 올라가며 닿는 모든 항), preamble, text, body,
            detached, unrecorded, context_texts}
    인용하는 항의 대비 기록이 프로그램에 없어도(사람이 따로 대비한 항 등) 그 종속항은 뺀 것이 아니라
    detached 로 표시해 두고, 그 항이 더한 구성만 인용발명과 대비한다. unrecorded 는 기록이 없는 항 번호,
    context_texts 는 대비하지는 않았지만 글은 들어 있는 항(다른 독립항 등)의 본문이다.
    """
    alone = base_no is None
    known, by_no, deps, notes = (set() if alone else {base_no}), {}, [], []
    texts = {c["no"]: c["text"] for c in claims}
    for c in sorted(claims, key=lambda c: c["no"]):
        no = c["no"]
        if no == base_no:
            notes.append(f"청구항 {no}: 이미 대비한 청구항이므로 건너뛰었습니다")
            continue
        preamble, cited, body = split_reference(c["text"])
        if not cited:
            notes.append(f"청구항 {no}: 다른 항을 인용하지 않는 독립항으로 보여 종속항 대비에서 뺐습니다")
            continue
        later = [r for r in cited if r >= no]
        earlier = [r for r in cited if r < no]
        refs = [r for r in earlier if r in known]
        missing = [r for r in earlier if r not in known]
        if not earlier:
            notes.append(f"청구항 {no}: 자신보다 앞선 항이 아닌 {ref_label(later)}을 인용하고 있어 대비하지 못했습니다")
            continue
        if later:
            notes.append(f"청구항 {no}: 자신보다 앞선 항이 아닌 {ref_label(later)}의 인용은 무시했습니다")
        dep = {"id": str(no), "no": no, "preamble": preamble, "text": c["text"], "body": body}
        if not refs:
            # 인용하는 항의 대비 기록이 없다. 이 항이 더한 구성만 대비한다.
            # 글도 없는 항이 있으면 뿌리가 이미 대비한 청구항이라고 보고 그 본문을 맥락으로 쓴다.
            lost = [r for r in missing if r not in texts]
            dep.update(refs=missing, ancestors=sorted(set(missing) | ({base_no} if lost and not alone else set())),
                       detached=True, unrecorded=missing, context_texts={str(r): texts[r] for r in missing if r in texts})
            if not alone:  # 종속항만 대비할 때는 모든 항이 그렇다. 한 번만 알린다(ALONE_NOTE)
                notes.append(f"청구항 {no}: 인용하는 {ref_label(missing)}의 구성대비 기록이 없어, 이 항이 더한 구성만 인용발명과 "
                             f"대비했습니다. 결론은 쓰지 않았습니다")
        else:
            if missing and not alone:
                notes.append(f"청구항 {no}: 인용하는 항 가운데 {ref_label(missing)}은 대비 기록이 없어 {ref_label(refs)}만 기준으로 대비했습니다")
            ancestors, unrecorded, context_texts = set(refs), set(), {}
            for r in refs:
                parent = by_no.get(r) or {}
                ancestors.update(parent.get("ancestors") or ())
                unrecorded.update(parent.get("unrecorded") or ())
                context_texts.update(parent.get("context_texts") or {})
            dep.update(refs=refs, ancestors=sorted(ancestors), detached=False, unrecorded=sorted(unrecorded),
                       context_texts=context_texts)
        by_no[no] = dep
        known.add(no)
        deps.append(dep)
    return deps, notes
