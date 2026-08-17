from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmark.questions import QuestionSpec, normalize_question, slugify
from benchmark.sql_runtime import CohortFingerprint

BATCH_CONTRACT_FILENAME = "batch_contract.json"
_CONTRACT_VERSION = 1
_CONTRACT_FIELDS = {
    "contract_version",
    "question_ids",
    "question_hashes",
    "targets",
    "result_files",
}
_TARGET_FIELDS = {
    "variant",
    "cohort_fingerprint",
    "variant_file_hashes",
    "manifest_digest",
}
_FINGERPRINT_FIELDS = {
    "baseline_dataset_id",
    "random_seed",
    "baseline_file_hashes",
    "temporal_snapshot_identity",
}
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class TargetBatchIdentity:
    """目标标签实际使用的变体身份。"""

    variant: str
    cohort_fingerprint: CohortFingerprint | None
    variant_file_hashes: tuple[tuple[str, str], ...] | None
    manifest_digest: str | None


@dataclass(frozen=True)
class GeneratedBatchContract:
    """将整批结果绑定到问题、目标变体和结果字节。"""

    question_ids: tuple[str, ...]
    question_hashes: tuple[tuple[str, str], ...]
    targets: tuple[tuple[str, TargetBatchIdentity], ...]
    result_files: tuple[tuple[str, str], ...]


def build_target_batch_identity(
    *,
    fallback_variant: str,
    cohort_fingerprint: CohortFingerprint | None,
    manifest: dict[str, Any],
    variant_dir: Path,
) -> TargetBatchIdentity:
    if manifest:
        variant = manifest.get("variant")
        if not isinstance(variant, str) or not variant.strip():
            raise ValueError("Generated batch target manifest variant is invalid")
        raw_hashes = manifest.get("variant_file_hashes")
        variant_file_hashes = _normalize_hashes(
            raw_hashes,
            "target variant_file_hashes",
        )
        manifest_digest = _json_digest(manifest)
    else:
        variant = fallback_variant
        variant_file_hashes = _hash_variant_inputs(variant_dir)
        manifest_digest = None
    if not isinstance(variant, str) or not variant.strip():
        raise ValueError("Generated batch target variant must be nonempty")
    return TargetBatchIdentity(
        variant=variant.strip(),
        cohort_fingerprint=_canonical_fingerprint(cohort_fingerprint),
        variant_file_hashes=variant_file_hashes,
        manifest_digest=manifest_digest,
    )


def write_generated_batch_contract(
    directory: Path,
    *,
    question_ids: list[str],
    question_specs: list[QuestionSpec],
    targets: list[tuple[str, TargetBatchIdentity]],
    result_files: list[Path],
) -> Path:
    normalized_question_ids = _normalize_unique_texts(question_ids, "question IDs")
    question_hashes = _question_hashes(question_specs)
    if set(normalized_question_ids) != {
        question_id for question_id, _ in question_hashes
    }:
        raise ValueError("Generated batch question IDs and specs must match exactly")
    contract = GeneratedBatchContract(
        question_ids=normalized_question_ids,
        question_hashes=question_hashes,
        targets=_normalize_targets(targets),
        result_files=_hash_result_files(directory, result_files),
    )
    payload = {
        "contract_version": _CONTRACT_VERSION,
        "question_ids": list(contract.question_ids),
        "question_hashes": dict(contract.question_hashes),
        "targets": {
            label: _target_to_json(identity) for label, identity in contract.targets
        },
        "result_files": dict(contract.result_files),
    }
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / BATCH_CONTRACT_FILENAME
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=directory,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # 与同批 CSV 保持可审计的共享读权限，目录 rename 后无需二次改写。
        os.chmod(temporary, 0o664)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def load_generated_batch_contract(directory: Path) -> GeneratedBatchContract:
    path = directory / BATCH_CONTRACT_FILENAME
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid generated batch contract JSON: {path}") from error
    if not isinstance(payload, dict):
        raise TypeError(f"Generated batch contract must be an object: {path}")
    if set(payload) != _CONTRACT_FIELDS:
        raise ValueError(
            "Generated batch contract fields must be exactly: "
            + ", ".join(sorted(_CONTRACT_FIELDS))
        )
    version = payload["contract_version"]
    if isinstance(version, bool) or version != _CONTRACT_VERSION:
        raise ValueError(
            f"Generated batch contract version must be {_CONTRACT_VERSION}"
        )

    raw_question_ids = payload["question_ids"]
    if not isinstance(raw_question_ids, list):
        raise TypeError("Generated batch contract question_ids must be an array")
    question_ids = _normalize_unique_texts(raw_question_ids, "question IDs")
    question_hashes = _normalize_digest_map(
        payload["question_hashes"], "question_hashes"
    )
    if set(question_ids) != {question_id for question_id, _ in question_hashes}:
        raise ValueError("Generated batch contract question IDs and hashes must match")

    raw_targets = payload["targets"]
    if not isinstance(raw_targets, dict):
        raise TypeError("Generated batch contract targets must be an object")
    targets = _normalize_targets(
        [(label, _target_from_json(value)) for label, value in raw_targets.items()]
    )
    result_files = _normalize_hashes(payload["result_files"], "result_files")
    if result_files is None:
        raise TypeError("Generated batch contract result_files must be an object")
    expected_result_files = {
        f"{slugify(question_id)}__{slugify(label)}.csv"
        for question_id in question_ids
        for label, _ in targets
    }
    if {filename for filename, _ in result_files} != expected_result_files:
        raise ValueError(
            "Generated batch contract result files must cover every question-target pair"
        )
    return GeneratedBatchContract(question_ids, question_hashes, targets, result_files)


