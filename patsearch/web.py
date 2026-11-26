"""로컬 웹 화면. `python -m patsearch.web` 으로 띄우고 브라우저에서 쓴다.

이 PC 안(127.0.0.1)에서만 접속된다. 검색은 `python -m patsearch --run <폴더>` 를 자식 프로세스로 띄워
수행하고, 화면은 실행 폴더의 파일(status.json, run.log, report.html)을 읽어 보여 준다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import claims, compare, llm, runs
from .config import APP_NAME, APP_TAGLINE, ROOT, RUNS_DIR, env, load_env, load_settings, save_env, save_settings
from .openalex import OpenAlexError, search_works
from .ops import OpsClient, OpsError

UI_FILE = Path(__file__).with_name("ui.html")
SERVED_FILES = {
    "report.html": "text/html; charset=utf-8",
    "01_elements.json": "application/json; charset=utf-8",
    "02_queries.json": "application/json; charset=utf-8",
    "03_search_log.json": "application/json; charset=utf-8",
}
STATIC_DIR = Path(__file__).with_name("static")
STATIC_FILES = {
    "favicon.ico": "image/x-icon",
    "favicon.svg": "image/svg+xml",
    "emblem.svg": "image/svg+xml",
    "apple-touch-icon.png": "image/png",
}
# 대비 보고서 단계가 생기면 여기에 그 결과 파일(compare.html 등)을 추가한다.
MODEL_NAME = re.compile(r"^[A-Za-z0-9._:\-\[\]/]{0,80}$")
KEY_NAMES = {"EPO_OPS_KEY": "EPO Consumer Key", "EPO_OPS_SECRET": "EPO Consumer Secret", "OPENALEX_API_KEY": "OpenAlex API 키"}
KEY_VALUE = re.compile(r"^[A-Za-z0-9._\-+/=~]{8,300}$")
PROCS: dict[str, subprocess.Popen] = {}
LOCK = threading.Lock()


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# --- 실행 폴더 ---------------------------------------------------------------
def run_dir_of(run_id: str) -> Path:
    if not runs.RUN_ID.match(run_id or ""):
        raise ApiError(404, "기록을 찾을 수 없습니다")
    path = RUNS_DIR / run_id
    if path.parent.resolve() != RUNS_DIR.resolve() or not path.is_dir():
        raise ApiError(404, "기록을 찾을 수 없습니다")
    return path


def _claim(run_dir: Path) -> str:
    try:
        return (run_dir / "00_claim.txt").read_text(encoding="utf-8-sig")
    except OSError:
        return ""


def _tail(path: Path, lines: int = 400) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _started_here(run_id: str) -> bool:
    proc = PROCS.get(run_id)
    return proc is not None and proc.poll() is None


def run_item(run_dir: Path) -> dict:
    meta = runs.read_meta(run_dir)
    state = runs.run_state(run_dir)
    m = re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", run_dir.name)
    created = meta.get("created") or (f"{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}:{m[6]}" if m else "")
    if meta.get("mode") == "compare" and not (run_dir / "status.json").exists():
        state = {"state": "idle", "message": ""}  # 검색을 돌린 적 없는 구성대비 전용 기록
    return {
        "id": run_dir.name,
        "created": created,
        "snippet": " ".join(_claim(run_dir).split())[:160],
        "state": state["state"],
        "message": state["message"],
        "progress": runs.stage_progress(run_dir),
        "summary": runs.read_summary(run_dir),
        "meta": meta,
        "can_stop": _started_here(run_dir.name),
        "has_report": (run_dir / "report.html").exists(),
    }


def list_runs() -> list:
    if not RUNS_DIR.is_dir():
        return []
    dirs = [d for d in RUNS_DIR.iterdir() if d.is_dir() and runs.RUN_ID.match(d.name)]
    return [run_item(d) for d in sorted(dirs, key=lambda d: d.name, reverse=True)]


def run_detail(run_id: str) -> dict:
    run_dir = run_dir_of(run_id)
    item = run_item(run_dir)
    item["claim"] = _claim(run_dir)
    log = _tail(run_dir / "run.log")
    err = _tail(run_dir / "run.err.log", 60)
    item["log"] = log + (f"\n── 오류 출력 ──\n{err}" if err.strip() else "")
    return item


def running_id() -> str:
    for item in list_runs():
        if item["state"] == "running":
            return item["id"]
    return ""


# --- 실행·중지·삭제 ----------------------------------------------------------
def _spawn(run_dir: Path) -> None:
    """검색 프로세스를 띄운다. 설정은 자식이 실행 폴더의 meta.json 에서 읽는다."""
    cmd = [sys.executable, "-m", "patsearch", "--run", str(run_dir), "--no-open", "--max-wait", "1500"]
    child_env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    err = open(run_dir / "run.err.log", "ab")
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
        creationflags=flags,
    )
    err.close()
    PROCS[run_dir.name] = proc
    runs.write_status(run_dir, "running", pid=proc.pid)

    def reap():
        code = proc.wait()
        # 정상 종료라면 자식이 상태를 직접 써 둔다. 그렇지 못하고 죽은 경우만 여기서 기록한다.
        status = runs._read_json(run_dir / "status.json") or {}
        if run_dir.is_dir() and status.get("state") == "running":
            runs.write_status(run_dir, "error", message=f"검색 프로세스가 비정상 종료했습니다 (exit {code})", pid=0)

    threading.Thread(target=reap, daemon=True).start()


def _clean_model(value) -> str:
    value = str(value or "").strip()
    if not MODEL_NAME.match(value):
        raise ApiError(400, "모델 이름에 쓸 수 없는 문자가 있습니다")
    return value


def _clean_settings(raw: dict) -> dict:
    """화면에서 온 설정을 검증해 저장 가능한 형태로 만든다."""
    cli = str(raw.get("cli") or "claude")
    if cli not in llm.CLIS:
        raise ApiError(400, "지원하지 않는 CLI 입니다")
    try:
        max_screen = max(10, min(2000, int(raw.get("max_screen") or 300)))
    except (TypeError, ValueError):
        raise ApiError(400, "스크리닝할 특허 수는 숫자여야 합니다") from None
    models = {}
    for cid in llm.CLIS:
        entry = (raw.get("models") or {}).get(cid) or {}
        models[cid] = {"model": _clean_model(entry.get("model")), "screen_model": _clean_model(entry.get("screen_model"))}
    return {"cli": cli, "models": models, "max_screen": max_screen}


def save_settings_request(body: dict) -> dict:
    if isinstance(body.get("settings"), dict):
        save_settings(_clean_settings(body["settings"]))
    updates = {}
    for name, value in (body.get("keys") or {}).items():
        if name not in KEY_NAMES:
            raise ApiError(400, "알 수 없는 키 이름입니다")
        value = str(value or "").strip()
        if not value:
            continue  # 빈 칸은 '바꾸지 않음'
        if not KEY_VALUE.match(value):
            raise ApiError(400, f"{KEY_NAMES[name]}: 키에 쓸 수 없는 문자가 있거나 길이가 맞지 않습니다")
        updates[name] = value
    for name in body.get("clear") or []:
        if name not in KEY_NAMES:
            raise ApiError(400, "알 수 없는 키 이름입니다")
        updates[name] = ""
    if updates:
        save_env(updates)
    return app_state()


def check_connections() -> dict:
    """설정된 CLI·모델과 두 검색 서비스에 각각 한 번씩 실제로 요청해 본다."""
    settings = load_settings()
    cli = settings["cli"]
    chosen = settings["models"].get(cli, {})
    results = []
    label = llm.CLIS[cli]["label"]
    seen = set()
    for role, model in (("분석 모델", chosen.get("model", "")), ("스크리닝 모델", chosen.get("screen_model", ""))):
        if model in seen:
            continue
        seen.add(model)
        name = f"{label} · {role} ({model or '기본값'})"
        try:
            llm.set_cli(cli)
            llm.ask_json('Return exactly this JSON: {"ok": true}', model=model, timeout=120, retries=0)
            results.append({"name": name, "ok": True, "detail": "응답 정상"})
        except llm.LLMError as e:
            results.append({"name": name, "ok": False, "detail": str(e)[:300]})
    key, secret = env("EPO_OPS_KEY"), env("EPO_OPS_SECRET")
    if not (key and secret):
        results.append({"name": "EPO OPS", "ok": False, "detail": "키가 없습니다. 위에서 Consumer Key 와 Secret 을 입력하세요."})
    else:
        try:
            ops = OpsClient(key, secret, max_wait=5)
            ops.token(force=True)
            n = ops.count('ta="intra prediction" and ta=interpolat*')
            results.append({"name": "EPO OPS", "ok": True, "detail": f"인증 성공, 시험 검색 {n:,}건"})
        except OpsError as e:
            results.append({"name": "EPO OPS", "ok": False, "detail": str(e)[:300]})
    try:
        r = search_works('"intra prediction" AND "interpolation filter"', per_page=1, api_key=env("OPENALEX_API_KEY"))
        note = "키 사용" if env("OPENALEX_API_KEY") else "키 없음 — 공용 한도로 동작"
        results.append({"name": "OpenAlex", "ok": True, "detail": f'시험 검색 {r["total"]:,}건 ({note})'})
    except OpenAlexError as e:
        results.append({"name": "OpenAlex", "ok": False, "detail": str(e)[:300]})
    return {"results": results}


def create_run(body: dict) -> dict:
    claim = str(body.get("claim") or "").strip()
    if len(claim) < 20:
        raise ApiError(400, "청구항을 입력하세요")
    if len(claim) > 30000:
        raise ApiError(400, "청구항이 너무 깁니다 (30,000자 이하)")
    before = str(body.get("before") or "").replace("-", "").strip()
    if before and not re.fullmatch(r"\d{8}", before):
        raise ApiError(400, "기준일 형식이 잘못되었습니다")
    # CLI·모델·스크리닝 수는 설정 화면에서 저장한 값을 쓴다
    settings = load_settings()
    cli = settings["cli"]
    if not shutil.which(llm.CLIS[cli]["exe"]):
        raise ApiError(400, f'{llm.CLIS[cli]["label"]} CLI 가 설치되어 있지 않습니다. 설정에서 다른 CLI 를 고르세요.')
    chosen = settings["models"].get(cli, {})
    meta = {
        "cli": cli,
        "model": chosen.get("model", ""),
        "screen_model": chosen.get("screen_model", ""),
        "before": before,
        "max_screen": settings["max_screen"],
        "created": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        "source": "ui",
    }
    compare_only = body.get("mode") == "compare"  # 검색 없이 청구항만 기록해 두고 구성대비에 쓴다
    dependents = ""
    if compare_only:
        meta["mode"] = "compare"
        # 종속항까지 한꺼번에 붙여 넣었으면 독립항과 종속항으로 나눠 따로 적어 둔다
        try:
            parts = claims.split_set(claim)
        except claims.ClaimError as e:
            raise ApiError(400, str(e)) from None
        meta["no_base"] = parts["base_no"] is None
        if meta["no_base"]:
            # 독립항 없이 종속항만 넣었다. 넣은 글은 그대로 보여 주고, 대비는 종속항만으로 한다(compare.run_alone)
            dependents = parts["dependents"]
        else:
            claim = parts["base"]
            if len(claim) < 20:
                raise ApiError(400, "독립항이 너무 짧습니다. 청구항을 다시 확인하세요")
            if parts["deps"]:
                dependents = parts["dependents"]
    with LOCK:
        if not compare_only and running_id():
            raise ApiError(409, "이미 실행 중인 검색이 있습니다. EPO 요청 제한 때문에 한 번에 하나만 실행합니다.")
        run_dir = RUNS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-ui"
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "00_claim.txt").write_text(claim, encoding="utf-8")
        (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        if dependents:
            (run_dir / "compare").mkdir()
            (run_dir / "compare" / "dependents.txt").write_text(dependents + "\n", encoding="utf-8")
        if not compare_only:
            _spawn(run_dir)
    return {"id": run_dir.name}


def resume_run(run_id: str) -> dict:
    run_dir = run_dir_of(run_id)
    with LOCK:
        busy = running_id()
        if busy:
            raise ApiError(409, "이미 실행 중인 검색이 있습니다")
        if not (run_dir / "00_claim.txt").exists():
            raise ApiError(400, "청구항 파일이 없어 이어서 실행할 수 없습니다")
        _spawn(run_dir)
    return {"id": run_id}


def _kill_tree(proc: subprocess.Popen) -> None:
    """프로세스와 그 아래에서 돌던 LLM CLI 까지 한꺼번에 끝낸다."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                       creationflags=0x08000000)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def stop_run(run_id: str) -> dict:
    run_dir = run_dir_of(run_id)
    proc = PROCS.get(run_id)
    if proc is None or proc.poll() is not None:
        raise ApiError(409, "이 화면에서 시작한 실행만 중지할 수 있습니다")
    _kill_tree(proc)
    # 중지 직전에 스스로 끝난 경우(완료·오류)에는 그 상태를 덮어쓰지 않는다
    status = runs._read_json(run_dir / "status.json") or {}
    if status.get("state", "running") == "running":
        runs.write_status(run_dir, "stopped", message="사용자가 중지", pid=0)
        with open(run_dir / "run.log", "a", encoding="utf-8") as f:
            f.write("사용자가 중지했습니다.\n")
    return {"id": run_id}


