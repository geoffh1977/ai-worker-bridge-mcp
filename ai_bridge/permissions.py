from __future__ import annotations

import posixpath
import re
from functools import lru_cache
from pathlib import Path

import yaml

from .config import WorkerConfig
from .exceptions import InvalidWorkingDirectory, MissingWorkingDirectory

_FRONTMATTER_RE = re.compile(
    r"\A(?:\ufeff)?[ \t]*---[ \t]*\r?\n(?P<body>.*?)(?:\r?\n)[ \t]*---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)


def extract_working_directory(prompt: str) -> str | None:
    """Return ``working_directory`` from a leading YAML frontmatter block."""
    match = _FRONTMATTER_RE.match(prompt)
    if not match:
        return None
    try:
        frontmatter = yaml.safe_load(match.group("body")) or {}
    except yaml.YAMLError as exc:
        raise InvalidWorkingDirectory("<invalid frontmatter>", []) from exc
    if not isinstance(frontmatter, dict):
        raise InvalidWorkingDirectory("<invalid frontmatter>", [])
    value = frontmatter.get("working_directory")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidWorkingDirectory(str(value), [])
    return value.strip()


def resolve_working_directory(worker: WorkerConfig, prompt: str) -> str:
    """Require and validate a frontmatter working directory using declared worker policy."""
    requested = extract_working_directory(prompt)
    if requested is None:
        raise MissingWorkingDirectory()
    allowed_paths = worker.filesystem.write
    if not path_is_allowed(requested, allowed_paths, canonicalize=worker.filesystem.canonicalize):
        raise InvalidWorkingDirectory(requested, allowed_paths)
    return _normalize_for_dispatch(requested, canonicalize=worker.filesystem.canonicalize)


def path_is_allowed(candidate: str, allowed_paths: list[str], *, canonicalize: bool = False) -> bool:
    """Return True when candidate is an allowed path or subpath.

    Defaults are deny-all because production bridges should not grant root by accident.
    Lexical validation is retained for container-mismatched filesystems; canonical mode
    additionally resolves symlinks for paths visible to the bridge runtime.
    """
    normalized_candidate = _safe_normalize(candidate)
    if normalized_candidate is None:
        return False
    if canonicalize:
        return _canonical_path_is_allowed(normalized_candidate, allowed_paths)

    for allowed in allowed_paths:
        if not isinstance(allowed, str) or not allowed.startswith("/"):
            continue
        normalized_allowed = _safe_normalize(allowed)
        if normalized_allowed is None:
            continue
        if "*" in normalized_allowed:
            if _wildcard_prefix_matches(normalized_allowed, normalized_candidate):
                return True
            continue
        if normalized_allowed == "/":
            return True
        if normalized_candidate == normalized_allowed:
            return True
        if normalized_candidate.startswith(f"{normalized_allowed.rstrip('/')}/"):
            return True
    return False


def _canonical_path_is_allowed(candidate: str, allowed_paths: list[str]) -> bool:
    if "*" in candidate:
        return False
    try:
        resolved_candidate = Path(candidate).resolve(strict=False)
    except OSError:
        return False
    for allowed in allowed_paths:
        if not isinstance(allowed, str) or not allowed.startswith("/") or "*" in allowed:
            continue
        normalized_allowed = _safe_normalize(allowed)
        if normalized_allowed is None:
            continue
        try:
            resolved_allowed = Path(normalized_allowed).resolve(strict=False)
        except OSError:
            continue
        if resolved_allowed == Path("/"):
            return True
        if resolved_candidate == resolved_allowed or resolved_allowed in resolved_candidate.parents:
            return True
    return False


def _normalize_for_dispatch(path: str, *, canonicalize: bool) -> str:
    normalized = _safe_normalize(path)
    if normalized is None:
        return path
    if canonicalize:
        return str(Path(normalized).resolve(strict=False))
    return posixpath.normpath(normalized)


def _safe_normalize(path: str) -> str | None:
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    segments = [segment for segment in path.split("/") if segment]
    if ".." in segments:
        return None
    return posixpath.normpath(path)


@lru_cache(maxsize=1024)
def _wildcard_prefix_matches(pattern: str, candidate: str) -> bool:
    pattern_parts = tuple(part for part in pattern.split("/") if part)
    candidate_parts = tuple(part for part in candidate.split("/") if part)
    return _match_parts(pattern_parts, candidate_parts)


def _match_parts(pattern_parts: tuple[str, ...], candidate_parts: tuple[str, ...]) -> bool:
    if not pattern_parts:
        return True
    if not candidate_parts and any(part != "*" for part in pattern_parts):
        return False

    head, *tail = pattern_parts
    tail_tuple = tuple(tail)
    if head == "*":
        return any(_match_parts(tail_tuple, candidate_parts[index:]) for index in range(len(candidate_parts) + 1))
    if not candidate_parts or head != candidate_parts[0]:
        return False
    return _match_parts(tail_tuple, candidate_parts[1:])
