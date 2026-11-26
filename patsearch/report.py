"""결과를 브라우저에서 여는 단일 HTML 파일로 만든다."""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

GRADE_LABEL = {"high": "상", "mid": "중", "low": "하", "unscreened": "미판정"}
STATUS_LABEL = {"ok": "사용", "few": "사용(소량)", "empty": "0건", "too_broad": "과다", "error": "오류"}

TEMPLATE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1c1d1f;--mute:#676b73;--line:#dfe0e3;--accent:#4b6a9b;
--hi:#0d7a4a;--hi-bg:#dff3e8;--mid:#8a5a00;--mid-bg:#fbeecb;--lo:#676b73;--lo-bg:#ebecee;--err:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#16171a;--card:#1f2125;--ink:#e8e9eb;--mute:#9a9fa8;--line:#34373d;
--accent:#a9c1e6;--hi:#7fdcab;--hi-bg:#143a28;--mid:#f1c96b;--mid-bg:#40330f;--lo:#9a9fa8;--lo-bg:#2b2d32;--err:#ff8a80}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 "Segoe UI","Malgun Gothic",system-ui,sans-serif}
main{max-width:1280px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:34px 0 10px}
.meta{color:var(--mute);font-size:13px}
.tiles{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 16px;min-width:120px}
.tile b{display:block;font-size:22px;font-variant-numeric:tabular-nums}.tile span{color:var(--mute);font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.claim{white-space:pre-wrap}
.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--mute);font-weight:600;white-space:nowrap;position:sticky;top:0;background:var(--card)}
tr:last-child td{border-bottom:0}
code{font:12.5px/1.5 Consolas,"Cascadia Mono",monospace;word-break:break-word}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.badge{display:inline-block;border-radius:999px;padding:1px 9px;font-size:12px;font-weight:600;white-space:nowrap}
.g-high{color:var(--hi);background:var(--hi-bg)}.g-mid{color:var(--mid);background:var(--mid-bg)}
.g-low,.g-unscreened{color:var(--lo);background:var(--lo-bg)}
.s-ok,.s-few{color:var(--hi)}.s-empty,.s-too_broad{color:var(--mid)}.s-error{color:var(--err)}
.el{display:inline-block;min-width:34px;text-align:center;border-radius:4px;padding:0 5px;margin-right:3px;font-size:12px}
.v2{color:var(--hi);background:var(--hi-bg);font-weight:600}.v1{color:var(--mid);background:var(--mid-bg)}
.v0{color:var(--lo);background:var(--lo-bg)}
.mute{color:var(--mute);font-size:12.5px}
.terms{color:var(--mute);font-size:12.5px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
details summary{cursor:pointer;color:var(--mute);font-size:12.5px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 10px}
.bar button{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:4px 12px;cursor:pointer;font:inherit}
.bar button.on{border-color:var(--accent);color:var(--accent);font-weight:600}
.bar input{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:6px;padding:5px 10px;font:inherit;min-width:220px}
.note{color:var(--mute);font-size:12.5px;margin-top:8px}
</style></head><body><main>
<h1>__TITLE__</h1>
<div class="meta">__META__</div>
<div class="tiles">__TILES__</div>
<h2>청구항</h2><div class="card claim">__CLAIM__</div>
<h2>구성 분해</h2>
<div class="card"><b>요지</b> __GIST__<br><b>검색 우선 특징</b> __POINT__</div>
<div class="scroll" style="margin-top:10px"><table>
<tr><th>구성</th><th>구분</th><th>청구항 문구 / 풀이</th><th>검색어</th></tr>__ELEMENTS__</table></div>
<div class="note">CPC 후보: __CPC__</div>
<h2>검색식 로그</h2>
<div class="scroll"><table>
<tr><th>ID</th><th>DB</th><th>수준</th><th class="num">히트</th><th>상태</th><th>검색식</th><th>의도 / 수정 내용</th></tr>__LOG__</table></div>
<div class="note">상태가 "사용"인 검색식의 결과만 후보에 들어갑니다. 0건·과다·오류·소량인 검색식은 LLM이 히트 수를 보고 다시 쓴 것이 아래 줄(r1, r2)에 이어집니다.</div>
<h2>후보 문헌</h2>
<div class="bar">
<button data-g="all" class="on">전체</button><button data-g="high">상</button><button data-g="mid">중</button><button data-g="low">하</button>
<button data-k="all" class="on">특허+논문</button><button data-k="patent">특허</button><button data-k="paper">논문</button>
<input id="q" placeholder="제목·출원인·번호 필터">
<span class="mute" id="shown"></span></div>
<div class="scroll"><table id="cands">
<tr><th>#</th><th>점수</th><th>구성별</th><th>문헌</th><th>제목 / 판단 근거</th><th>공개일</th><th>검색식</th></tr>__CANDS__</table></div>
<div class="note">점수는 제목·초록만 보고 매긴 1차 선별값입니다(구성별 0=언급 없음, 1=부분 관련, 2=명시적 대응). 순위와 등급은 ★ 표시된 차별점 구성이 먼저 결정합니다: ★가 2점이어야 "상", 1점 이상이어야 "중"입니다. "수단 가설"은 청구항이 효과로만 적은 구성을 달성하는 통상적 수단을 LLM이 추정한 것으로, 그 수단이 같은 상황에서 쓰이면 대응으로 봅니다. 초록에 없는 내용은 반영되지 않으므로 "하"라고 해서 무관하다는 뜻은 아니며, 최종 판단은 전문을 읽고 해야 합니다.</div>
</main>
<script>
const rows=[...document.querySelectorAll('#cands tr[data-g]')];let g='all',k='all';
function apply(){const q=document.getElementById('q').value.toLowerCase();let n=0;
rows.forEach(r=>{const ok=(g==='all'||r.dataset.g===g)&&(k==='all'||r.dataset.k===k)&&(!q||r.textContent.toLowerCase().includes(q));
r.style.display=ok?'':'none';if(ok)n++});document.getElementById('shown').textContent=n+' / '+rows.length+'건';}
document.querySelectorAll('.bar button').forEach(b=>b.onclick=()=>{const key=b.dataset.g?'g':'k';
document.querySelectorAll('.bar button[data-'+key+']').forEach(x=>x.classList.remove('on'));b.classList.add('on');
if(key==='g')g=b.dataset.g;else k=b.dataset.k;apply()});
document.getElementById('q').oninput=apply;apply();
</script></body></html>
"""


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _date(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d and len(d) >= 8 else esc(d)


def _tile(value, label) -> str:
    return f'<div class="tile"><b>{esc(value)}</b><span>{esc(label)}</span></div>'


def write_report(path: Path, claim: str, decomp: dict, log: list, candidates: list, screening: list, settings) -> None:
    screen_by_key = {r["key"]: r for r in screening}
    element_ids = [e["id"] for e in decomp.get("elements", [])]
    key_ids = [e["id"] for e in decomp.get("elements", []) if e.get("role") == "key"] or [
        e["id"] for e in decomp.get("elements", []) if e.get("role", "core") == "core"
    ]
    grade_rank = {"high": 0, "mid": 1, "low": 2, "unscreened": 3}

    elements_html = ""
    for e in decomp.get("elements", []):
        terms = "<br>".join(
            f'<b>{esc(c.get("name_ko", ""))}</b> {esc(", ".join(c.get("terms_en", [])))}' for c in e.get("concepts", [])
        )
        means = "".join(
            f'<div><b>수단 가설 · {esc(i.get("name_ko", ""))}</b> {esc(", ".join(i.get("terms_en", [])))}</div>'
            for i in e.get("implementations", [])
        )
        if means:
            terms += f'<div style="margin-top:6px">{means}</div>'
        role = {"key": "차별점", "core": "핵심", "context": "전제"}.get(e.get("role", "core"), "핵심")
        if e["id"] in key_ids:
            role = f'<span class="badge g-high">{role}</span>'
        elements_html += (
            f'<tr><td><b>{esc(e["id"])}</b></td><td>{role}</td>'
            f'<td>{esc(e.get("text_ko", ""))}<div class="mute">{esc(e.get("summary_ko", ""))}</div></td>'
            f'<td class="terms">{terms}</td></tr>'
        )

    log_html = ""
    for e in log:
        note = esc(e.get("change_ko") or e.get("rationale_ko", ""))
        if e.get("error"):
            note += f'<div class="s-error">{esc(e["error"])}</div>'
        hits = "" if e.get("hits") is None else f'{e["hits"]:,}'
        log_html += (
            f'<tr><td><b>{esc(e["id"])}</b></td><td>{"OPS" if e.get("source") == "ops" else "OpenAlex"}</td>'
            f'<td>{esc(e.get("level", ""))}</td><td class="num">{hits}</td>'
            f'<td class="s-{esc(e["status"])}">{STATUS_LABEL.get(e["status"], esc(e["status"]))}</td>'
            f'<td><code>{esc(e["cql"])}</code></td><td class="mute">{note}</td></tr>'
        )

    def sort_key(c):
        r = screen_by_key.get(c["key"]) or {}
        score = r["score"] if r.get("score") is not None else -1
        key_score = r.get("key_score")
        if key_score is None:
            key_score = sum((r.get("scores") or {}).get(k, 0) for k in key_ids)
        return (grade_rank.get(r.get("grade"), 3), -key_score, -score, -c.get("prescore", 0))

    cands_html, shown = "", 0
    for c in sorted(candidates, key=sort_key):
        r = screen_by_key.get(c["key"])
        if r is None:
            continue  # 스크리닝 상한 밖
        shown += 1
        grade = r["grade"]
        score = "–" if r.get("score") is None else r["score"]
        per_el = "".join(
            f'<span class="el v{r["scores"].get(k, 0)}">{esc(k)}{"★" if k in key_ids else ""} {r["scores"].get(k, 0)}</span>'
            for k in element_ids
        ) if r.get("scores") else ""
        if c["kind"] == "patent":
            link = f'https://worldwide.espacenet.com/patent/search?q=pn%3D{esc(c["pn"])}'
            ident = f'<a href="{link}" target="_blank" rel="noopener">{esc(c["pn"])}</a>'
            extra = len(c.get("members", [])) - 1
            if extra > 0:
                ident += f' <span class="mute">+{extra}</span>'
        else:
            ident = f'<a href="{esc(c.get("url", ""))}" target="_blank" rel="noopener">논문</a>'
        who = esc("; ".join(c.get("applicants", [])))
        abstract = esc(c.get("abstract", "")) or "(초록 없음)"
        cands_html += (
            f'<tr data-g="{grade}" data-k="{c["kind"]}"><td class="num">{shown}</td>'
            f'<td><span class="badge g-{grade}">{GRADE_LABEL[grade]} {score}</span></td>'
            f'<td style="white-space:nowrap">{per_el}</td>'
            f'<td>{ident}<div class="mute">{who}</div></td>'
            f'<td><b>{esc(c.get("title", ""))}</b><div>{esc(r.get("reason_ko", ""))}</div>'
            f'<details><summary>초록</summary>{abstract}'
            f'<div class="mute">{esc(", ".join(c.get("cpc", [])[:8]))}</div></details></td>'
            f'<td style="white-space:nowrap">{_date(c.get("pub_date", ""))}</td>'
            f'<td class="mute">{esc(" ".join(c.get("hit_queries", [])))}</td></tr>'
        )
    if not cands_html:
        cands_html = '<tr><td colspan="7" class="mute">후보가 없습니다. 검색식 로그를 확인하세요.</td></tr>'

    n_pat = sum(1 for c in candidates if c["kind"] == "patent")
    n_paper = sum(1 for c in candidates if c["kind"] == "paper")
    used = sum(1 for e in log if e["status"] in ("ok", "few"))
    grades = {g: sum(1 for r in screening if r["grade"] == g) for g in ("high", "mid")}
    tiles = (
        _tile(f"{used} / {len(log)}", "사용된 검색식 / 실행")
        + _tile(n_pat, "특허 후보(패밀리)")
        + _tile(n_paper, "논문 후보")
        + _tile(len(screening), "스크리닝한 문헌")
        + _tile(grades["high"], "관련도 상")
        + _tile(grades["mid"], "관련도 중")
    )
    meta = f'{datetime.now():%Y-%m-%d %H:%M} 작성'
    if settings.before:
        meta += f' · 기준일 {_date(settings.before)} 이전 공개 문헌'
    else:
        meta += " · 기준일 미지정(공개일 제한 없음)"
    cpc = ", ".join(
        f'{esc(c.get("symbol", ""))} <span class="mute">{esc(c.get("desc_ko", ""))}</span>' for c in decomp.get("cpc", [])
    )
    title = f'선행기술 검색 — {decomp.get("title_en", "")}'
    page = (
        TEMPLATE.replace("__TITLE__", esc(title))
        .replace("__META__", meta)
        .replace("__TILES__", tiles)
        .replace("__CLAIM__", esc(claim))
        .replace("__GIST__", esc(decomp.get("gist_ko", "")))
        .replace("__POINT__", esc(decomp.get("distinguishing_point_ko", "")))
        .replace("__ELEMENTS__", elements_html)
        .replace("__CPC__", cpc)
        .replace("__LOG__", log_html)
        .replace("__CANDS__", cands_html)
    )
    path.write_text(page, encoding="utf-8")