def delete_run(run_id: str) -> dict:
    run_dir = run_dir_of(run_id)
    if runs.run_state(run_dir)["state"] == "running":
        raise ApiError(409, "실행 중인 검색은 지울 수 없습니다. 먼저 중지하세요.")
    if _compare_running(run_id):
        raise ApiError(409, "구성대비가 실행 중입니다. 끝난 뒤에 지우세요.")
    shutil.rmtree(run_dir)
    PROCS.pop(run_id, None)
    return {"deleted": 1}


def delete_all() -> dict:
    deleted = 0
    for item in list_runs():
        if item["state"] == "running":
            continue
        shutil.rmtree(RUNS_DIR / item["id"], ignore_errors=True)
        PROCS.pop(item["id"], None)
        deleted += 1
    return {"deleted": deleted}


# --- 구성대비 ------------------------------------------------------------------
MAX_PDF_BYTES = 40 * 1024 * 1024


def _compare_dir(run_id: str) -> Path:
    return run_dir_of(run_id) / "compare"


def _no_base(run_dir: Path) -> bool:
    """독립항 없이 종속항만 넣어 만든 기록인가."""
    meta = runs.read_meta(run_dir)
    if "no_base" in meta:
        return bool(meta["no_base"])
    if meta.get("mode") != "compare":
        return False
    try:  # 표시가 없는 예전 기록: 넣어 둔 글의 가장 앞 항이 종속항인지로 가린다
        return claims.split_set(_claim(run_dir))["base_no"] is None
    except claims.ClaimError:
        return False


