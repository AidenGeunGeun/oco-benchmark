"""Runtime production-fidelity boundary proof."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from controller.atomic import atomic_write_text


@dataclass(frozen=True)
class BoundaryConfig:
    protected_roots: tuple[Path, ...]
    allowed_roots: tuple[Path, ...]
    monitored_roots: tuple[Path, ...] = ()
    require_trace: bool = False


@dataclass(frozen=True)
class FileState:
    kind: str
    size: int
    mtime_ns: int


Manifest = dict[str, FileState]


@dataclass(frozen=True)
class RootChanges:
    root: Path
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.modified)


@dataclass(frozen=True)
class BoundaryProof:
    passed: bool
    protected_changes: tuple[RootChanges, ...]
    allowed_changes: tuple[RootChanges, ...]
    outside_changes: tuple[RootChanges, ...]
    trace_outside_writes: tuple[str, ...]
    trace_path: Path | None
    trace_missing: bool
    protected_roots: tuple[Path, ...]
    allowed_roots: tuple[Path, ...]
    monitored_roots: tuple[Path, ...]
    notes: tuple[str, ...] = ()


@dataclass
class BoundaryMonitor:
    config: BoundaryConfig
    before_protected: dict[Path, Manifest] = field(default_factory=dict)
    before_allowed: dict[Path, Manifest] = field(default_factory=dict)
    before_monitored: dict[Path, Manifest] = field(default_factory=dict)

    def start(self) -> None:
        self.before_protected = {
            root.resolve(): snapshot_tree(root) for root in self.config.protected_roots
        }
        self.before_allowed = {
            root.resolve(): snapshot_tree(root) for root in self.config.allowed_roots
        }
        self.before_monitored = {
            root.resolve(): snapshot_tree(root) for root in self.config.monitored_roots
        }

    def finish(self, artifact_path: Path) -> BoundaryProof:
        protected_changes = tuple(
            diff_manifests(root, before, snapshot_tree(root))
            for root, before in self.before_protected.items()
        )
        allowed_changes = tuple(
            diff_manifests(root, before, snapshot_tree(root))
            for root, before in self.before_allowed.items()
        )
        outside_changes = tuple(
            change
            for change in (
                classify_outside_changes(root, before, snapshot_tree(root), self.config)
                for root, before in self.before_monitored.items()
            )
            if change.changed
        )
        trace_path = artifact_path.parent / "filesystem-trace.log"
        trace_missing = self.config.require_trace and not trace_path.exists()
        trace_outside_writes = tuple(
            classify_trace_outside_writes(trace_path, self.config)
            if trace_path.exists()
            else []
        )
        notes = (
            "OCO subprocess HOME and XDG_CONFIG_HOME should point inside the attempt directory for real runs.",
            "This proof monitors configured roots, records allowed writes, and flags protected or out-of-allowed writes.",
            "When filesystem tracing is available, write-like syscalls are classified even outside monitored roots.",
        )
        proof = BoundaryProof(
            passed=(
                not any(change.changed for change in protected_changes)
                and not outside_changes
                and not trace_outside_writes
                and not trace_missing
            ),
            protected_changes=protected_changes,
            allowed_changes=allowed_changes,
            outside_changes=outside_changes,
            trace_outside_writes=trace_outside_writes,
            trace_path=trace_path if trace_path.exists() else None,
            trace_missing=trace_missing,
            protected_roots=tuple(
                root.resolve() for root in self.config.protected_roots
            ),
            allowed_roots=tuple(root.resolve() for root in self.config.allowed_roots),
            monitored_roots=tuple(
                root.resolve() for root in self.config.monitored_roots
            ),
            notes=notes,
        )
        write_boundary_proof(artifact_path, proof)
        return proof


def snapshot_tree(root: Path) -> Manifest:
    resolved = root.resolve()
    if not resolved.exists():
        return {}
    manifest: Manifest = {}
    for current, dirs, files in os.walk(resolved):
        current_path = Path(current)
        dirs[:] = [name for name in dirs if name not in {".git", "__pycache__"}]
        for directory in dirs:
            path = current_path / directory
            manifest[_relative(resolved, path)] = _state(path, "dir")
        for filename in files:
            path = current_path / filename
            try:
                manifest[_relative(resolved, path)] = _state(path, "file")
            except OSError:
                continue
    return manifest


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _state(path: Path, kind: str) -> FileState:
    stat = path.stat()
    return FileState(kind=kind, size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def diff_manifests(root: Path, before: Manifest, after: Manifest) -> RootChanges:
    before_keys = set(before)
    after_keys = set(after)
    added = tuple(sorted(after_keys - before_keys))
    removed = tuple(sorted(before_keys - after_keys))
    modified = tuple(
        sorted(key for key in before_keys & after_keys if before[key] != after[key])
    )
    return RootChanges(root=root, added=added, removed=removed, modified=modified)


def classify_outside_changes(
    root: Path, before: Manifest, after: Manifest, config: BoundaryConfig
) -> RootChanges:
    diff = diff_manifests(root, before, after)
    allowed = tuple(path.resolve() for path in config.allowed_roots)
    protected = tuple(path.resolve() for path in config.protected_roots)

    def outside(relative: str) -> bool:
        full_path = (root / relative).resolve()
        return not _under_any(full_path, allowed) and not _under_any(
            full_path, protected
        )

    return RootChanges(
        root=root,
        added=tuple(item for item in diff.added if outside(item)),
        removed=tuple(item for item in diff.removed if outside(item)),
        modified=tuple(item for item in diff.modified if outside(item)),
    )


def _under_any(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


WRITE_FLAG_PATTERN = re.compile(r"O_(?:WRONLY|RDWR|CREAT|TRUNC|APPEND)")
WRITE_SYSCALL_PATTERN = re.compile(
    r"\b(?:creat|mkdir|mkdirat|rename|renameat|renameat2|unlink|unlinkat|rmdir|symlink|symlinkat|link|linkat)\("
)
QUOTED_PATH_PATTERN = re.compile(r'"([^"]+)"')
IGNORED_TRACE_PREFIXES = ("/dev/", "/proc/", "/sys/")
TRACE_LINE_PATTERN = re.compile(
    r"^\s*(?:(?P<pid>\d+)\s+)?(?P<syscall>[A-Za-z_][\w]*)\((?P<args>.*)\)\s+=\s+(?P<result>.+?)\s*$"
)
FAILED_RESULT_PATTERN = re.compile(r"^-1\s+[A-Z][A-Z0-9_]*\b")
SUCCESS_FD_PATTERN = re.compile(r"^(?P<fd>\d+)\b")
FD_PATH_PATTERN = re.compile(r"^(?P<fd>\d+)<(?P<path>[^>]+)>$")


def classify_trace_outside_writes(
    trace_path: Path, config: BoundaryConfig
) -> list[str]:
    allowed = tuple(path.resolve() for path in config.allowed_roots)
    outside: set[str] = set()
    fd_dirs: dict[str, dict[int, Path]] = {}
    for line in trace_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        parsed = _parse_trace_line(line)
        if parsed is not None:
            pid, syscall, args, result = parsed
            _update_trace_fd_dirs(pid, syscall, args, result, fd_dirs)
            if _trace_syscall_failed(result):
                continue
        if not (WRITE_FLAG_PATTERN.search(line) or WRITE_SYSCALL_PATTERN.search(line)):
            continue
        if parsed is None:
            continue
        pid, syscall, args, _result = parsed
        for raw_path, parent in _trace_write_paths(pid, syscall, args, fd_dirs):
            if not raw_path.startswith("/"):
                if parent is None:
                    outside.add(f"RELATIVE:{raw_path}")
                    continue
                full_path = (parent / raw_path).resolve()
                if _under_any(full_path, allowed):
                    continue
                outside.add(str(full_path))
                continue
            if raw_path.startswith(IGNORED_TRACE_PREFIXES):
                continue
            full_path = Path(raw_path).resolve()
            if _under_any(full_path, allowed):
                continue
            outside.add(str(full_path))
    return sorted(outside)


def _parse_trace_line(line: str) -> tuple[str, str, str, str] | None:
    match = TRACE_LINE_PATTERN.match(line)
    if match is None:
        return None
    return (
        match.group("pid") or "",
        match.group("syscall"),
        match.group("args"),
        match.group("result"),
    )


def _trace_syscall_failed(result: str) -> bool:
    return FAILED_RESULT_PATTERN.match(result.strip()) is not None


def _update_trace_fd_dirs(
    pid: str,
    syscall: str,
    args: str,
    result: str,
    fd_dirs: dict[str, dict[int, Path]],
) -> None:
    if syscall == "close" and not _trace_syscall_failed(result):
        fd = _parse_fd_number(args.split(",", 1)[0].strip())
        if fd is not None:
            fd_dirs.setdefault(pid, {}).pop(fd, None)
        return
    if syscall not in {"open", "openat", "openat2"} or _trace_syscall_failed(result):
        return
    opened_fd = _successful_fd(result)
    if opened_fd is None:
        return
    if "O_DIRECTORY" not in args:
        fd_dirs.setdefault(pid, {}).pop(opened_fd, None)
        return
    raw_path, parent = _trace_open_path_and_parent(pid, syscall, args, fd_dirs)
    if raw_path is None:
        return
    if raw_path.startswith("/"):
        fd_dirs.setdefault(pid, {})[opened_fd] = Path(raw_path).resolve()
    elif parent is not None:
        fd_dirs.setdefault(pid, {})[opened_fd] = (parent / raw_path).resolve()


def _successful_fd(result: str) -> int | None:
    if _trace_syscall_failed(result):
        return None
    match = SUCCESS_FD_PATTERN.match(result.strip())
    if match is None:
        return None
    return int(match.group("fd"))


def _trace_open_path_and_parent(
    pid: str,
    syscall: str,
    args: str,
    fd_dirs: dict[str, dict[int, Path]],
) -> tuple[str | None, Path | None]:
    paths = QUOTED_PATH_PATTERN.findall(args)
    if not paths:
        return None, None
    if syscall == "open":
        return paths[0], None
    fd_token = args.split(",", 1)[0].strip()
    return paths[0], _resolve_trace_dirfd(pid, fd_token, fd_dirs)


def _trace_write_paths(
    pid: str,
    syscall: str,
    args: str,
    fd_dirs: dict[str, dict[int, Path]],
) -> list[tuple[str, Path | None]]:
    paths = QUOTED_PATH_PATTERN.findall(args)
    if not paths:
        return []
    if syscall in {"openat", "openat2", "mkdirat", "unlinkat"}:
        parent = _resolve_trace_dirfd(pid, args.split(",", 1)[0].strip(), fd_dirs)
        return [(paths[0], parent)]
    if syscall in {"renameat", "renameat2", "linkat"} and len(paths) >= 2:
        tokens = _split_trace_args(args)
        old_parent = _resolve_trace_dirfd(pid, tokens[0], fd_dirs) if tokens else None
        new_parent = (
            _resolve_trace_dirfd(pid, tokens[2], fd_dirs) if len(tokens) > 2 else None
        )
        return [(paths[0], old_parent), (paths[1], new_parent)]
    if syscall == "symlinkat" and len(paths) >= 2:
        tokens = _split_trace_args(args)
        parent = (
            _resolve_trace_dirfd(pid, tokens[1], fd_dirs) if len(tokens) > 1 else None
        )
        return [(paths[1], parent)]
    return [(path, None) for path in paths]


def _resolve_trace_dirfd(
    pid: str, fd_token: str, fd_dirs: dict[str, dict[int, Path]]
) -> Path | None:
    fd_token = fd_token.strip()
    if fd_token == "AT_FDCWD":
        return None
    fd_path = FD_PATH_PATTERN.match(fd_token)
    if fd_path is not None:
        return Path(fd_path.group("path")).resolve()
    fd = _parse_int(fd_token)
    if fd is None:
        return None
    return fd_dirs.get(pid, {}).get(fd)


def _parse_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _parse_fd_number(value: str) -> int | None:
    fd_path = FD_PATH_PATTERN.match(value.strip())
    if fd_path is not None:
        return int(fd_path.group("fd"))
    return _parse_int(value.strip())


def _split_trace_args(args: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    in_string = False
    escaped = False
    depth = 0
    for char in args:
        if in_string:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            current.append(char)
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}" and depth > 0:
            depth -= 1
        if char == "," and depth == 0:
            tokens.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    tokens.append("".join(current).strip())
    return tokens


def write_boundary_proof(path: Path, proof: BoundaryProof) -> None:
    lines = [
        "# Production-Fidelity Boundary Proof",
        "",
        f"Status: {'PASS' if proof.passed else 'FAIL'}",
        "",
        "## Protected Roots",
    ]
    for root, changes in zip(
        proof.protected_roots, proof.protected_changes, strict=True
    ):
        lines.extend(_format_changes(root, changes, violation=True))
    lines.extend(["", "## Allowed Roots"])
    for root, changes in zip(proof.allowed_roots, proof.allowed_changes, strict=True):
        lines.extend(_format_changes(root, changes, violation=False))
    lines.extend(["", "## Out-of-Bounds Changes"])
    if not proof.outside_changes:
        lines.append(
            "- Result: no writes outside allowed/protected roots within monitored roots"
        )
    for changes in proof.outside_changes:
        lines.extend(_format_changes(changes.root, changes, violation=True))
    lines.extend(["", "## Filesystem Trace"])
    if proof.trace_path is None:
        result = (
            "required trace missing" if proof.trace_missing else "trace not present"
        )
        lines.append(f"- Result: {result}")
    else:
        lines.append(f"- Trace: {proof.trace_path}")
        if proof.trace_outside_writes:
            lines.append("- Result: out-of-bounds write-like syscalls detected")
            for traced_path in proof.trace_outside_writes[:50]:
                lines.append(f"  - {traced_path}")
        else:
            lines.append("- Result: no out-of-bounds write-like syscalls detected")
    lines.extend(["", "## Monitored Roots"])
    for root in proof.monitored_roots:
        lines.append(f"- Root: {root}")
    lines.extend(["", "## Notes"])
    for note in proof.notes:
        lines.append(f"- {note}")
    atomic_write_text(path, "\n".join(lines) + "\n")


def _format_changes(root: Path, changes: RootChanges, *, violation: bool) -> list[str]:
    label = "violation" if violation else "allowed"
    lines = [f"- Root: {root}"]
    if not changes.changed:
        lines.append(f"  - Result: no changes ({label} set empty)")
        return lines
    lines.append(f"  - Result: changes detected ({label})")
    for field_name, values in (
        ("added", changes.added),
        ("removed", changes.removed),
        ("modified", changes.modified),
    ):
        if values:
            preview = ", ".join(values[:20])
            if len(values) > 20:
                preview += f", ... ({len(values)} total)"
            lines.append(f"  - {field_name}: {preview}")
    return lines


def default_real_boundary_config(
    *,
    run_root: Path,
    production_config_dir: Path,
    project_root: Path,
    repo_cache_dir: Path | None = None,
) -> BoundaryConfig:
    oco_source = project_root.parent / "OpenCodeOrchestra"
    monitored = _dedupe_roots(
        (
            run_root.parent,
            production_config_dir.parent,
            oco_source,
            production_config_dir,
        )
    )
    allowed = _dedupe_roots(
        (run_root,) if repo_cache_dir is None else (run_root, repo_cache_dir)
    )
    return BoundaryConfig(
        protected_roots=(oco_source, production_config_dir),
        allowed_roots=allowed,
        monitored_roots=monitored,
        require_trace=True,
    )


def _dedupe_roots(roots: tuple[Path, ...]) -> tuple[Path, ...]:
    seen: set[Path] = set()
    result: list[Path] = []
    for root in roots:
        resolved = root.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return tuple(result)


def ensure_roots_inside_project(roots: Iterable[Path], project_root: Path) -> bool:
    resolved_project = project_root.resolve()
    return all(root.resolve().is_relative_to(resolved_project) for root in roots)
