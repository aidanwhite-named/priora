"""Priora 가 LLM CLI 를 띄우는 작업 폴더(<프로그램 폴더>\\.cache)에서 생긴 CLI 세션 기록을 찾아 지운다.

uninstall.bat 이 부른다. 표준 라이브러리만 쓴다.

    python tools\\cli_sessions.py --workdir D:\\Priora\\.cache           무엇이 지워질지만 알려 준다
    python tools\\cli_sessions.py --workdir D:\\Priora\\.cache --apply   실제로 지운다

지우는 범위는 "작업 폴더가 --workdir 이거나 그 아래인 세션"뿐이다. 프로그램 폴더 자체에서 사용자가 직접 연
세션, 다른 폴더의 세션, CLI 의 로그인·설정은 건드리지 않는다. 각 CLI 의 저장 형식은 공개 규격이 아니므로
모르는 형식은 건너뛰고 errors 에 적는다. 결과는 JSON 한 줄(ASCII)로 표준 출력에 쓴다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _norm(path) -> str:
    text = str(path or "").strip().strip('"')
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return os.path.normcase(os.path.normpath(text)).rstrip("\\/") if text else ""


class Target:
    """지울 세션의 작업 폴더. 그 폴더와 하위 폴더만 맞는 것으로 본다."""

    def __init__(self, workdir: str):
        self.raw = os.path.abspath(workdir).rstrip("\\/")
        self.path = _norm(self.raw)

    def has(self, path) -> bool:
        n = _norm(path)
        return bool(n) and (n == self.path or n.startswith(self.path + os.sep))

    def has_uri(self, uri) -> bool:
        text = str(uri or "")
        if not text.lower().startswith("file:"):
            return self.has(text)
        path = unquote(urlparse(text).path)
        if re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
        return self.has(path)


class Job:
    def __init__(self, apply: bool):
        self.apply = apply
        self.report: dict = {}

    def section(self, name: str) -> dict:
        return self.report.setdefault(name, {"sessions": 0, "items": [], "errors": []})

    def remove(self, name: str, path: Path) -> None:
        sec = self.section(name)
        sec["items"].append(str(path))
        if not self.apply:
            return
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            sec["errors"].append(f"{path}: {e}")

    def edit(self, name: str, label: str, fn) -> None:
        """파일 안의 일부만 고치는 작업. 미리 보기에서는 label 만 적는다."""
        sec = self.section(name)
        sec["items"].append(label)
        if not self.apply:
            return
        try:
            fn()
        except Exception as e:  # 남의 프로그램 파일이라 형식이 달라도 전체가 멈추면 안 된다
            sec["errors"].append(f"{label}: {type(e).__name__}: {e}")

    def note(self, name: str, text: str) -> None:
        self.section(name)["errors"].append(text)


# --- 공통 도구 -------------------------------------------------------------------
def _write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".priora-tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _filter_jsonl(job: Job, name: str, path: Path, drop) -> None:
    """jsonl 에서 drop(줄을 해석한 값)이 참인 줄만 뺀다."""
    try:
        lines = path.read_text(encoding="utf-8", errors="surrogateescape").splitlines(keepends=True)
    except OSError:
        return
    keep = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            value = None
        if isinstance(value, dict) and drop(value):
            continue
        keep.append(line)
    removed = len(lines) - len(keep)
    if removed:
        job.edit(name, f"{path} (입력 기록 {removed}줄)",
                 lambda: _write_text_raw(path, "".join(keep)))


def _write_text_raw(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".priora-tmp")
    tmp.write_text(text, encoding="utf-8", errors="surrogateescape", newline="")
    os.replace(tmp, path)


def _connect(db: Path, write: bool) -> sqlite3.Connection:
    if write:
        return sqlite3.connect(str(db), timeout=5)
    return sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)


def _tables(con: sqlite3.Connection) -> dict:
    """테이블 이름 → 열 이름 목록 (가상 테이블 포함)."""
    out = {}
    for (table,) in con.execute("select name from sqlite_master where type='table'").fetchall():
        try:
            out[table] = [row[1] for row in con.execute(f'pragma table_info("{table}")')]
        except sqlite3.Error:
            out[table] = []
    return out


def _delete_rows(db: Path, column: str, ids: set) -> None:
    """column 열을 가진 모든 테이블에서 그 값이 ids 인 줄을 지운다."""
    con = _connect(db, write=True)
    try:
        marks = ",".join("?" * len(ids))
        for table, cols in _tables(con).items():
            if column in cols:
                try:
                    con.execute(f'delete from "{table}" where "{column}" in ({marks})', list(ids))
                except sqlite3.Error:
                    pass  # 전문 검색용 내부 테이블 등은 직접 지울 수 없다
        con.commit()
    finally:
        con.close()


# --- Claude Code ------------------------------------------------------------------
def _first_cwds(project_dir: Path) -> list:
    """세션 파일마다 가장 먼저 적힌 작업 폴더."""
    found = []
    for f in project_dir.glob("*.jsonl"):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for _, line in zip(range(60), fh):
                    try:
                        cwd = json.loads(line).get("cwd")
                    except (ValueError, AttributeError):
                        continue
                    if cwd:
                        found.append(cwd)
                        break
        except OSError:
            continue
    return found


def clean_claude(job: Job, t: Target, home: Path) -> None:
    name = "claude"
    sec = job.section(name)
    base = home / ".claude"
    sids = set()
    encoded = re.sub(r"[^A-Za-z0-9]", "-", t.raw).lower()  # Claude Code 가 작업 폴더로 만드는 폴더 이름
    projects = base / "projects"
    if projects.is_dir():
        for d in sorted(projects.iterdir()):
            dname = d.name.lower()
            if not d.is_dir() or not (dname == encoded or dname.startswith(encoded + "-")):
                continue
            cwds = _first_cwds(d)
            # 이름이 같아도 다른 폴더일 수 있다(기호가 모두 '-' 로 바뀌므로). 세션에 적힌 작업 폴더로 확인한다.
            if (cwds and not all(t.has(c) for c in cwds)) or (not cwds and dname != encoded):
                continue
            ids = {f.stem for f in d.glob("*.jsonl")} | {x.name for x in d.iterdir() if x.is_dir() and UUID.match(x.name)}
            sids |= ids
            sec["sessions"] += len(ids)
            job.remove(name, d)
    sessions = base / "sessions"
    if sessions.is_dir():
        for f in sorted(sessions.glob("*.json")):
            data = _read_json(f)
            if isinstance(data, dict) and t.has(data.get("cwd")):
                if data.get("sessionId"):
                    sids.add(str(data["sessionId"]))
                job.remove(name, f)
                for extra in sessions.glob(f.stem + ".*.key"):
                    job.remove(name, extra)
    for sub in ("file-history", "session-env", "todos", "debug", "tasks"):
        folder = base / sub
        if not folder.is_dir() or not sids:
            continue
        for entry in sorted(folder.iterdir()):
            m = UUID.match(entry.name)
            if m and m.group(0) in sids:
                job.remove(name, entry)
    _filter_jsonl(job, name, base / "history.jsonl", lambda v: t.has(v.get("project")))

    config = home / ".claude.json"
    data = _read_json(config)
    if isinstance(data, dict) and isinstance(data.get("projects"), dict):
        keys = [k for k in data["projects"] if t.has(k)]
        if keys:
            def rewrite():
                fresh = _read_json(config)  # 그 사이 Claude Code 가 고쳤을 수 있으니 다시 읽는다
                for k in [k for k in fresh["projects"] if t.has(k)]:
                    del fresh["projects"][k]
                _write_text(config, json.dumps(fresh, ensure_ascii=False, indent=2))
            job.edit(name, f"{config} (폴더 항목 {len(keys)}개)", rewrite)


# --- Codex CLI --------------------------------------------------------------------
def clean_codex(job: Job, t: Target, home: Path, base: Path | None = None) -> None:
    name = "codex"
    sec = job.section(name)
    base = base or home / ".codex"
    if not base.is_dir():
        return
    ids = set()
    for folder in ("sessions", "archived_sessions"):
        if not (base / folder).is_dir():
            continue
        for f in sorted((base / folder).rglob("*.jsonl")):
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    meta = json.loads(fh.readline())
            except (OSError, ValueError):
                continue
            payload = meta.get("payload") if isinstance(meta, dict) and isinstance(meta.get("payload"), dict) else meta
            if isinstance(payload, dict) and t.has(payload.get("cwd")):
                if payload.get("id"):
                    ids.add(str(payload["id"]))
                sec["sessions"] += 1
                job.remove(name, f)
    listed = {i for i in sec["items"]}
    for db in sorted(base.glob("state_*.sqlite")):
        try:
            con = _connect(db, write=False)
            try:
                cols = _tables(con).get("threads", [])
                if "cwd" not in cols or "id" not in cols:
                    continue
                pick = "id, cwd, rollout_path" if "rollout_path" in cols else "id, cwd, ''"
                rows = [r for r in con.execute(f"select {pick} from threads") if t.has(r[1])]
            finally:
                con.close()
        except sqlite3.Error as e:
            job.note(name, f"{db}: 읽지 못했습니다 ({e}). Codex 를 끄고 다시 실행하세요.")
            continue
        for tid, _cwd, rollout in rows:
            if str(tid) not in ids:
                ids.add(str(tid))
                sec["sessions"] += 1
            path = Path(rollout) if rollout else None
            if path and path.is_file() and _norm(path).startswith(_norm(base) + os.sep) and str(path) not in listed:
                listed.add(str(path))
                job.remove(name, path)
        if rows:
            tids = {str(r[0]) for r in rows}
            job.edit(name, f"{db} (threads {len(rows)}줄)", lambda db=db, tids=tids: _delete_rows(db, "id", tids))
    if ids:
        for db in sorted(list(base.glob("state_*.sqlite")) + list(base.glob("thread_history_*.sqlite"))):
            job.edit(name, f"{db} (세션에 딸린 줄)", lambda db=db: _delete_rows(db, "thread_id", ids))
        _filter_jsonl(job, name, base / "history.jsonl", lambda v: str(v.get("session_id") or "") in ids)

    config = base / "config.toml"
    try:
        lines = config.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        lines = []
    keep, skipping, dropped = [], False, 0
    for line in lines:
        head = re.match(r"^\s*\[projects\.(['\"])(.+)\1\]\s*$", line)
        if head:
            path = head.group(2).replace("\\\\", "\\") if head.group(1) == '"' else head.group(2)
            skipping = t.has(path)
            dropped += skipping
        elif line.lstrip().startswith("["):
            skipping = False
        if not skipping:
            keep.append(line)
    if dropped:
        job.edit(name, f"{config} (폴더 신뢰 항목 {dropped}개)", lambda: _write_text(config, "".join(keep)))


# --- Antigravity CLI --------------------------------------------------------------
AGY_KEEP_DIRS = {"bin", "builtin", "updater", "cache", "log"}


def clean_antigravity(job: Job, t: Target, home: Path) -> None:
    name = "antigravity"
    sec = job.section(name)
    root = home / ".gemini"
    base = root / "antigravity-cli"
    if not base.is_dir():
        return
    ids = set()

    meta_file = base / "cache" / "conversation_metadata.json"
    meta = _read_json(meta_file)
    convs = meta.get("conversations") if isinstance(meta, dict) else None
    if isinstance(convs, dict):
        for cid, entry in convs.items():
            summary = entry.get("summary") if isinstance(entry, dict) else None
            uris = summary.get("WorkspaceURIs") if isinstance(summary, dict) else None
            if isinstance(uris, list) and any(t.has_uri(u) for u in uris):
                ids.add(cid)
    last_file = base / "cache" / "last_conversations.json"
    last = _read_json(last_file)
    last_keys = [k for k in last if t.has(k)] if isinstance(last, dict) else []
    ids |= {str(last[k]) for k in last_keys if isinstance(last[k], str)}

    dbs = [db for db in (base / "conversation_summaries.db", root / "antigravity" / "conversation_summaries.db") if db.is_file()]
    db_hits = {}
    for db in dbs:
        try:
            con = _connect(db, write=False)
            try:
                cols = _tables(con).get("conversation_summaries", [])
                if "conversation_id" not in cols or "workspace_uris" not in cols:
                    continue
                hit = {str(cid) for cid, uris in con.execute("select conversation_id, workspace_uris from conversation_summaries")
                       if any(t.has_uri(u) for u in re.findall(r'file:[^\s",\]]+', str(uris or "")))}
            finally:
                con.close()
        except sqlite3.Error as e:
            job.note(name, f"{db}: 읽지 못했습니다 ({e}). Antigravity 를 끄고 다시 실행하세요.")
            continue
        db_hits[db] = hit
        ids |= hit
    sec["sessions"] = len(ids)

    if ids:
        for folder in sorted(p for p in base.iterdir() if p.is_dir() and p.name not in AGY_KEEP_DIRS):
            for entry in sorted(folder.iterdir()):
                m = UUID.match(entry.name)
                if m and m.group(0) in ids:
                    job.remove(name, entry)  # 대화 파일, brain 폴더, 주석, 잠금 파일
        if isinstance(convs, dict) and any(c in convs for c in ids):
            def rewrite_meta():
                fresh = _read_json(meta_file)
                for cid in ids:
                    fresh["conversations"].pop(cid, None)
                _write_text(meta_file, json.dumps(fresh, ensure_ascii=False, indent=2))
            job.edit(name, f"{meta_file} (대화 목록)", rewrite_meta)
        for db in dbs:
            if db_hits.get(db) or db in db_hits:
                job.edit(name, f"{db} (대화 요약·검색 색인)", lambda db=db: _delete_rows(db, "conversation_id", ids))
    if last_keys:
        def rewrite_last():
            fresh = _read_json(last_file)
            for k in [k for k in fresh if t.has(k)]:
                del fresh[k]
            _write_text(last_file, json.dumps(fresh, ensure_ascii=False, indent=2))
        job.edit(name, f"{last_file} (폴더별 마지막 대화)", rewrite_last)
    _filter_jsonl(job, name, base / "history.jsonl", lambda v: t.has(v.get("workspace")))

    # 실행할 때마다 생기는 로그. 첫머리에 적힌 작업 폴더로 가린다.
    log_dir = base / "log"
    removed_logs = set()
    if log_dir.is_dir():
        for f in sorted(log_dir.glob("*.log")):
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    head = fh.read(200_000)
            except OSError:
                continue
            dirs = [m.strip() for m in re.findall(r"workspaceDirs=\[([^\]]*)\]", head)]
            dirs += [m.strip() for m in re.findall(r"for workspace (.+)$", head, flags=re.M)]
            if dirs and all(t.has(d) for d in dirs):
                removed_logs.add(f.name)
                job.remove(name, f)
    link = base / "cli.log"  # 가장 최근 로그를 가리키는 링크
    try:
        if link.is_symlink() and Path(os.readlink(link)).name in removed_logs:
            job.remove(name, link)
    except OSError:
        pass


def main() -> int:
    p = argparse.ArgumentParser(description="Priora 작업 폴더에서 생긴 LLM CLI 세션 기록을 지운다.")
    p.add_argument("--workdir", required=True, help="Priora 가 CLI 를 띄우는 폴더 (<프로그램 폴더>\\.cache)")
    p.add_argument("--home", default="", help="사용자 폴더 (기본: 현재 사용자). 시험용")
    p.add_argument("--apply", action="store_true", help="실제로 지운다. 없으면 무엇이 지워질지만 알려 준다")
    args = p.parse_args()

    target = Target(args.workdir)
    if len(Path(target.raw).parts) < 3 or Path(target.raw).name.lower() != ".cache":
        # 잘못된 값(예: 드라이브 전체, 프로그램 폴더 자체)으로 남의 세션까지 지우는 일을 막는다
        print(json.dumps({"error": "workdir must be the .cache folder inside the program folder"}))
        return 2
    home = Path(args.home) if args.home else Path.home()
    job = Job(args.apply)
    codex_home = None if args.home else (Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else None)
    for fn, extra in ((clean_claude, ()), (clean_codex, (codex_home,)), (clean_antigravity, ())):
        try:
            fn(job, target, home, *extra)
        except Exception as e:
            job.note(fn.__name__.replace("clean_", ""), f"{type(e).__name__}: {e}")
    print(json.dumps({"workdir": target.raw, "apply": args.apply, "clis": job.report}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