def _base_no(run_dir: Path):
    """종속항이 기대는 청구항 번호. 종속항만 넣은 기록이면 None."""
    return None if _no_base(run_dir) else compare._claim_no(_claim(run_dir))


def _compare_names(cdir: Path) -> dict:
    return runs._read_json(cdir / "names.json") or {}


def _compare_running(run_id: str) -> bool:
    proc = PROCS.get(f"{run_id}:compare")
    return proc is not None and proc.poll() is None


def compare_state(run_id: str) -> dict:
    cdir = _compare_dir(run_id)
    names = _compare_names(cdir)
    docs = [
        {"id": f.stem, "name": names.get(f.stem, f.stem), "size": f.stat().st_size}
        for f in sorted((cdir / "docs").glob("*.pdf"), key=lambda f: int(f.stem[1:]) if f.stem[1:].isdigit() else 0)
    ] if (cdir / "docs").is_dir() else []
    state, message = "idle", ""
    if (cdir / "status.json").exists():
        st = runs.run_state(cdir)
        state, message = st["state"], st["message"]
    result = runs._read_json(cdir / "result.json") if state != "running" else None
    try:  # 화면에서 넣어 둔 종속항. 문헌을 바꿔 결과가 지워져도 이 글은 남는다
        dependents = (cdir / "dependents.txt").read_text(encoding="utf-8-sig").strip()
    except OSError:
        dependents = ""
    return {"docs": docs, "state": state, "message": message, "log": _tail(cdir / "run.log", 200), "result": result,
            "dependents": dependents, "alone": _no_base(cdir.parent)}