def validate_generated_batch_contract(
    directory: Path,
    *,
    question_ids: list[str],
    question_specs: list[QuestionSpec],
    targets: list[tuple[str, TargetBatchIdentity]],
    strict: bool,
) -> GeneratedBatchContract | None:
    path = directory / BATCH_CONTRACT_FILENAME
    if not path.exists():
        if strict:
            raise FileNotFoundError(f"Missing generated batch contract: {path}")
        print(
            "Warning: generated batch contract is missing; "
            f"accepting legacy batch {path}"
        )
        return None
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Generated batch contract must be a regular file: {path}")

    actual = load_generated_batch_contract(directory)
    requested_questions = set(_normalize_unique_texts(question_ids, "question IDs"))
    if not requested_questions.issubset(actual.question_ids):
        raise ValueError(
            "Generated batch contract does not contain all requested question IDs"
        )
    actual_question_hashes = dict(actual.question_hashes)
    if any(
        actual_question_hashes.get(question_id) != digest
        for question_id, digest in _question_hashes(question_specs)
    ):
        raise ValueError(
            "Generated batch contract does not match requested question semantics"
        )

    expected_targets = _normalize_targets(targets)
    actual_labels = {label for label, _ in actual.targets}
    expected_labels = {label for label, _ in expected_targets}
    if not expected_labels.issubset(actual_labels):
        raise ValueError(
            "Generated batch contract target labels do not match evaluation"
        )
    actual_targets = dict(actual.targets)
    if any(
        actual_targets.get(label) != identity for label, identity in expected_targets
    ):
        raise ValueError(
            "Generated batch contract target variant, cohort fingerprint, or "
            "variant file hashes do not match current run"
        )
    _validate_result_files(directory, actual.result_files)
    if strict:
        _validate_result_csv_schemas(
            directory,
            question_specs=question_specs,
            target_labels=tuple(label for label, _ in expected_targets),
        )
    return actual


