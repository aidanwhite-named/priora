"""로컬에 설치된 LLM CLI 를 LLM 으로 쓴다. API 키 없이 각 CLI 의 로그인 계정으로 동작한다.

지원: Claude Code(`claude -p`), Codex CLI(`codex exec`). 새 CLI 를 붙이려면 CLIS 에 항목을 넣고
_call_<이름> 함수를 추가한다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from .config import CACHE_DIR

SYSTEM = (
    "You are a patent prior-art search analyst. Follow the instructions in the user message exactly "
    "and reply with one valid JSON value only. No prose, no code fences."
)

# models: 화면의 선택 목록에 보여 줄 모델 이름. 빈 문자열(기본값)은 CLI 자체 설정을 따른다.
CLIS = {
    "claude": {"label": "Claude Code", "exe": "claude", "models": ["fable", "opus", "sonnet", "haiku"],
               "screen_default": "haiku"},
    "codex": {"label": "Codex CLI", "exe": "codex", "models": [], "screen_default": ""},
}
_current = "claude"


class LLMError(Exception):
    pass


def set_cli(name: str) -> None:
    global _current
    if name not in CLIS:
        raise LLMError(f"지원하지 않는 CLI 입니다: {name} (지원: {', '.join(CLIS)})")
    _current = name


def current_cli() -> str:
    return _current


def available_clis() -> list:
    return [
        {"id": cid, "label": c["label"], "available": bool(shutil.which(c["exe"])), "models": c["models"],
         "screen_default": c["screen_default"]}
        for cid, c in CLIS.items()
    ]


def cli_path(name: str = "") -> str:
    name = name or _current
    exe = shutil.which(CLIS[name]["exe"])
    if not exe:
        raise LLMError(f'{CLIS[name]["label"]} CLI 를 찾을 수 없습니다. 터미널에서 `{CLIS[name]["exe"]} --version` 이 되는지 확인하세요.')
    return exe


def claude_path() -> str:
    return cli_path("claude")


def _extract_json(text: str):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0].strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    if not starts:
        raise ValueError("응답에 JSON 이 없습니다")
    value, _ = json.JSONDecoder().raw_decode(t[min(starts):])
    return value


def _run(cmd: list, prompt: str, timeout: int, label: str):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            cmd, input=prompt.encode("utf-8"), capture_output=True, timeout=timeout, cwd=str(CACHE_DIR),
            # 웹 화면에서 띄운 경우 호출할 때마다 콘솔 창이 번쩍이지 않게 한다 (Windows)
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        raise LLMError(f"{label} 응답 시간 초과 ({timeout}초)") from None
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace").strip(),
        proc.stderr.decode("utf-8", "replace").strip(),
    )


def _call_claude(prompt: str, model: str, timeout: int) -> str:
    cmd = [
        cli_path("claude"), "-p",
        "--output-format", "json",
        "--tools", "",
        "--no-session-persistence",
        "--system-prompt", SYSTEM,
    ]
    if model:
        cmd += ["--model", model]
    code, stdout, stderr = _run(cmd, prompt, timeout, "claude CLI")
    if not stdout:
        raise LLMError(f"claude CLI 출력이 없습니다 (exit {code}): {stderr[:500]}")
    try:
        outer = json.loads(stdout)
    except ValueError:
        raise LLMError(f"claude CLI 출력 해석 실패: {stdout[:500]}") from None
    if outer.get("is_error") or "result" not in outer:
        raise LLMError(f"claude CLI 오류: {str(outer.get('result') or outer.get('subtype') or stderr)[:500]}")
    return outer["result"]


def _call_codex(prompt: str, model: str, timeout: int) -> str:
    tmp_dir = CACHE_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=tmp_dir, suffix=".txt", delete=False) as f:
        out_path = f.name
    cmd = [
        cli_path("codex"), "exec",
        "--skip-git-repo-check", "--ephemeral",
        "--sandbox", "read-only",
        "--color", "never",
        "-o", out_path,
    ]
    if model:
        cmd += ["-m", model]
    try:
        # 프롬프트 인자를 주지 않으면 표준입력에서 읽는다. 시스템 지시는 앞에 붙인다.
        code, stdout, stderr = _run(cmd, f"{SYSTEM}\n\n{prompt}", timeout, "codex CLI")
        try:
            with open(out_path, encoding="utf-8") as f:
                text = f.read().strip()
        except OSError:
            text = ""
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
    if code != 0 or not text:
        raise LLMError(f"codex CLI 오류 (exit {code}): {(stderr or stdout)[-500:]}")
    return text


_CALLERS = {"claude": _call_claude, "codex": _call_codex}


def ask_json(prompt: str, model: str = "", cache=None, timeout: int = 900, retries: int = 1):
    """프롬프트를 보내고 JSON 으로 해석된 응답을 돌려준다."""
    key = f"{model}|{prompt}" if _current == "claude" else f"{_current}|{model}|{prompt}"
    if cache is not None:
        hit = cache.get("llm", key)
        if hit is not None:
            return hit["value"]
    error = None
    for _ in range(retries + 1):
        text = _CALLERS[_current](prompt, model, timeout)
        try:
            value = _extract_json(text)
        except ValueError as e:
            error = LLMError(f"LLM 응답이 JSON 이 아닙니다: {e}: {text[:300]}")
            continue
        if cache is not None:
            cache.put("llm", key, {"value": value})
        return value
    raise error