def compare_upload(run_id: str, filename: str, data: bytes) -> dict:
    if _compare_running(run_id):
        raise ApiError(409, "구성대비 실행 중에는 문헌을 바꿀 수 없습니다")
    if not data.startswith(b"%PDF"):
        raise ApiError(400, f"{filename}: PDF 파일이 아닙니다")
    cdir = _compare_dir(run_id)
    docs_dir = cdir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    used = [int(f.stem[1:]) for f in docs_dir.glob("d*.pdf") if f.stem[1:].isdigit()]
    if len(used) >= 12:
        raise ApiError(400, "인용발명은 12건까지 넣을 수 있습니다")
    doc_id = f"d{max(used, default=0) + 1}"
    path = docs_dir / f"{doc_id}.pdf"
    path.write_bytes(data)
    try:
        doc = compare.load_document(path, doc_id=doc_id, name=compare._display_name(Path(filename), ""))
    except compare.CompareError as e:
        path.unlink(missing_ok=True)
        raise ApiError(400, str(e)) from None
    names = _compare_names(cdir)
    names[doc_id] = doc["name"]
    (cdir / "names.json").write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
    _compare_invalidate(cdir)
    return compare_state(run_id)


def _compare_invalidate(cdir: Path) -> None:
    """문헌이 바뀌면 이전 결과는 더 이상 맞지 않으므로 지운다."""
    for name in ("result.json", "result.txt", "judgments.json", "docs.json", "elements.json", "status.json", *compare.DEP_FILES):
        (cdir / name).unlink(missing_ok=True)