@contextmanager
def snapshot_generated_batch(
    directory: Path,
    *,
    snapshot_parent: Path,
    question_ids: list[str],
    question_specs: list[QuestionSpec],
    targets: list[tuple[str, TargetBatchIdentity]],
    strict: bool,
) -> Iterator[Path]:
    """校验并固定本次评估使用的生成结果，关闭目录替换竞态。"""

    contract = validate_generated_batch_contract(
        directory,
        question_ids=question_ids,
        question_specs=question_specs,
        targets=targets,
        strict=strict,
    )
    snapshot_parent.mkdir(parents=True, exist_ok=True)
    snapshot = Path(
        tempfile.mkdtemp(prefix=".generated-batch.snapshot-", dir=snapshot_parent)
    )
    try:
        if contract is None:
            # legacy 批次没有可核验的整批摘要，只复制当下可见的直接 CSV 子项；
            # 私有快照至少保证后续评估期间不会继续追随源目录变化。
            legacy_files = (
                sorted(
                    path
                    for path in directory.iterdir()
                    if path.suffix.casefold() == ".csv"
                    and path.is_file()
                    and not path.is_symlink()
                )
                if directory.is_dir()
                else []
            )
            for source in legacy_files:
                _copy_regular_file(source, snapshot / source.name)
        else:
            _copy_regular_file(
                directory / BATCH_CONTRACT_FILENAME,
                snapshot / BATCH_CONTRACT_FILENAME,
            )
            for filename, _ in contract.result_files:
                _copy_regular_file(directory / filename, snapshot / filename)

            copied_contract = load_generated_batch_contract(snapshot)
            if copied_contract != contract:
                raise ValueError(
                    "Generated batch changed while creating the evaluation snapshot"
                )
            # 二次完整校验同时覆盖精确文件集合、结果字节和当前评估语义。
            validate_generated_batch_contract(
                snapshot,
                question_ids=question_ids,
                question_specs=question_specs,
                targets=targets,
                strict=True,
            )
        yield snapshot
    finally:
        # 只删除本函数以随机名称创建的私有目录，不触碰源批次或旧评估输出。
        shutil.rmtree(snapshot, ignore_errors=True)


def _normalize_unique_texts(values: list[Any], field: str) -> tuple[str, ...]:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"Generated batch contract {field} must be nonempty strings")
    normalized = tuple(sorted(value.strip() for value in values))
    if not normalized or len(normalized) != len(set(normalized)):
        raise ValueError(
            f"Generated batch contract {field} must be nonempty and unique"
        )
    return normalized


def _normalize_targets(
    targets: list[tuple[str, TargetBatchIdentity]],
) -> tuple[tuple[str, TargetBatchIdentity], ...]:
    normalized: list[tuple[str, TargetBatchIdentity]] = []
    labels: list[str] = []
    for label, identity in targets:
        if not isinstance(identity, TargetBatchIdentity):
            raise TypeError("Generated batch contract target identity is invalid")
        labels.append(label)
        normalized.append((label.strip(), identity))
    _normalize_unique_texts(labels, "target labels")
    return tuple(sorted(normalized))


def _canonical_fingerprint(
    fingerprint: CohortFingerprint | None,
) -> CohortFingerprint | None:
    if fingerprint is None:
        return None
    return CohortFingerprint(
        baseline_dataset_id=fingerprint.baseline_dataset_id.casefold(),
        random_seed=fingerprint.random_seed,
        baseline_file_hashes=tuple(sorted(fingerprint.baseline_file_hashes)),
        temporal_snapshot_identity=tuple(fingerprint.temporal_snapshot_identity),
    )


def _target_to_json(identity: TargetBatchIdentity) -> dict[str, Any]:
    return {
        "variant": identity.variant,
        "cohort_fingerprint": _fingerprint_to_json(identity.cohort_fingerprint),
        "variant_file_hashes": (
            dict(identity.variant_file_hashes)
            if identity.variant_file_hashes is not None
            else None
        ),
        "manifest_digest": identity.manifest_digest,
    }


def _target_from_json(value: Any) -> TargetBatchIdentity:
    if not isinstance(value, dict) or set(value) != _TARGET_FIELDS:
        raise ValueError("Generated batch contract target fields are invalid")
    variant = value["variant"]
    if not isinstance(variant, str) or not variant.strip():
        raise ValueError("Generated batch contract target variant is invalid")
    return TargetBatchIdentity(
        variant=variant.strip(),
        cohort_fingerprint=_fingerprint_from_json(value["cohort_fingerprint"]),
        variant_file_hashes=_normalize_hashes(
            value["variant_file_hashes"],
            "target variant_file_hashes",
            allow_none=True,
        ),
        manifest_digest=_optional_digest(
            value["manifest_digest"], "target manifest_digest"
        ),
    )


