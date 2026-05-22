"""SWE-bench Pro task loading and deterministic task-list materialization."""

from __future__ import annotations

import hashlib
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

from controller.atomic import atomic_write_json, atomic_write_text


PUBLIC_DATASET_SOURCE = "ScaleAI/SWE-bench_Pro"
PUBLIC_DATASET_REVISION = "7ab5114912baf22bb098818e604c02fe7ad2c11f"
PUBLIC_DATASET_SPLIT = "test"
PUBLIC_DATASET_CONFIG = "default"
PUBLIC_DATASET_EXPECTED_ROWS = 731
PUBLIC_DATASET_DOC_URL = (
    "https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro/raw/"
    f"{PUBLIC_DATASET_REVISION}/README.md"
)
HF_ROWS_ENDPOINT = "https://datasets-server.huggingface.co/rows"

TASK_LIST_SCHEMA_VERSION = 1
TASK_LIST_HASH_ALGORITHM = "sha256"

REQUIRED_FIELDS: tuple[str, ...] = (
    "instance_id",
    "repo",
    "base_commit",
    "problem_statement",
    "fail_to_pass",
    "pass_to_pass",
)

CANONICAL_FIELD_ORDER: tuple[str, ...] = (
    "instance_id",
    "repo",
    "repo_url",
    "repo_key",
    "base_commit",
    "problem_statement",
    "fail_to_pass",
    "pass_to_pass",
    "patch",
    "test_patch",
    "requirements",
    "interface",
    "repo_language",
    "issue_specificity",
    "issue_categories",
    "before_repo_set_cmd",
    "selected_test_files_to_run",
    "dockerhub_tag",
    "source_index",
)


class TaskLoadError(RuntimeError):
    """Raised when a Pro task source cannot be loaded or validated."""


class HttpSession(Protocol):
    def get(self, url: str, *, params: dict[str, Any], timeout: float) -> Any: ...


@dataclass(frozen=True)
class DatasetMetadata:
    source: str = PUBLIC_DATASET_SOURCE
    revision: str = PUBLIC_DATASET_REVISION
    split: str = PUBLIC_DATASET_SPLIT
    config: str = PUBLIC_DATASET_CONFIG
    expected_row_count: int | None = PUBLIC_DATASET_EXPECTED_ROWS
    doc_url: str = PUBLIC_DATASET_DOC_URL
    loader: str = "huggingface-datasets-server-rows-api"

    def to_json(self, *, row_count: int, content_hash: str) -> dict[str, Any]:
        return {
            "source": self.source,
            "revision": self.revision,
            "split": self.split,
            "config": self.config,
            "expected_row_count": self.expected_row_count,
            "row_count": row_count,
            "content_hash_algorithm": TASK_LIST_HASH_ALGORITHM,
            "content_hash": content_hash,
            "doc_url": self.doc_url,
            "loader": self.loader,
        }


def canonicalize_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    canonical: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        canonical_row = _canonicalize_row(row, source_index=index)
        instance_id = canonical_row["instance_id"]
        if instance_id in seen:
            raise TaskLoadError(f"duplicate instance_id {instance_id!r}")
        seen.add(instance_id)
        canonical.append(canonical_row)
    ordered = sorted(canonical, key=lambda item: item["instance_id"])
    for canonical_index, row in enumerate(ordered):
        row["source_index"] = canonical_index
    return ordered