def compare_remove(run_id: str, doc_id: str) -> dict:
    if _compare_running(run_id):
        raise ApiError(409, "구성대비 실행 중에는 문헌을 바꿀 수 없습니다")
    if not re.fullmatch(r"d\d{1,3}", doc_id or ""):
        raise ApiError(404, "문헌을 찾을 수 없습니다")
    cdir = _compare_dir(run_id)
    path = cdir / "docs" / f"{doc_id}.pdf"
    if not path.is_file():
        raise ApiError(404, "문헌을 찾을 수 없습니다")
    path.unlink()
    names = _compare_names(cdir)
    names.pop(doc_id, None)
    (cdir / "names.json").write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
    _compare_invalidate(cdir)
    return compare_state(run_id)


def compare_start(run_id: str) -> dict:
    """청구항 구성대비를 처음부터 실행한다. 넣어 둔 종속항이 있으면 이어서 함께 대비한다."""
    run_dir = run_dir_of(run_id)
    cdir = run_dir / "compare"
    if not any((cdir / "docs").glob("*.pdf")):
        raise ApiError(400, "인용발명 PDF 를 먼저 넣으세요")
    alone = _no_base(run_dir)
    if alone and not (cdir / "dependents.txt").is_file():  # 예전 기록: 넣어 둔 글에서 종속항을 나눠 적어 둔다
        try:
            text = claims.split_set(_claim(run_dir))["dependents"]
        except claims.ClaimError as e:
            raise ApiError(400, str(e)) from None
        (cdir / "dependents.txt").write_text(text + "\n", encoding="utf-8")
    _compare_spawn(run_id, ["--alone"] if alone else [])
    return compare_state(run_id)


