from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path

from . import llm, runs
from .config import RUNS_DIR, Cache, env, load_env, load_settings
from .llm import LLMError, ask_json, claude_path
from .openalex import OpenAlexError, search_works
from .ops import OpsClient, OpsError
from .pipeline import STAGES, Settings, clear_from, run, say, set_log


def make_ops(cache, max_wait: float = 120.0) -> OpsClient | None:
    key, secret = env("EPO_OPS_KEY"), env("EPO_OPS_SECRET")
    return OpsClient(key, secret, cache=cache, max_wait=max_wait) if key and secret else None


def check() -> int:
    """LLM, EPO OPS, OpenAlex 연결을 각각 한 번씩 시험한다."""
    ok = True
    try:
        print(f"claude CLI : {claude_path()}")
        answer = ask_json('Return {"ok": true}', model="haiku", timeout=120)
        print(f"  응답 {answer}")
    except LLMError as e:
        ok = False
        print(f"  실패: {e}")
    ops = make_ops(None)
    if ops is None:
        ok = False
        print("EPO OPS    : .env 에 EPO_OPS_KEY / EPO_OPS_SECRET 가 없습니다")
    else:
        try:
            ops.token()
            n = ops.count('ta="intra prediction" and ta=interpolat*')
            print(f"EPO OPS    : 인증 성공, 시험 검색 {n:,}건")
        except OpsError as e:
            ok = False
            print(f"EPO OPS    : 실패: {e}")
    try:
        result = search_works('"intra prediction" AND "interpolation filter"', per_page=1, api_key=env("OPENALEX_API_KEY"))
        key_note = "키 사용" if env("OPENALEX_API_KEY") else "키 없음(공용 한도)"
        print(f'OpenAlex   : 시험 검색 {result["total"]:,}건 ({key_note})')
    except OpenAlexError as e:
        ok = False
        print(f"OpenAlex   : 실패: {e}")
    return 0 if ok else 1


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    load_env()
    p = argparse.ArgumentParser(prog="patsearch", description="청구항으로 선행기술 후보를 찾는다.")
    p.add_argument("claim", nargs="?", help="청구항이 들어 있는 텍스트 파일")
    p.add_argument("--check", action="store_true", help="LLM·EPO OPS·OpenAlex 연결만 시험")
    p.add_argument("--before", default="", help="기준일 YYYYMMDD (이 날짜 이전 공개 문헌만). 보통 우선일")
    p.add_argument("--run", default="", help="이어서 쓸 실행 폴더 (runs\\... ). 있는 단계 파일은 재사용")
    p.add_argument("--from", dest="from_stage", choices=STAGES, help="이 단계부터 다시 실행 (--run 과 함께)")
    p.add_argument("--stop-after", choices=STAGES, default="report", help="이 단계까지만 실행")
    p.add_argument("--cli", default="", choices=["", *llm.CLIS], help="LLM 으로 쓸 CLI (기본: claude)")
    p.add_argument("--model", default=None, help="구성 분해·검색식용 모델 (기본: CLI 기본 모델)")
    p.add_argument("--screen-model", default=None, help="초록 스크리닝용 모델 (기본: claude 는 haiku)")
    p.add_argument("--min-hits", type=int, default=20, help="이보다 적게 걸린 검색식은 넓힌 검색식을 하나 더 만든다")
    p.add_argument("--max-hits", type=int, default=400, help="검색식당 허용 히트 수 상한")
    p.add_argument("--per-query", type=int, default=100, help="검색식당 가져올 문헌 수")
    p.add_argument("--max-screen", type=int, default=0, help="스크리닝할 특허 패밀리 수 상한 (기본 300)")
    p.add_argument("--rounds", type=int, default=2, help="검색식 수정 반복 횟수")
    p.add_argument("--max-wait", type=int, default=120, help="EPO 요청 제한에 걸렸을 때 자동으로 기다릴 최대 시간(초)")
    p.add_argument("--no-openalex", action="store_true")
    p.add_argument("--no-cache", action="store_true", help="LLM·검색 응답 캐시를 읽지 않음")
    p.add_argument("--no-open", action="store_true", help="끝난 뒤 리포트를 브라우저로 열지 않음")
    args = p.parse_args()

    if args.check:
        return check()
    if args.before and not re.fullmatch(r"\d{8}", args.before):
        p.error("--before 는 YYYYMMDD 형식이어야 합니다")

    if args.run:
        run_dir = Path(args.run)
        if not run_dir.is_dir():
            p.error(f"실행 폴더가 없습니다: {run_dir}")
        claim_path = Path(args.claim) if args.claim else run_dir / "00_claim.txt"
    else:
        if not args.claim:
            p.error("청구항 파일을 지정하세요 (예: python -m patsearch claims\\claim1.txt)")
        claim_path = Path(args.claim)
        run_dir = RUNS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{claim_path.stem}"
    if not claim_path.is_file():
        p.error(f"청구항 파일이 없습니다: {claim_path}")
    claim = claim_path.read_text(encoding="utf-8-sig").strip()
    if not claim:
        p.error("청구항 파일이 비어 있습니다")

    # 이어서 실행할 때 지정하지 않은 값은 처음 실행 때의 설정(meta.json)을 따른다
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = runs.read_meta(run_dir)
    # 그것도 없으면 설정 화면에서 저장한 기본값(settings.json), 마지막으로 환경변수 순이다.
    saved = load_settings()
    cli = args.cli or meta.get("cli") or env("PATSEARCH_CLI") or saved["cli"]
    try:
        llm.set_cli(cli)
    except LLMError as e:
        p.error(str(e))
    saved_models = saved["models"].get(cli, {})
    before = args.before or meta.get("before", "")
    model = args.model if args.model is not None else meta.get(
        "model", env("PATSEARCH_MODEL") or saved_models.get("model", "")
    )
    screen_model = args.screen_model if args.screen_model is not None else meta.get(
        "screen_model",
        env("PATSEARCH_SCREEN_MODEL") or saved_models.get("screen_model", llm.CLIS[cli]["screen_default"]),
    )
    max_screen = args.max_screen or meta.get("max_screen") or saved["max_screen"]
    meta.update(cli=cli, model=model, screen_model=screen_model, before=before, max_screen=max_screen)
    meta.setdefault("created", f"{datetime.now():%Y-%m-%d %H:%M:%S}")
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    cache = Cache(enabled=not args.no_cache)
    settings = Settings(
        model=model, screen_model=screen_model, before=before,
        min_hits=args.min_hits, max_hits=args.max_hits, per_query=args.per_query,
        max_screen=max_screen, rounds=args.rounds,
        use_openalex=not args.no_openalex, openalex_key=env("OPENALEX_API_KEY"),
    )
    if args.from_stage:
        clear_from(run_dir, args.from_stage)
    set_log(run_dir / "run.log")
    runs.write_status(run_dir, "running")
    say(f"── {datetime.now():%Y-%m-%d %H:%M:%S} 실행 시작 ({llm.CLIS[cli]['label']}, 분석 모델 {model or '기본값'}, "
        f"스크리닝 모델 {screen_model or '기본값'}, 기준일 {before or '없음'})")
    say(f"실행 폴더: {run_dir}")
    try:
        report = run(claim, run_dir, settings, cache, make_ops(cache, args.max_wait), stop_after=args.stop_after)
    except LLMError as e:
        hint = ""
        if "model" in str(e).lower():
            hint = " — CLI 가 이 모델을 쓸 수 없습니다. 모델을 '기본값' 대신 직접 골라 다시 실행해 보세요."
        say(f"LLM 오류: {e}{hint}")
        runs.write_status(run_dir, "error", message=f"LLM 오류: {e}{hint}")
        return 1
    except OpsError as e:
        say(f"중단: {e}")
        say("이미 받은 검색 결과는 캐시에 남아 있습니다. 잠시 뒤 이어서 실행하세요.")
        say(f"  python -m patsearch --run {run_dir}")
        runs.write_status(run_dir, "error", message=str(e))
        return 2
    except KeyboardInterrupt:
        say("사용자가 중지했습니다.")
        runs.write_status(run_dir, "stopped", message="사용자가 중지")
        return 130
    except Exception as e:  # 예상하지 못한 오류도 기록에 남긴다
        say("예상하지 못한 오류:\n" + traceback.format_exc())
        runs.write_status(run_dir, "error", message=f"{type(e).__name__}: {e}")
        return 1
    if report:
        runs.summarize(run_dir)
        runs.write_status(run_dir, "done")
    else:
        runs.write_status(run_dir, "stopped", message=f"{args.stop_after} 단계까지만 실행")
    if report and not args.no_open:
        webbrowser.open(report.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
