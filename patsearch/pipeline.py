"""청구항 → 구성 분해 → 검색식 → 검색(히트 수 조정) → 초록 스크리닝 → 리포트."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import prompts
from .llm import LLMError, ask_json
from .openalex import OpenAlexError, search_works
from .ops import OpsClient, OpsQueryError
from .report import write_report

STAGES = ["elements", "queries", "search", "screen", "report"]
STAGE_FILES = {
    "elements": ["01_elements.json"],
    "queries": ["02_queries.json"],
    "search": ["03_search_log.json", "04_candidates.json"],
    "screen": ["05_screening.json"],
    "report": ["report.html"],
}
LEVEL_WEIGHT = {"narrow": 3, "medium": 2, "broad": 1}
COUNTRY_RANK = {"WO": 0, "EP": 1, "US": 2}
USABLE = ("ok", "few")  # 결과를 후보에 넣는 검색식 상태


@dataclass
class Settings:
    model: str = ""
    screen_model: str = "haiku"
    before: str = ""  # YYYYMMDD. 이 날짜 이전 공개 문헌만
    min_hits: int = 20
    max_hits: int = 400
    per_query: int = 100
    max_screen: int = 300
    max_papers: int = 50
    rounds: int = 2
    batch_size: int = 12
    workers: int = 4
    use_openalex: bool = True
    openalex_key: str = ""


_log_path: Path | None = None


def set_log(path: Path | None) -> None:
    """진행 메시지를 이 파일에도 남긴다. 웹 화면이 이 파일을 읽어 진행 상황을 보여 준다."""
    global _log_path
    _log_path = path


def say(msg: str) -> None:
    print(msg, flush=True)
    if _log_path is not None:
        try:
            with open(_log_path, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except OSError:
            pass


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


# --- 1. 구성 분해 ------------------------------------------------------------
def stage_elements(claim: str, s: Settings, cache) -> dict:
    decomp = ask_json(prompts.DECOMPOSE.format(claim=claim), model=s.model, cache=cache)
    if not isinstance(decomp, dict) or not decomp.get("elements"):
        raise LLMError("구성 분해 결과에 elements 가 없습니다")
    for e in decomp["elements"]:
        say(f'  [{e["id"]}] ({e.get("role", "core")}) {e.get("summary_ko", "")}')
        for i in e.get("implementations", []):
            say(f'        수단 가설: {i.get("name_ko", "")}')
    say("  CPC 후보: " + ", ".join(c.get("symbol", "") for c in decomp.get("cpc", [])))
    return decomp


# --- 2. 검색식 생성 ----------------------------------------------------------
def stage_queries(decomp: dict, s: Settings, cache) -> dict:
    prompt = prompts.QUERIES.format(
        elements=prompts.compact_elements(decomp),
        cql_guide=prompts.CQL_GUIDE,
        min_hits=s.min_hits,
        max_hits=s.max_hits,
    )
    queries = ask_json(prompt, model=s.model, cache=cache)
    if not isinstance(queries, dict) or not queries.get("ops_queries"):
        raise LLMError("검색식 생성 결과에 ops_queries 가 없습니다")
    queries.setdefault("openalex_queries", [])
    for q in queries["ops_queries"]:
        say(f'  {q["id"]:<4} {q.get("level", ""):<7} {q["cql"]}')
    for q in queries["openalex_queries"]:
        say(f'  {q["id"]:<4} openalex {q["query"]}')
    return queries


# --- 3. 검색 실행 + 히트 수 기반 조정 ------------------------------------------
def _with_date(cql: str, before: str) -> str:
    return f"({cql}) and pd<={before}" if before else cql


def _results_text(entries: list, current_round: int) -> str:
    lines = []
    for e in entries:
        flag = " NEEDS_FIX" if e["round"] == current_round and e["status"] != "ok" else ""
        detail = f'error="{e["error"]}"' if e["status"] == "error" else f'hits={e["hits"]}'
        lines.append(f'{e["id"]} [{e["level"]}] {detail} status={e["status"]}{flag} :: {e["cql"]}')
    return "\n".join(lines)


def _run_ops_queries(decomp: dict, queries: dict, ops: OpsClient, s: Settings, cache):
    """검색식을 실행하고 히트 수에 따라 고쳐 쓴다. (실행 기록, 검색식 id → 문헌 목록)을 돌려준다.

    요청 수를 줄이려고 히트 수 확인과 서지 수집을 한 번의 요청으로 한다.
    """
    entries, fetched = [], {}
    pending = [
        {
            "id": q["id"], "root": q["id"], "level": q.get("level", "medium"),
            "elements": q.get("elements", []), "rationale_ko": q.get("rationale_ko", ""),
            "cql": q["cql"], "round": 0, "parent": None, "change_ko": "",
        }
        for q in queries["ops_queries"]
    ]
    for rnd in range(s.rounds + 1):
        for q in pending:
            entry = dict(q, source="ops", hits=None, status="ok", error="")
            try:
                result = ops.search(_with_date(q["cql"], s.before), limit=s.per_query)
                entry["hits"] = result["total"]
                fetched[q["id"]] = result["docs"]
                if entry["hits"] == 0:
                    entry["status"] = "empty"
                elif entry["hits"] < s.min_hits:
                    entry["status"] = "few"  # 결과는 쓰되, 더 넓힌 검색식을 하나 더 만든다
                elif entry["hits"] > s.max_hits:
                    entry["status"] = "too_broad"
            except OpsQueryError as e:
                entry["status"], entry["error"] = "error", str(e)
            entries.append(entry)
            shown = entry["error"] if entry["status"] == "error" else f'{entry["hits"]}건'
            say(f'  {entry["id"]:<6} {entry["status"]:<9} {shown:<10} {entry["cql"]}')
        bad = [e for e in entries if e["round"] == rnd and e["status"] != "ok"]
        if not bad or rnd == s.rounds:
            break
        say(f"  → {len(bad)}개 검색식을 히트 수에 맞춰 수정합니다 ({rnd + 1}/{s.rounds}회차)")
        prompt = prompts.REFINE.format(
            elements=prompts.compact_elements(decomp),
            cql_guide=prompts.CQL_GUIDE,
            results=_results_text(entries, rnd),
            min_hits=s.min_hits,
            max_hits=s.max_hits,
        )
        try:
            revisions = ask_json(prompt, model=s.model, cache=cache).get("revisions", [])
        except (LLMError, AttributeError) as e:
            say(f"  ! 검색식 수정 실패, 현재 결과로 진행합니다: {e}")
            break
        by_id = {e["id"]: e for e in bad}
        pending = []
        for r in revisions:
            parent = by_id.get(str(r.get("parent", "")))
            if parent is None or not r.get("cql"):
                continue
            new_id = f'{parent["root"]}r{rnd + 1}'
            parent["superseded_by"] = new_id
            pending.append(
                {
                    "id": new_id, "root": parent["root"], "level": parent["level"],
                    "elements": parent["elements"], "rationale_ko": parent["rationale_ko"],
                    "cql": r["cql"], "round": rnd + 1, "parent": parent["id"],
                    "change_ko": r.get("change_ko", ""),
                }
            )
        if not pending:
            break
    return entries, fetched


def _doc_rank(doc: dict):
    return (
        0 if doc.get("abstract_lang") == "en" else 1,
        0 if doc.get("abstract") else 1,
        COUNTRY_RANK.get(doc.get("country", ""), 9),
        doc.get("pub_date") or "99999999",
    )


def _collect_patents(entries: list, fetched: dict) -> list:
    families: dict[str, dict] = {}
    for e in entries:
        if e["status"] not in USABLE:
            continue
        docs = fetched.get(e["id"], [])
        e["fetched"] = len(docs)
        for doc in docs:
            fam = families.setdefault(doc["family_id"], {"docs": {}, "queries": []})
            fam["docs"].setdefault(doc["pn"], doc)
            if e["id"] not in fam["queries"]:
                fam["queries"].append(e["id"])
    levels = {e["id"]: e["level"] for e in entries}
    candidates = []
    for family_id, fam in families.items():
        docs = sorted(fam["docs"].values(), key=_doc_rank)
        best = docs[0]
        dates = [d["pub_date"] for d in docs if d.get("pub_date")]
        candidates.append(
            {
                "key": f"P:{family_id}", "kind": "patent", "pn": best["pn"],
                "members": [d["pn"] for d in docs],
                "title": best["title"], "abstract": best["abstract"],
                "pub_date": min(dates) if dates else "", "priority_date": best.get("priority_date", ""),
                "applicants": best.get("applicants", []), "cpc": best.get("cpc", []),
                "hit_queries": fam["queries"],
                "prescore": sum(LEVEL_WEIGHT.get(levels.get(q, ""), 1) for q in fam["queries"]),
            }
        )
    candidates.sort(key=lambda c: (-c["prescore"], c["pn"]))
    return candidates


def _collect_papers(queries: dict, entries: list, s: Settings, cache) -> list:
    papers: dict[str, dict] = {}
    for q in queries.get("openalex_queries", []):
        entry = {
            "id": q["id"], "root": q["id"], "source": "openalex", "level": "paper", "elements": [],
            "rationale_ko": q.get("rationale_ko", ""), "cql": q["query"], "round": 0, "parent": None,
            "change_ko": "", "hits": None, "status": "ok", "error": "",
        }
        try:
            result = search_works(q["query"], per_page=25, api_key=s.openalex_key, before=s.before, cache=cache)
            entry["hits"], entry["fetched"] = result["total"], len(result["works"])
            for w in result["works"]:
                paper = papers.setdefault(
                    w["id"],
                    {
                        "key": f'W:{w["id"]}', "kind": "paper", "pn": w["id"], "url": w["url"],
                        "title": w["title"], "abstract": w["abstract"], "pub_date": w["pub_date"],
                        "applicants": [w["venue"]] if w["venue"] else [], "cpc": [],
                        "hit_queries": [], "prescore": 0, "cited_by": w["cited_by"],
                    },
                )
                paper["hit_queries"].append(q["id"])
                paper["prescore"] += 1
        except OpenAlexError as e:
            entry["status"], entry["error"] = "error", str(e)
        entries.append(entry)
        shown = entry["error"][:60] if entry["status"] == "error" else f'{entry["hits"]}건'
        say(f'  {entry["id"]:<6} {entry["status"]:<9} {shown:<10} {entry["cql"]}')
    return sorted(papers.values(), key=lambda c: (-c["prescore"], -(c.get("cited_by") or 0)))


def stage_search(decomp: dict, queries: dict, ops: OpsClient | None, s: Settings, cache):
    entries, candidates = [], []
    if ops is None:
        say("  ! EPO OPS 키가 없어 특허 검색을 건너뜁니다 (.env 에 EPO_OPS_KEY / EPO_OPS_SECRET 입력)")
    else:
        # OPS 오류(차단·쿼터·네트워크)는 위로 올려 실행을 멈춘다. 특허가 빠진 리포트를 만들지 않기 위해서다.
        # 이미 받은 응답은 캐시에 있으므로 같은 폴더로 다시 실행하면 이어서 진행된다.
        ops.on_wait = lambda sec: say(f"  … EPO 요청 제한으로 {round(sec)}초 대기")
        entries, fetched = _run_ops_queries(decomp, queries, ops, s, cache)
        candidates = _collect_patents(entries, fetched)
        say(f"  특허 후보 {len(candidates)}개 패밀리 (OPS 요청 {ops.requests_made}회)")
    if s.use_openalex:
        papers = _collect_papers(queries, entries, s, cache)
        say(f"  논문 후보 {len(papers)}건")
        candidates += papers
    return entries, candidates


# --- 4. 초록 스크리닝 ---------------------------------------------------------
ROLE_WEIGHT = {"key": 4, "core": 2, "context": 1}


def key_elements(decomp: dict) -> list:
    """차별점이 되는 구성의 id. 표시가 없으면(예전 파일) core 구성 전체를 쓴다."""
    elements = decomp.get("elements", [])
    keys = [e["id"] for e in elements if e.get("role") == "key"]
    return keys or [e["id"] for e in elements if e.get("role", "core") == "core"] or [e["id"] for e in elements]


def _score(scores: dict, weights: dict) -> int:
    total = sum(w * 2 for w in weights.values()) or 1
    got = sum(w * max(0, min(2, int(scores.get(k, 0) or 0))) for k, w in weights.items())
    return round(100 * got / total)


def _grade(scores: dict, score: int, keys: list) -> str:
    """등급은 차별점 구성이 결정한다. 전제부만 맞는 문헌이 '상'이 되지 않게 하기 위해서다."""
    key_min = min(scores.get(k, 0) for k in keys)
    if key_min == 2 and score >= 60:
        return "high"
    if key_min >= 1 and score >= 40:
        return "mid"
    return "low"


def stage_screen(claim: str, decomp: dict, candidates: list, s: Settings, cache) -> list:
    patents = [c for c in candidates if c["kind"] == "patent"][: s.max_screen]
    papers = [c for c in candidates if c["kind"] == "paper"][: s.max_papers]
    targets = [c for c in patents + papers if c.get("title") or c.get("abstract")]
    if not targets:
        return []
    weights = {e["id"]: ROLE_WEIGHT.get(e.get("role", "core"), 2) for e in decomp["elements"]}
    keys = key_elements(decomp)
    score_keys = ", ".join(f'"{k}": 0' for k in weights)
    elements_text = prompts.screening_elements(decomp)
    batches = [targets[i : i + s.batch_size] for i in range(0, len(targets), s.batch_size)]

    def run_batch(batch):
        payload = [
            {"i": i, "title": c.get("title", ""), "abstract": (c.get("abstract") or "(초록 없음)")[:1500]}
            for i, c in enumerate(batch)
        ]
        prompt = prompts.SCREEN.format(
            claim=claim, elements=elements_text,
            candidates=json.dumps(payload, ensure_ascii=False, indent=1), score_keys=score_keys,
        )
        try:
            answer = ask_json(prompt, model=s.screen_model, cache=cache)
            rows = answer.get("results", []) if isinstance(answer, dict) else answer
            by_i = {int(r["i"]): r for r in rows if isinstance(r, dict) and "i" in r}
        except (LLMError, ValueError, TypeError) as e:
            say(f"  ! 스크리닝 배치 실패: {e}")
            by_i = {}
        out = []
        for i, c in enumerate(batch):
            r = by_i.get(i)
            if r is None:
                out.append({"key": c["key"], "scores": {}, "score": None, "grade": "unscreened", "reason_ko": ""})
                continue
            scores = {k: max(0, min(2, int((r.get("scores") or {}).get(k, 0) or 0))) for k in weights}
            score = _score(scores, weights)
            out.append({"key": c["key"], "scores": scores, "score": score,
                        "key_score": sum(scores.get(k, 0) for k in keys),
                        "grade": _grade(scores, score, keys), "reason_ko": r.get("reason_ko", "")})
        return out

    results, done = [], 0
    with ThreadPoolExecutor(max_workers=s.workers) as pool:
        for out in pool.map(run_batch, batches):
            results.extend(out)
            done += 1
            say(f"  스크리닝 {done}/{len(batches)} 배치")
    counts = {g: sum(1 for r in results if r["grade"] == g) for g in ("high", "mid", "low", "unscreened")}
    say(f'  상 {counts["high"]} · 중 {counts["mid"]} · 하 {counts["low"]} · 미판정 {counts["unscreened"]}')
    return results


# --- 실행 --------------------------------------------------------------------
def clear_from(run_dir: Path, stage: str) -> None:
    for st in STAGES[STAGES.index(stage):]:
        for name in STAGE_FILES[st]:
            (run_dir / name).unlink(missing_ok=True)


def run(claim: str, run_dir: Path, s: Settings, cache, ops: OpsClient | None, stop_after: str = "report") -> Path | None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "00_claim.txt").write_text(claim, encoding="utf-8")
    last = STAGES.index(stop_after)

    def have(stage):
        return all((run_dir / n).exists() for n in STAGE_FILES[stage])

    say("[1/5] 청구항 구성 분해")
    f_elements = run_dir / "01_elements.json"
    if have("elements"):
        decomp = _read(f_elements)
        say("  (기존 파일 사용)")
    else:
        decomp = stage_elements(claim, s, cache)
        _write(f_elements, decomp)
    if last == 0:
        return None

    say("[2/5] 검색식 생성")
    f_queries = run_dir / "02_queries.json"
    if have("queries"):
        queries = _read(f_queries)
        say("  (기존 파일 사용)")
    else:
        queries = stage_queries(decomp, s, cache)
        _write(f_queries, queries)
    if last == 1:
        return None

    say("[3/5] 검색 실행 및 히트 수 조정")
    f_log, f_cands = run_dir / "03_search_log.json", run_dir / "04_candidates.json"
    if have("search"):
        log, candidates = _read(f_log), _read(f_cands)
        say("  (기존 파일 사용)")
    else:
        log, candidates = stage_search(decomp, queries, ops, s, cache)
        _write(f_log, log)
        _write(f_cands, candidates)
    if last == 2:
        return None

    say("[4/5] 초록 스크리닝")
    f_screen = run_dir / "05_screening.json"
    if have("screen"):
        screening = _read(f_screen)
        say("  (기존 파일 사용)")
    else:
        screening = stage_screen(claim, decomp, candidates, s, cache)
        _write(f_screen, screening)
    if last == 3:
        return None

    say("[5/5] 리포트 작성")
    report = run_dir / "report.html"
    write_report(report, claim, decomp, log, candidates, screening, s)
    say(f"  {report}")
    return report
