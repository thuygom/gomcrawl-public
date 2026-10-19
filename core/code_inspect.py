from __future__ import annotations

from pathlib import Path
import re
from typing import Mapping

from .utils import compact_text


CODE_INSPECT_LEVELS = {
    "low": {
        "max_files": 12,
        "max_findings": 12,
        "checklist": ("obvious secrets", "dangerous execution", "missing network timeouts"),
    },
    "medium": {
        "max_files": 60,
        "max_findings": 30,
        "checklist": (
            "entrypoint and call-path risks",
            "secret leakage",
            "dangerous execution",
            "network timeout and retry gaps",
            "broad exception swallowing",
            "test coverage gaps for changed behavior",
        ),
    },
    "high": {
        "max_files": 200,
        "max_findings": 80,
        "checklist": (
            "entrypoint and call-path risks",
            "trust boundaries",
            "secret leakage",
            "dangerous execution",
            "network timeout and retry gaps",
            "broad exception swallowing",
            "data retention and logging",
            "concurrency and retry behavior",
            "contract and regression tests",
        ),
    },
}

LEVEL_ALIASES = {
    "하": "low",
    "낮음": "low",
    "light": "low",
    "shallow": "low",
    "중": "medium",
    "중간": "medium",
    "중간수준": "medium",
    "middle": "medium",
    "med": "medium",
    "상": "high",
    "높음": "high",
    "deep": "high",
    "thorough": "high",
}

CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".sh",
    ".bash",
    ".zsh",
}
EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".run",
    ".logs",
    ".venv",
    "__pycache__",
    "build",
    "crawler",
    "dist",
    "node_modules",
    "outputs",
    "venv",
}
MAX_FILE_BYTES = 250_000

SECRET_RE = re.compile(
    r"""(?ix)
    \b[\w.-]*(?:api[_-]?key|secret|token|password|passwd|credential)[\w.-]*\b
    [\w\s.-]{0,24}
    (?:=|:)\s*
    ["']([^"']{12,})["']
    """
)
HTTP_CALL_RE = re.compile(r"\b(?:httpx|requests)\.(?:get|post|put|patch|delete|request)\s*\(")
DANGEROUS_EXEC_RE = re.compile(r"\b(eval|exec)\s*\(")
SUBPROCESS_RE = re.compile(r"\bsubprocess\.(?:run|call|check_call|check_output|Popen)\s*\(")


def normalize_code_inspect_level(value: object = None) -> str:
    text = compact_text(value).lower().replace(" ", "")
    if not text:
        return "medium"
    return LEVEL_ALIASES.get(text, text if text in CODE_INSPECT_LEVELS else "medium")


def _profile(level: str) -> dict[str, object]:
    return CODE_INSPECT_LEVELS[normalize_code_inspect_level(level)]


def _safe_secret(value: str) -> bool:
    lowered = value.lower()
    if any(token in lowered for token in ("example", "sample", "placeholder", "xxxx", "your_", "dummy")):
        return True
    if set(value) <= {"x", "X", "*", "."}:
        return True
    return False


def _line_window(lines: list[str], index: int, width: int = 3) -> str:
    return " ".join(line.strip() for line in lines[index : index + width])


def _finding(
    *,
    severity: str,
    rule_id: str,
    path: str,
    line: int,
    problem: str,
    fix: str,
    evidence: str,
) -> dict[str, object]:
    return {
        "severity": severity,
        "rule_id": rule_id,
        "path": path,
        "line": line,
        "problem": problem,
        "fix": fix,
        "evidence": evidence[:240],
    }


def inspect_code_text(path: str, text: str, *, level: str = "medium") -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        line_number = index + 1
        stripped = line.strip()
        if not stripped:
            continue

        secret_match = SECRET_RE.search(stripped)
        if secret_match and not _safe_secret(secret_match.group(1)):
            findings.append(
                _finding(
                    severity="high",
                    rule_id="hardcoded-secret",
                    path=path,
                    line=line_number,
                    problem="Secret-like value appears hard-coded in source.",
                    fix="Load this value from env or a secret file and keep the real value out of git/logs.",
                    evidence=stripped,
                )
            )

        if "shell=True" in stripped and SUBPROCESS_RE.search(stripped):
            findings.append(
                _finding(
                    severity="high",
                    rule_id="subprocess-shell-true",
                    path=path,
                    line=line_number,
                    problem="Subprocess call uses shell=True.",
                    fix="Pass argv as a list and avoid shell expansion unless the command is fully controlled.",
                    evidence=stripped,
                )
            )

        if DANGEROUS_EXEC_RE.search(stripped):
            findings.append(
                _finding(
                    severity="high",
                    rule_id="dynamic-code-execution",
                    path=path,
                    line=line_number,
                    problem="Dynamic code execution is reachable.",
                    fix="Replace eval/exec with structured parsing or an explicit dispatch table.",
                    evidence=stripped,
                )
            )

        if HTTP_CALL_RE.search(stripped) and "timeout=" not in _line_window(lines, index):
            findings.append(
                _finding(
                    severity="medium",
                    rule_id="network-call-without-timeout",
                    path=path,
                    line=line_number,
                    problem="Network call does not show an explicit timeout.",
                    fix="Set a bounded timeout and handle retry/backoff at the caller boundary.",
                    evidence=stripped,
                )
            )

        if re.match(r"except\s*(Exception|BaseException)?\s*(?:as\s+\w+)?\s*:", stripped):
            window = _line_window(lines, index + 1, width=4)
            if re.search(r"\bpass\b", window) and "raise" not in window:
                findings.append(
                    _finding(
                        severity="medium",
                        rule_id="swallowed-exception",
                        path=path,
                        line=line_number,
                        problem="Broad exception handler appears to swallow failures.",
                        fix="Return a structured error, log safe metadata, or re-raise after cleanup.",
                        evidence=f"{stripped} {window}".strip(),
                    )
                )

        if normalize_code_inspect_level(level) == "high" and re.search(r"\b(TODO|FIXME)\b", stripped, re.I):
            findings.append(
                _finding(
                    severity="low",
                    rule_id="todo-marker",
                    path=path,
                    line=line_number,
                    problem="TODO/FIXME marker remains in inspected code.",
                    fix="Convert it to a tracked issue or finish the intended change.",
                    evidence=stripped,
                )
            )
    return findings


