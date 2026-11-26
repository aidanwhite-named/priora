"""실행 폴더(runs\\<id>)의 상태·요약을 읽고 쓰는 도우미. 터미널 실행과 웹 화면이 함께 쓴다."""
from __future__ import annotations

import ctypes
import json
import os
import re
import time
from pathlib import Path

RUN_ID = re.compile(r"^\d{8}-\d{6}-[A-Za-z0-9_\-]{1,60}$")
STAGE_FILES = ["01_elements.json", "02_queries.json", "04_candidates.json", "05_screening.json", "report.html"]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_status(run_dir: Path, state: str, **extra) -> None:
    data = {"state": state, "pid": os.getpid(), "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
    data.update(extra)
    (run_dir / "status.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def run_state(run_dir: Path) -> dict:
    """state: running | done | error | stopped | interrupted"""
    status = _read_json(run_dir / "status.json") or {}
    state = status.get("state", "")
    if state == "running":
        if not pid_alive(status.get("pid", 0)):
            state = "interrupted"
    elif not state:
        state = "done" if (run_dir / "report.html").exists() else "interrupted"
    return {"state": state, "message": status.get("message", ""), "pid": status.get("pid", 0)}


def stage_progress(run_dir: Path) -> int:
    """끝난 단계 수(0~5)."""
    done = 0
    for name in STAGE_FILES:
        if not (run_dir / name).exists():
            break
        done += 1
    return done


def summarize(run_dir: Path, write: bool = True) -> dict:
    decomp = _read_json(run_dir / "01_elements.json") or {}
    log = _read_json(run_dir / "03_search_log.json") or []
    cands = _read_json(run_dir / "04_candidates.json") or []
    screening = _read_json(run_dir / "05_screening.json") or []
    summary = {
        "title": decomp.get("title_en", ""),
        "gist": decomp.get("gist_ko", ""),
        "queries_run": len(log),
        "queries_used": sum(1 for e in log if e.get("status") in ("ok", "few")),
        "patents": sum(1 for c in cands if c.get("kind") == "patent"),
        "papers": sum(1 for c in cands if c.get("kind") == "paper"),
        "screened": len(screening),
        "high": sum(1 for r in screening if r.get("grade") == "high"),
        "mid": sum(1 for r in screening if r.get("grade") == "mid"),
    }
    if write:
        (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    return summary


def read_summary(run_dir: Path) -> dict:
    summary = _read_json(run_dir / "summary.json")
    if summary is None and (run_dir / "report.html").exists():
        summary = summarize(run_dir)
    return summary or {}


def read_meta(run_dir: Path) -> dict:
    return _read_json(run_dir / "meta.json") or {}
