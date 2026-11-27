"""로컬에 설치된 LLM CLI 를 LLM 으로 쓴다. API 키 없이 각 CLI 의 로그인 계정으로 동작한다.

지원: Claude Code(`claude -p`), Codex CLI(`codex exec`), Antigravity CLI(`agy -p`).
새 CLI 를 붙이려면 CLIS 에 항목을 넣고 _call_<이름> 함수와 로그인 상태 확인(_status_<이름>)·AUTH 항목을 추가한다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

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
    "antigravity": {"label": "Antigravity CLI", "exe": "agy",
                    # 로그인되어 있으면 `agy models` 로 받은 전체 목록(auth_status 의 models)이 화면에서 덧붙는다
                    "models": ["gemini-4-neon-medium", "gemini-4-neon-high", "gemini-4-carbon-medium",
                               "gemini-4-argon-medium", "gemini-4-hydrogen-medium", "gemini-4-hydrogen-high",
                               "gemini-3.8-flash-medium", "gemini-3.8-flash-low",
                               "claude-sonnet-5-5-medium", "claude-opus-5-5-medium"],
                    "screen_default": ""},
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


def _call_antigravity(prompt: str, model: str, timeout: int) -> str:
    # 프롬프트가 길어 명령줄 인자로 넘기지 않는다. stream-json 입력으로 표준입력에 메시지 한 줄을 보낸다.
    cmd = [
        cli_path("antigravity"),
        "--input-format", "stream-json", "--output-format", "stream-json",
        "--sandbox", "--disable-slash-commands",
    ]
    if model:
        cmd += ["--model", model]
    cmd.append("-p=")
    message = json.dumps({"event": "user", "message": {"content": f"{SYSTEM}\n\n{prompt}"}}, ensure_ascii=False)
    code, stdout, stderr = _run(cmd, message + "\n", timeout, "Antigravity CLI")
    result = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") == "result":
            result = event.get("result") or {}
    if not result:
        raise LLMError(f"Antigravity CLI 출력이 없습니다 (exit {code}): {(stderr or stdout)[-500:]}")
    if result.get("status") != "SUCCESS" or not str(result.get("response") or "").strip():
        raise LLMError(f"Antigravity CLI 오류: {str(result.get('error') or result.get('status') or stderr)[:500]}")
    return result["response"]


_CALLERS = {"claude": _call_claude, "codex": _call_codex, "antigravity": _call_antigravity}


# --- 로그인 상태 · 로그인 · 로그아웃 ----------------------------------------------------
# login: 사용자가 직접 입력해야 하므로 새 콘솔 창에서 띄울 인자.
# logout: 창 없이 실행할 인자. 그런 명령이 없는 CLI 는 logout_console 로 콘솔 창을 띄워 안내한다.
AUTH = {
    "claude": {"login": ["auth", "login"], "logout": ["auth", "logout"]},
    "codex": {"login": ["login"], "logout": ["logout"]},
    # agy 에는 로그인 하위 명령이 없다. 대화 창을 띄우면서 /login, /logout 을 첫 입력으로 넣는다.
    "antigravity": {"login": ["-i", "/login"], "logout_console": ["-i", "/logout"],
                    "hint_login": "로그인 안내가 나오지 않으면 이 창에서 /login 을 입력하세요.",
                    "hint_logout": "로그아웃되지 않았으면 이 창에서 /logout 을 입력하세요."},
}


def _quick(cmd: list, timeout: int = 40):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=str(CACHE_DIR),
                              creationflags=0x08000000 if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        return None, "", "응답 시간 초과"
    except OSError as e:
        return None, "", str(e)
    return proc.returncode, proc.stdout.decode("utf-8", "replace").strip(), proc.stderr.decode("utf-8", "replace").strip()


def _status_claude(exe: str) -> dict:
    code, out, err = _quick([exe, "auth", "status", "--json"])
    try:
        info = json.loads(out)
    except ValueError:
        return {"logged_in": None, "detail": (err or out or "상태를 읽지 못했습니다")[:300]}
    if not info.get("loggedIn"):
        return {"logged_in": False, "detail": "로그인되어 있지 않습니다"}
    method = {"claude.ai": "Claude 구독", "console": "Anthropic Console"}.get(info.get("authMethod"), info.get("authMethod") or "")
    parts = [info.get("email") or "", method, str(info.get("subscriptionType") or "").upper()]
    return {"logged_in": True, "detail": " · ".join(p for p in parts if p)}


def _status_codex(exe: str) -> dict:
    code, out, err = _quick([exe, "login", "status"])
    text = (out or err).strip()
    if code is None:
        return {"logged_in": None, "detail": text[:300]}
    if code == 0 and "not logged" not in text.lower():
        return {"logged_in": True, "detail": text.replace("Logged in using", "로그인 방식:")[:300]}
    return {"logged_in": False, "detail": "로그인되어 있지 않습니다"}


def _status_antigravity(exe: str) -> dict:
    # 로그인 상태를 묻는 명령이 없어, 로그인해야 받을 수 있는 모델 목록을 받아 본다
    code, out, err = _quick([exe, "models"], timeout=60)
    models = [ln.split("\t", 1)[0].strip() for ln in out.splitlines() if "\t" in ln]
    models = [m for m in models if m]
    if code == 0 and models:
        return {"logged_in": True, "detail": f"Google 계정 · 쓸 수 있는 모델 {len(models)}개", "models": models}
    text = (err or out or "").strip()
    if code is None:
        return {"logged_in": None, "detail": text[:300]}
    return {"logged_in": False, "detail": ("로그인이 필요합니다. " + text[-200:]).strip()}


_STATUS = {"claude": _status_claude, "codex": _status_codex, "antigravity": _status_antigravity}


def auth_status(name: str) -> dict:
    """CLI 하나의 설치·로그인 상태. logged_in: True | False | None(알 수 없음)."""
    c = CLIS[name]
    exe = shutil.which(c["exe"])
    base = {"id": name, "label": c["label"], "exe": c["exe"], "installed": bool(exe), "version": "",
            "logged_in": False, "detail": ""}
    if not exe:
        return {**base, "detail": f"설치되어 있지 않습니다 (`{c['exe']}` 명령을 찾을 수 없음)"}
    if name != "antigravity":  # agy 에는 --version 이 없다
        code, out, _ = _quick([exe, "--version"], timeout=20)
        if code == 0 and out:
            base["version"] = out.splitlines()[0][:80]
    return {**base, **_STATUS[name](exe)}


def _console(name: str, args: list, hint: str) -> None:
    """사용자가 직접 입력할 수 있도록 새 콘솔 창에서 CLI 를 띄운다."""
    exe = cli_path(name)
    if os.name != "nt":
        raise LLMError(f"터미널을 열어 다음 명령을 직접 실행하세요: {' '.join([CLIS[name]['exe'], *args])}")
    tmp_dir = CACHE_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    script = tmp_dir / f"auth-{name}.cmd"
    label = CLIS[name]["label"]
    script.write_text(
        "@echo off\r\nchcp 65001 >nul\r\n"
        f"title {label}\r\n"
        f"echo [{label}] {hint}\r\necho.\r\n"
        f"call {subprocess.list2cmdline([exe, *args])}\r\n"
        "echo.\r\necho 끝났으면 이 창을 닫고 Priora 화면에서 [상태 새로고침]을 누르세요.\r\npause\r\n",
        encoding="utf-8",
    )
    subprocess.Popen(["cmd", "/c", str(script)], cwd=str(CACHE_DIR), creationflags=0x00000010)  # CREATE_NEW_CONSOLE


def login(name: str) -> dict:
    spec = AUTH[name]
    _console(name, spec["login"], spec.get("hint_login", "브라우저가 열리면 로그인을 마치세요."))
    return {"opened": True}


def logout(name: str) -> dict:
    spec = AUTH[name]
    if spec.get("logout"):
        code, out, err = _quick([cli_path(name), *spec["logout"]], timeout=60)
        if code != 0:
            raise LLMError(f"{CLIS[name]['label']} 로그아웃 실패: {(err or out or '응답 없음')[:300]}")
        return {"opened": False}
    _console(name, spec["logout_console"], spec.get("hint_logout", ""))
    return {"opened": True}


# 세 CLI 모두 `update` 하위 명령으로 스스로 최신 버전을 받는다. 진행 상황이 보이도록 새 콘솔 창에서 띄운다.
UPDATE = {"claude": ["update"], "codex": ["update"], "antigravity": ["update"]}


def update(name: str) -> dict:
    _console(name, UPDATE[name], "최신 버전을 확인하고 설치합니다.")
    return {"opened": True}


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
