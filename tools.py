from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Callable, Dict, List


WORKSPACE_ROOT = (Path(__file__).resolve().parent / "workspace").resolve()
ALLOWED_COMMANDS = {"ls", "node", "python"}
SHELL_TIMEOUT = 30


class ToolError(Exception):
    pass


def ensure_workspace() -> None:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)


def ensure_relative_path(path: str) -> None:
    candidate = Path(path)
    if candidate.is_absolute():
        raise ToolError("absolute paths are not allowed; use a path relative to workspace")


def is_within_workspace(path: Path) -> bool:
    common = Path(os.path.commonpath([str(WORKSPACE_ROOT), str(path.resolve())]))
    return common == WORKSPACE_ROOT


def resolve_user_path(path: str) -> Path:
    ensure_workspace()
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate.resolve()
    return (WORKSPACE_ROOT / candidate).resolve()


def approval_root_for_path(path: Path) -> Path:
    if path.exists() and path.is_dir():
        return path
    return path.parent


def shell_access_targets(command: str) -> List[Path]:
    ensure_workspace()
    parts = shlex.split(command)
    if not parts:
        raise ToolError("empty command")
    if parts[0] not in ALLOWED_COMMANDS:
        raise ToolError(f"command not allowed: {parts[0]}")

    targets: List[Path] = []
    for arg in parts[1:]:
        if arg.startswith("-"):
            continue
        candidate = Path(arg)
        if candidate.is_absolute():
            targets.append(candidate.resolve())
            continue
        resolved = (WORKSPACE_ROOT / candidate).resolve()
        if not is_within_workspace(resolved):
            targets.append(resolved)
    return targets


def resolve_workspace_path(path: str) -> Path:
    ensure_workspace()
    ensure_relative_path(path)
    target = (WORKSPACE_ROOT / path).resolve()
    if not is_within_workspace(target):
        raise ToolError("path escapes workspace")
    return target


def resolve_path(path: str, allow_absolute: bool = False) -> Path:
    ensure_workspace()
    candidate = Path(path)
    if candidate.is_absolute():
        if not allow_absolute:
            raise ToolError("absolute paths are not allowed; use a path relative to workspace")
        return candidate.resolve()

    target = (WORKSPACE_ROOT / candidate).resolve()
    if not allow_absolute and not is_within_workspace(target):
        raise ToolError("path escapes workspace")
    return target


def write_file(path: str, content: str, allow_absolute: bool = False) -> dict:
    target = resolve_path(path, allow_absolute=allow_absolute)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    result = {
        "ok": True,
        "bytes": len(content.encode("utf-8")),
    }
    if is_within_workspace(target):
        result["path"] = str(target.relative_to(WORKSPACE_ROOT))
    else:
        result["path"] = str(target)
    return result


def read_file(path: str, allow_absolute: bool = False) -> dict:
    target = resolve_path(path, allow_absolute=allow_absolute)
    if not target.exists():
        raise ToolError(f"file not found: {path}")
    if not target.is_file():
        raise ToolError(f"not a file: {path}")
    result = {
        "ok": True,
        "content": target.read_text(encoding="utf-8"),
    }
    if is_within_workspace(target):
        result["path"] = str(target.relative_to(WORKSPACE_ROOT))
    else:
        result["path"] = str(target)
    return result


def list_dir(path: str = ".", allow_absolute: bool = False) -> dict:
    target = resolve_path(path, allow_absolute=allow_absolute)
    if not target.exists():
        raise ToolError(f"directory not found: {path}")
    if not target.is_dir():
        raise ToolError(f"not a directory: {path}")
    entries = []
    for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        entries.append(
            {
                "name": item.name,
                "type": "dir" if item.is_dir() else "file",
            }
        )
    result = {
        "ok": True,
        "entries": entries,
    }
    if target == WORKSPACE_ROOT:
        result["path"] = "."
    elif is_within_workspace(target):
        result["path"] = str(target.relative_to(WORKSPACE_ROOT))
    else:
        result["path"] = str(target)
    return result


def run_shell(command: str, allow_external_args: bool = False, timeout: int | None = None) -> dict:
    ensure_workspace()
    parts = shlex.split(command)
    if not parts:
        raise ToolError("empty command")
    if parts[0] not in ALLOWED_COMMANDS:
        raise ToolError(f"command not allowed: {parts[0]}")
    for arg in parts[1:]:
        if arg.startswith("-"):
            continue
        candidate = Path(arg)
        if candidate.is_absolute() and not allow_external_args:
            raise ToolError("absolute shell arguments are not allowed")
        if ".." in candidate.parts and not allow_external_args:
            raise ToolError("shell arguments cannot escape workspace")

    completed = subprocess.run(
        parts,
        cwd=WORKSPACE_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout if timeout is not None else SHELL_TIMEOUT,
        check=False,
    )
    return {
        "ok": completed.returncode == 0,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


TOOLS: Dict[str, Callable[..., dict]] = {
    "write_file": write_file,
    "read_file": read_file,
    "list_dir": list_dir,
    "run_shell": run_shell,
}