def compare_dependents(run_id: str, body: dict) -> dict:
    """종속항을 저장하고, 끝나 있는 청구항 구성대비 결과 위에 종속항만 대비한다. 비워서 보내면 종속항 결과를 지운다."""
    run_dir = run_dir_of(run_id)
    cdir = run_dir / "compare"
    text = str(body.get("text") or "").strip()
    if _compare_running(run_id):
        raise ApiError(409, "이미 구성대비를 실행 중입니다")
    if not (cdir / "judgments.json").exists():
        raise ApiError(400, "먼저 청구항 구성대비를 실행하세요")
    if not text and _no_base(run_dir):
        raise ApiError(400, "종속항만 대비하는 기록입니다. 대비할 종속항을 넣어 주세요")
    if not text:
        for name in ("dependents.txt", *compare.DEP_FILES):
            (cdir / name).unlink(missing_ok=True)
        primary, extra = compare.saved_order(cdir)
        compare.recompose(cdir, _claim(run_dir), primary=primary, extra=extra)
        return compare_state(run_id)
    # 인용 관계는 프로그램이 읽으므로, 잘못된 입력은 LLM 을 부르기 전에 여기서 알려 준다
    try:
        deps, notes = claims.read_dependents(text, _base_no(run_dir))
    except claims.ClaimError as e:
        raise ApiError(400, str(e)) from None
    if not deps:
        raise ApiError(400, "대비할 종속항이 없습니다. " + " / ".join(notes))
    (cdir / "dependents.txt").write_text(text + "\n", encoding="utf-8")
    _compare_spawn(run_id, ["--dependents-only"] + (["--alone"] if _no_base(run_dir) else []))
    return compare_state(run_id)


def claims_preview(body: dict) -> dict:
    """붙여 넣은 청구항을 프로그램이 어떻게 나눠 읽는지 미리 보여 준다. LLM 을 부르지 않는다.

    run 이 있으면 그 기록의 청구항에 딸린 종속항으로, 없으면 독립항과 종속항이 섞인 묶음으로 읽는다.
    """
    text = str(body.get("text") or "")

    def snip(s: str) -> str:
        return " ".join(s.split())[:110]

    base = None
    try:
        if body.get("run"):
            deps, notes = claims.read_dependents(text, _base_no(run_dir_of(str(body["run"]))))
        else:
            parts = claims.split_set(text)
            deps, notes = parts["deps"], parts["notes"]
            if parts["base_no"] is not None:  # None: 독립항 없이 종속항만 넣은 글
                base = {"no": parts["base_no"], "snippet": snip(claims.strip_mark(parts["base"]))}
    except claims.ClaimError as e:
        return {"error": str(e), "base": None, "deps": [], "notes": []}
    return {
        "error": "", "base": base, "notes": notes,
        "deps": [{"no": d["no"], "ref_label": claims.ref_label(d["refs"]), "detached": bool(d.get("detached")),
                  "snippet": snip(d["body"])} for d in deps],
    }


def _compare_spawn(run_id: str, args: list) -> None:
    run_dir = run_dir_of(run_id)
    cdir = run_dir / "compare"
    with LOCK:
        if _compare_running(run_id):
            raise ApiError(409, "이미 구성대비를 실행 중입니다")
        if not (run_dir / "00_claim.txt").exists():
            raise ApiError(400, "청구항이 없습니다")
        (cdir / "run.log").unlink(missing_ok=True)
        cmd = [sys.executable, "-m", "patsearch.compare", "--run", str(run_dir), *args]
        err = open(cdir / "run.err.log", "wb")
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1"),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        err.close()
        PROCS[f"{run_id}:compare"] = proc
        runs.write_status(cdir, "running", pid=proc.pid)

    def reap():
        code = proc.wait()
        status = runs._read_json(cdir / "status.json") or {}
        if cdir.is_dir() and status.get("state") == "running":
            runs.write_status(cdir, "error", message=f"구성대비 프로세스가 비정상 종료했습니다 (exit {code})", pid=0)

    threading.Thread(target=reap, daemon=True).start()