def _fingerprint_to_json(
    fingerprint: CohortFingerprint | None,
) -> dict[str, Any] | None:
    if fingerprint is None:
        return None
    return {
        "baseline_dataset_id": fingerprint.baseline_dataset_id,
        "random_seed": fingerprint.random_seed,
        "baseline_file_hashes": dict(fingerprint.baseline_file_hashes),
        "temporal_snapshot_identity": [
            list(snapshot) for snapshot in fingerprint.temporal_snapshot_identity
        ],
    }


def _fingerprint_from_json(value: Any) -> CohortFingerprint | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _FINGERPRINT_FIELDS:
        raise ValueError(
            "Generated batch contract cohort fingerprint fields are invalid"
        )
    dataset_id = value["baseline_dataset_id"]
    if not isinstance(dataset_id, str) or _SHA256_PATTERN.fullmatch(dataset_id) is None:
        raise ValueError(
            "Generated batch contract cohort fingerprint dataset ID is invalid"
        )
    random_seed = value["random_seed"]
    if (
        isinstance(random_seed, bool)
        or not isinstance(random_seed, int)
        or random_seed < 0
    ):
        raise ValueError(
            "Generated batch contract cohort fingerprint random seed is invalid"
        )
    hashes = _normalize_hashes(
        value["baseline_file_hashes"],
        "cohort fingerprint baseline_file_hashes",
    )
    if hashes is None:
        raise TypeError(
            "Generated batch contract cohort fingerprint hashes must be an object"
        )
    raw_temporal = value["temporal_snapshot_identity"]
    if not isinstance(raw_temporal, list) or any(
        not isinstance(item, list)
        or len(item) != 3
        or any(not isinstance(part, str) or not part for part in item)
        for item in raw_temporal
    ):
        raise ValueError(
            "Generated batch contract temporal snapshot identity is invalid"
        )
    return CohortFingerprint(
        baseline_dataset_id=dataset_id,
        random_seed=random_seed,
        baseline_file_hashes=hashes,
        temporal_snapshot_identity=tuple(tuple(item) for item in raw_temporal),
    )


def _normalize_hashes(
    value: Any,
    field: str,
    *,
    allow_none: bool = False,
) -> tuple[tuple[str, str], ...] | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, dict) or not value:
        raise ValueError(f"Generated batch contract {field} must be a nonempty object")
    normalized: list[tuple[str, str]] = []
    for name, digest in value.items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or Path(name).name != name
            or not isinstance(digest, str)
            or _SHA256_PATTERN.fullmatch(digest) is None
        ):
            raise ValueError(f"Generated batch contract {field} is invalid")
        normalized.append((name, digest))
    return tuple(sorted(normalized))


def _normalize_digest_map(
    value: Any,
    field: str,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"Generated batch contract {field} must be a nonempty object")
    normalized: list[tuple[str, str]] = []
    for name, digest in value.items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(digest, str)
            or _SHA256_PATTERN.fullmatch(digest) is None
        ):
            raise ValueError(f"Generated batch contract {field} is invalid")
        normalized.append((name, digest))
    return tuple(sorted(normalized))


def _hash_result_files(
    directory: Path,
    result_files: list[Path],
) -> tuple[tuple[str, str], ...]:
    if not result_files:
        raise ValueError("Generated batch contract result files must not be empty")
    hashes: dict[str, str] = {}
    directory = directory.resolve()
    for path in result_files:
        resolved = path.resolve()
        if resolved.parent != directory or path.suffix.casefold() != ".csv":
            raise ValueError("Generated batch result file must be a direct CSV child")
        if path.name in hashes or not path.is_file() or path.is_symlink():
            raise ValueError(
                "Generated batch result files must be unique regular files"
            )
        hashes[path.name] = _sha256(path)
    return tuple(sorted(hashes.items()))


def _validate_result_files(
    directory: Path,
    expected_hashes: tuple[tuple[str, str], ...],
) -> None:
    expected = dict(expected_hashes)
    actual_files = {
        path.name
        for path in directory.iterdir()
        if path.name != BATCH_CONTRACT_FILENAME
    }
    if actual_files != set(expected):
        raise ValueError(
            "Generated batch result file set does not match the batch contract"
        )
    for filename, expected_digest in expected.items():
        path = directory / filename
        if not path.is_file() or path.is_symlink() or _sha256(path) != expected_digest:
            raise ValueError(
                f"Generated batch result hash does not match contract: {filename}"
            )


