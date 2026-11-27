from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

APP_NAME = "Priora"
APP_TAGLINE = "청구항 기반 선행기술 검색"
ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".cache"
RUNS_DIR = ROOT / "runs"


def load_env(path: Path = ROOT / ".env") -> None:
    """.env 파일의 KEY=VALUE 를 환경변수로 올린다. 이미 있는 환경변수는 덮어쓰지 않는다."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def save_env(updates: dict, path: Path = ROOT / ".env") -> None:
    """.env 의 KEY=VALUE 를 고쳐 쓰고 현재 프로세스 환경변수에도 반영한다. 빈 값은 키를 비운다.

    주석과 다른 줄은 그대로 둔다.
    """
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    pending = dict(updates)
    out = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else ""
        if key in pending:
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(line)
    for key, value in pending.items():
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    for key, value in updates.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)


# --- 화면과 터미널이 함께 쓰는 기본 설정 (키가 아닌 것) ---------------------------
SETTINGS_FILE = ROOT / "settings.json"
DEFAULT_SETTINGS = {
    "cli": "claude",
    "models": {
        # model: 검색(구성 분해·검색식) · screen_model: 초록 스크리닝 · compare_model: 구성대비
        "claude": {"model": "sonnet", "screen_model": "haiku", "compare_model": "sonnet"},
        "codex": {"model": "", "screen_model": "", "compare_model": ""},
        "antigravity": {"model": "", "screen_model": "", "compare_model": ""},
    },
    "max_screen": 300,
}


MODEL_ROLES = ("model", "screen_model", "compare_model")


def load_settings() -> dict:
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if isinstance(saved, dict):
        if isinstance(saved.get("cli"), str):
            settings["cli"] = saved["cli"]
        if isinstance(saved.get("max_screen"), int):
            settings["max_screen"] = saved["max_screen"]
        for cli, models in (saved.get("models") or {}).items():
            if isinstance(models, dict):
                settings["models"].setdefault(cli, {"model": "", "screen_model": "", "compare_model": ""}).update(
                    {k: str(v) for k, v in models.items() if k in MODEL_ROLES}
                )
    return settings


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


class Cache:
    """프롬프트·검색식 해시를 키로 하는 단순 파일 캐시. 같은 요청을 다시 보내지 않기 위한 것."""

    def __init__(self, root: Path = CACHE_DIR, enabled: bool = True):
        self.root = root
        self.enabled = enabled

    def _path(self, ns: str, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.root / ns / f"{digest}.json"

    def get(self, ns: str, key: str):
        if not self.enabled:
            return None
        path = self._path(ns, key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put(self, ns: str, key: str, value) -> None:
        path = self._path(ns, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