def compare_stop(run_id: str) -> dict:
    cdir = _compare_dir(run_id)
    proc = PROCS.get(f"{run_id}:compare")
    if proc is None or proc.poll() is not None:
        raise ApiError(409, "실행 중인 구성대비가 없습니다")
    _kill_tree(proc)
    status = runs._read_json(cdir / "status.json") or {}
    if status.get("state", "running") == "running":
        runs.write_status(cdir, "stopped", message="사용자가 중지했습니다. 다시 실행하면 처음부터 대비합니다.", pid=0)
        with open(cdir / "run.log", "a", encoding="utf-8") as f:
            f.write("사용자가 중지했습니다.\n")
    return compare_state(run_id)


# --- CLI 로그인 ------------------------------------------------------------------
def _cli_id(name: str) -> str:
    if name not in llm.CLIS:
        raise ApiError(404, "지원하지 않는 CLI 입니다")
    return name


def auth_list() -> dict:
    """모든 CLI 의 설치·로그인 상태. CLI 마다 몇 초씩 걸리므로 동시에 확인한다."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(len(llm.CLIS)) as pool:
        items = list(pool.map(llm.auth_status, llm.CLIS))
    return {"clis": items, "current": load_settings()["cli"]}


def auth_action(name: str, action: str) -> dict:
    name = _cli_id(name)
    if not shutil.which(llm.CLIS[name]["exe"]):
        raise ApiError(400, f'{llm.CLIS[name]["label"]} 가 설치되어 있지 않습니다')
    try:
        result = llm.login(name) if action == "login" else llm.logout(name)
    except llm.LLMError as e:
        raise ApiError(400, str(e)) from None
    return {**result, "status": llm.auth_status(name)}


def compare_compose(run_id: str, body: dict) -> dict:
    """인용발명 순서를 바꿔 양식만 다시 만든다. 저장된 판정을 쓰므로 LLM 을 부르지 않는다."""
    run_dir = run_dir_of(run_id)
    cdir = run_dir / "compare"
    if not (cdir / "judgments.json").exists():
        raise ApiError(400, "먼저 구성대비를 실행하세요")
    extra = body.get("extra")
    if extra is not None and not (isinstance(extra, list) and all(isinstance(x, str) for x in extra)):
        raise ApiError(400, "잘못된 요청입니다")
    compare.recompose(cdir, _claim(run_dir), primary=str(body.get("primary") or ""), extra=extra)
    return compare_state(run_id)


def app_state() -> dict:
    # 키 값 자체는 화면으로 보내지 않는다. 저장되어 있는지만 알려 준다.
    stored = {name: bool(env(name)) for name in KEY_NAMES}
    return {
        "app": {"name": APP_NAME, "tagline": APP_TAGLINE},
        "clis": llm.available_clis(),
        "keys": {"ops": stored["EPO_OPS_KEY"] and stored["EPO_OPS_SECRET"], "openalex": stored["OPENALEX_API_KEY"],
                 "stored": stored},
        "settings": load_settings(),
        "running": running_id(),
    }


# --- HTTP --------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "patsearch"

    def log_message(self, fmt, *args):  # 콘솔을 조용히
        pass

    def _send(self, status: int, body: bytes, ctype: str, cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value, status: int = 200) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _guard(self, write: bool) -> None:
        # 다른 사이트가 이 서버를 몰래 호출하지 못하게 한다: Host 는 로컬 주소여야 하고,
        # 상태를 바꾸는 요청은 브라우저가 다른 출처에서는 붙일 수 없는 전용 헤더가 있어야 한다.
        port = self.server.server_address[1]
        if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            raise ApiError(403, "허용되지 않은 접근입니다")
        if write and self.headers.get("X-Patsearch") != "1":
            raise ApiError(403, "허용되지 않은 접근입니다")

    def _raw(self, limit: int) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ApiError(400, "빈 파일입니다")
        if length > limit:
            raise ApiError(413, f"파일이 너무 큽니다 ({limit // (1024 * 1024)}MB 이하)")
        return self.rfile.read(length)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 200_000:
            raise ApiError(413, "요청이 너무 큽니다")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise ApiError(400, "잘못된 요청입니다") from None
        if not isinstance(data, dict):
            raise ApiError(400, "잘못된 요청입니다")
        return data

    def _route(self, method: str) -> None:
        try:
            self._guard(write=method != "GET")
            parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/") if p]
            if method == "GET":
                if not parts:
                    return self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
                if len(parts) == 1 and parts[0] in STATIC_FILES:
                    path = STATIC_DIR / parts[0]
                    if not path.is_file():
                        raise ApiError(404, "파일이 없습니다")
                    return self._send(200, path.read_bytes(), STATIC_FILES[parts[0]], cache=True)
                if parts == ["api", "state"]:
                    return self._json(app_state())
                if parts == ["api", "auth"]:
                    return self._json(auth_list())
                if len(parts) == 3 and parts[:2] == ["api", "auth"]:
                    return self._json(llm.auth_status(_cli_id(parts[2])))
                if parts == ["api", "runs"]:
                    return self._json(list_runs())
                if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                    return self._json(run_detail(parts[2]))
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "compare":
                    return self._json(compare_state(parts[2]))
                if len(parts) == 3 and parts[0] == "runs" and parts[2] in SERVED_FILES:
                    path = run_dir_of(parts[1]) / parts[2]
                    if not path.is_file():
                        raise ApiError(404, "파일이 없습니다")
                    return self._send(200, path.read_bytes(), SERVED_FILES[parts[2]])
            elif method == "POST":
                if parts == ["api", "runs"]:
                    return self._json(create_run(self._body()), 201)
                if len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3] == "compare":
                    if parts[4] == "docs":
                        name = unquote(self.headers.get("X-Filename", "document.pdf"))[:200]
                        return self._json(compare_upload(parts[2], name, self._raw(MAX_PDF_BYTES)), 201)
                    if parts[4] == "start":
                        return self._json(compare_start(parts[2]))
                    if parts[4] == "compose":
                        return self._json(compare_compose(parts[2], self._body()))
                    if parts[4] == "dependents":
                        return self._json(compare_dependents(parts[2], self._body()))
                    if parts[4] == "stop":
                        return self._json(compare_stop(parts[2]))
                if len(parts) == 4 and parts[:2] == ["api", "auth"] and parts[3] in ("login", "logout"):
                    return self._json(auth_action(parts[2], parts[3]))
                if parts == ["api", "claims", "preview"]:
                    return self._json(claims_preview(self._body()))
                if parts == ["api", "settings"]:
                    return self._json(save_settings_request(self._body()))
                if parts == ["api", "check"]:
                    return self._json(check_connections())
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "stop":
                    return self._json(stop_run(parts[2]))
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "resume":
                    return self._json(resume_run(parts[2]))
            elif method == "DELETE":
                if parts == ["api", "runs"]:
                    return self._json(delete_all())
                if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                    return self._json(delete_run(parts[2]))
                if len(parts) == 6 and parts[:2] == ["api", "runs"] and parts[3:5] == ["compare", "docs"]:
                    return self._json(compare_remove(parts[2], parts[5]))
            raise ApiError(404, "없는 주소입니다")
        except ApiError as e:
            self._json({"error": str(e)}, e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    load_env()
    p = argparse.ArgumentParser(prog="patsearch.web", description="선행기술 검색 화면을 띄운다.")
    p.add_argument("--port", type=int, default=0, help="포트 (기본 8765, 사용 중이면 다음 번호)")
    p.add_argument("--no-open", action="store_true", help="브라우저를 자동으로 열지 않음")
    args = p.parse_args()
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    # 포트를 지정하지 않았으면 8765 부터 비어 있는 포트를 찾는다
    candidates = [args.port] if args.port else list(range(8765, 8775))
    server = None
    for port in candidates:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError as e:
            last = e
    if server is None:
        print(f"포트 {candidates[0]} 를 열 수 없습니다: {last}\n다른 포트로 실행하세요: python -m patsearch.web --port 8800")
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"선행기술 검색 화면: {url}\n끄려면 이 창에서 Ctrl+C", flush=True)
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