def _validate_result_csv_schemas(
    directory: Path,
    *,
    question_specs: list[QuestionSpec],
    target_labels: tuple[str, ...],
) -> None:
    for question in question_specs:
        for label in target_labels:
            filename = f"{slugify(question.question_id)}__{slugify(label)}.csv"
            _validate_result_csv_schema(
                directory / filename,
                entity_key=question.entity_key,
            )


def _validate_result_csv_schema(path: Path, *, entity_key: str) -> None:
    """在 DictReader 覆盖重复列之前验证生成结果的结构契约。"""

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        try:
            first_row = next(reader)
        except StopIteration:
            first_row = None

        fieldnames = reader.fieldnames
        if not fieldnames or any(not name.strip() for name in fieldnames):
            raise ValueError(
                f"Generated result CSV header names must be nonempty: {path}"
            )
        if len(fieldnames) != len(set(fieldnames)):
            raise ValueError(
                f"Generated result CSV header names must be unique: {path}"
            )
        if fieldnames.count(entity_key) != 1:
            raise ValueError(
                "Generated result CSV must contain entity key exactly once: "
                f"{entity_key}: {path}"
            )

        # DictReader 会把超出表头的单元格收进 None 键；若不在边界拒绝，
        # 下游按列名读取时会静默丢掉这些数据。
        if first_row is not None and None in first_row:
            raise ValueError(
                f"Generated result CSV row has more fields than the header: {path}"
            )
        for row in reader:
            if None in row:
                raise ValueError(
                    f"Generated result CSV row has more fields than the header: {path}"
                )


def _copy_regular_file(source: Path, destination: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(source, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Generated batch input must be a regular file: {source}")
        with os.fdopen(descriptor, "rb") as source_handle:
            descriptor = -1
            with destination.open("xb") as destination_handle:
                shutil.copyfileobj(source_handle, destination_handle)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _question_hashes(
    questions: list[QuestionSpec],
) -> tuple[tuple[str, str], ...]:
    hashes: dict[str, str] = {}
    for question in questions:
        if question.question_id in hashes:
            raise ValueError("Generated batch question IDs must be unique")
        weighting = question.weighting_policy
        temporal = question.temporal_evaluation
        payload = {
            "question": normalize_question(question.question),
            "institution_role": question.institution_role,
            "decision_type": question.decision_type,
            "entity_key": question.entity_key,
            "evaluation_title": question.evaluation_title,
            "reference_sql": question.reference_sql,
            "weighting_policy": (
                None
                if weighting is None
                else {
                    "policy_type": weighting.policy_type,
                    "default_weight": weighting.default_weight,
                    "bands": [
                        {"max_gpa": band.max_gpa, "weight": band.weight}
                        for band in weighting.bands
                    ],
                }
            ),
            "temporal_evaluation": (
                None
                if temporal is None
                else {
                    "current_reference_sql": temporal.current_reference_sql,
                    "replay_reference_sql": temporal.replay_reference_sql,
                    "snapshot": temporal.snapshot,
                }
            ),
            "remediated_causes": sorted(question.remediated_causes),
        }
        hashes[question.question_id] = _json_digest(payload)
    if not hashes:
        raise ValueError("Generated batch questions must not be empty")
    return tuple(sorted(hashes.items()))


def _hash_variant_inputs(
    variant_dir: Path,
) -> tuple[tuple[str, str], ...] | None:
    hashes = {
        path.name: _sha256(path)
        for path in variant_dir.glob("*.csv")
        if path.is_file() and not path.is_symlink()
    }
    if not hashes:
        # 仅测试替身可能没有真实目录；生产路径由 runtime 已先验证输入。
        return None
    return tuple(sorted(hashes.items()))


def _json_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _optional_digest(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"Generated batch contract {field} is invalid")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate key in generated batch contract: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    # JSON 常量入口只允许有限数值；NaN/Infinity 会破坏可重放比较。
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Non-finite value in generated batch contract: {value}")
    raise ValueError(f"Invalid value in generated batch contract: {value}")