def load_fixture_rows(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    parsed = json.loads(text)
    if not isinstance(parsed, list):
        raise TaskLoadError("fixture task source must be a JSON array or JSONL file")
    return [row for row in parsed if isinstance(row, dict)]


def load_public_rows(
    *,
    session: HttpSession | None = None,
    page_size: int = 100,
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    if page_size <= 0:
        raise TaskLoadError("page_size must be positive")
    if session is None:
        try:
            import requests
        except (
            ImportError
        ) as exc:  # pragma: no cover - requests is optional at import time.
            raise TaskLoadError(
                "requests is required for public dataset loading"
            ) from exc

        session = requests.Session()

    rows: list[dict[str, Any]] = []
    offset = 0
    total: int | None = None
    while True:
        params: dict[str, Any] = {
            "dataset": PUBLIC_DATASET_SOURCE,
            "config": PUBLIC_DATASET_CONFIG,
            "split": PUBLIC_DATASET_SPLIT,
            "revision": PUBLIC_DATASET_REVISION,
            "offset": offset,
            "length": page_size,
        }
        response = session.get(
            HF_ROWS_ENDPOINT,
            params=params,
            timeout=timeout,
        )
        if getattr(response, "status_code", 200) != 200:
            raise TaskLoadError(
                f"Hugging Face rows API returned HTTP {getattr(response, 'status_code', 'unknown')}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise TaskLoadError("Hugging Face rows API returned a non-object payload")
        total = int(
            payload.get("num_rows_total") or payload.get("num_rows") or total or 0
        )
        page_rows = payload.get("rows")
        if not isinstance(page_rows, list):
            raise TaskLoadError("Hugging Face rows API payload is missing rows[]")
        for item in page_rows:
            if not isinstance(item, dict) or not isinstance(item.get("row"), dict):
                raise TaskLoadError(
                    "Hugging Face rows API returned a malformed row item"
                )
            rows.append(item["row"])
        offset += len(page_rows)
        if not page_rows or (total is not None and offset >= total):
            break
    if total is not None and len(rows) != total:
        raise TaskLoadError(f"loaded {len(rows)} rows but API reported {total}")
    return rows


def materialize_task_list(
    rows: Iterable[dict[str, Any]],
    *,
    output_path: Path,
    manifest_path: Path | None = None,
    dataset: DatasetMetadata | None = None,
    require_expected_count: bool = False,
) -> dict[str, Any]:
    metadata = dataset or DatasetMetadata()
    canonical = canonicalize_rows(rows)
    if (
        require_expected_count
        and metadata.expected_row_count is not None
        and len(canonical) != metadata.expected_row_count
    ):
        raise TaskLoadError(
            f"expected {metadata.expected_row_count} rows, loaded {len(canonical)}"
        )
    payload = serialize_task_rows(canonical)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    manifest = {
        "schema_version": TASK_LIST_SCHEMA_VERSION,
        "canonical_field_order": list(CANONICAL_FIELD_ORDER),
        "canonical_ordering": "rows sorted by instance_id; object keys sorted during serialization",
        "dataset": metadata.to_json(row_count=len(canonical), content_hash=digest),
    }
    atomic_write_text(output_path, payload)
    atomic_write_json(
        manifest_path or output_path.with_suffix(".manifest.json"), manifest
    )
    return manifest


def serialize_task_rows(rows: Iterable[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    )


def recompute_task_list_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_task_list(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _canonicalize_row(row: dict[str, Any], *, source_index: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise TaskLoadError(f"row {source_index} is not an object")
    missing = [field for field in REQUIRED_FIELDS if field not in row]
    if missing:
        raise TaskLoadError(
            f"row {source_index} is missing required fields: {', '.join(missing)}"
        )

    instance_id = _required_string(row, "instance_id", source_index)
    repo = _required_string(row, "repo", source_index)
    canonical = {
        "instance_id": instance_id,
        "repo": repo,
        "repo_url": _repo_url(row.get("repo_url"), repo),
        "repo_key": repo.replace("/", "__"),
        "base_commit": _required_string(row, "base_commit", source_index),
        "problem_statement": _required_string(row, "problem_statement", source_index),
        "fail_to_pass": _string_list(
            row.get("fail_to_pass"), "fail_to_pass", source_index
        ),
        "pass_to_pass": _string_list(
            row.get("pass_to_pass"), "pass_to_pass", source_index
        ),
        "patch": _optional_string(row.get("patch")),
        "test_patch": _optional_string(row.get("test_patch")),
        "requirements": _optional_string(row.get("requirements")),
        "interface": _optional_string(row.get("interface")),
        "repo_language": _optional_string(row.get("repo_language")),
        "issue_specificity": _optional_string(row.get("issue_specificity")),
        "issue_categories": _string_list(
            row.get("issue_categories"),
            "issue_categories",
            source_index,
            allow_missing=True,
        ),
        "before_repo_set_cmd": _optional_string(row.get("before_repo_set_cmd")),
        "selected_test_files_to_run": _string_list(
            row.get("selected_test_files_to_run"),
            "selected_test_files_to_run",
            source_index,
            allow_missing=True,
        ),
        "dockerhub_tag": _optional_string(row.get("dockerhub_tag")),
        "source_index": source_index,
    }
    return {field: canonical[field] for field in CANONICAL_FIELD_ORDER}


def _required_string(row: dict[str, Any], field: str, index: int) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise TaskLoadError(f"row {index} field {field!r} must be a non-empty string")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _repo_url(value: Any, repo: str) -> str:
    if isinstance(value, str) and value.strip():
        return value
    if (
        repo.startswith("http://")
        or repo.startswith("https://")
        or repo.endswith(".git")
    ):
        return repo
    return f"https://github.com/{repo}.git"


def _string_list(
    value: Any, field: str, index: int, *, allow_missing: bool = False
) -> list[str]:
    if value is None:
        if allow_missing:
            return []
        raise TaskLoadError(f"row {index} field {field!r} must be a list of strings")
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                try:
                    parsed = ast.literal_eval(stripped)
                except (SyntaxError, ValueError) as ast_exc:
                    raise TaskLoadError(
                        f"row {index} field {field!r} has invalid JSON/Python-list text"
                    ) from ast_exc
            return _string_list(parsed, field, index, allow_missing=allow_missing)
        return [stripped]
    if not isinstance(value, list):
        raise TaskLoadError(f"row {index} field {field!r} must be a list of strings")
    result = [str(item) for item in value]
    if not allow_missing and any(not item for item in result):
        raise TaskLoadError(f"row {index} field {field!r} contains an empty string")
    return result