def _payload_files(payload: Mapping[str, object]) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    raw_files = payload.get("files")
    if isinstance(raw_files, list):
        for index, item in enumerate(raw_files):
            if isinstance(item, Mapping):
                text = item.get("content") or item.get("text") or item.get("code")
                name = item.get("path") or item.get("name") or f"payload_file_{index + 1}"
                if isinstance(text, str) and text.strip():
                    files.append({"path": str(name), "content": text})
            elif isinstance(item, str) and item.strip():
                files.append({"path": f"payload_file_{index + 1}", "content": item})
    code = payload.get("code")
    if isinstance(code, str) and code.strip():
        files.append({"path": str(payload.get("path") or "payload_code"), "content": code})
    return files


def inspect_code_payload(payload: Mapping[str, object]) -> dict[str, object]:
    requested_level = payload.get("inspection_level") or payload.get("inspect_level") or payload.get("level")
    level = normalize_code_inspect_level(requested_level)
    profile = _profile(level)
    files = _payload_files(payload)
    max_files = int(profile["max_files"])
    max_findings = int(profile["max_findings"])
    findings: list[dict[str, object]] = []
    for item in files[:max_files]:
        findings.extend(inspect_code_text(item["path"], item["content"], level=level))
        if len(findings) >= max_findings:
            findings = findings[:max_findings]
            break
    target = compact_text(
        payload.get("target")
        or payload.get("repository")
        or payload.get("repo")
        or payload.get("path")
        or payload.get("url")
        or payload.get("query")
    )
    return {
        "schema_version": "code_inspect/v1",
        "inspection_type": "code_inspect",
        "level": level,
        "requested_level": compact_text(requested_level) or "medium",
        "target": target,
        "input_status": "inspected" if files else "needs_code",
        "file_count": len(files[:max_files]),
        "truncated_files": max(0, len(files) - max_files),
        "finding_count": len(findings),
        "findings": findings,
        "checklist": list(profile["checklist"]),
        "report_contract": {
            "findings_first": True,
            "severity_order": ["critical", "high", "medium", "low"],
            "require_file_line": True,
            "include_test_gaps": level in {"medium", "high"},
        },
    }


def _iter_code_paths(paths: list[Path], *, max_files: int) -> list[Path]:
    selected: list[Path] = []
    for path in paths:
        if len(selected) >= max_files:
            break
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if len(selected) >= max_files:
                    break
                if any(part in EXCLUDED_DIRS for part in child.parts):
                    continue
                if child.is_file() and child.suffix in CODE_EXTENSIONS:
                    selected.append(child)
            continue
        if path.is_file() and path.suffix in CODE_EXTENSIONS:
            selected.append(path)
    return selected


def inspect_paths(paths: list[str], *, level: str = "medium") -> dict[str, object]:
    normalized_level = normalize_code_inspect_level(level)
    profile = _profile(normalized_level)
    max_files = int(profile["max_files"])
    max_findings = int(profile["max_findings"])
    selected = _iter_code_paths([Path(path) for path in paths], max_files=max_files)
    files: list[dict[str, str]] = []
    skipped: list[dict[str, object]] = []
    for path in selected:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                skipped.append({"path": str(path), "reason": "file too large"})
                continue
            files.append({"path": str(path), "content": path.read_text(encoding="utf-8", errors="replace")})
        except OSError as exc:
            skipped.append({"path": str(path), "reason": type(exc).__name__})
    result = inspect_code_payload({"files": files, "level": normalized_level, "target": ", ".join(paths)})
    result["skipped"] = skipped
    result["truncated_files"] = max(0, len(selected) - len(files)) + int(result.get("truncated_files") or 0)
    result["finding_count"] = min(int(result["finding_count"]), max_findings)
    result["findings"] = list(result["findings"])[:max_findings]
    return result
