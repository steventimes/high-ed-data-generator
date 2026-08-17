from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

_STAGING_MARKER = ".pipeline-staging-"
_BACKUP_MARKER = ".pipeline-backup-"


def prepare_run_staging(run_dir: str) -> Path:
    """校验正式目录，并在同一父目录创建整条流水线的暂存目录。"""
    destination = _validate_run_directory(run_dir, create_parent=True)
    staging = tempfile.mkdtemp(
        prefix=f".{destination.name}{_STAGING_MARKER}",
        dir=destination.parent,
    )
    return Path(staging)


def publish_staged_run(staging_dir: Path, run_dir: str) -> None:
    """原子发布完整运行批次；第二次 rename 失败时恢复旧目录。"""
    destination = _validate_run_directory(run_dir, create_parent=False)
    staging = _validate_staging_directory(staging_dir, destination)

    if not destination.exists():
        os.replace(staging, destination)
        return

    backup = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}{_BACKUP_MARKER}",
            dir=destination.parent,
        )
    )
    backup.rmdir()
    os.replace(destination, backup)
    try:
        os.replace(staging, destination)
    except OSError as publish_error:
        try:
            os.replace(backup, destination)
        except OSError as restore_error:
            raise RuntimeError(
                "Failed to publish the staged run and restore the previous run; "
                f"recoverable backup remains at {backup}: {restore_error}"
            ) from publish_error
        raise

    try:
        shutil.rmtree(backup)
    except OSError as error:
        # 新批次已经生效，备份清理失败只保留恢复线索，不能误报事务失败。
        print(
            "warning: run published successfully, but previous-run backup "
            f"cleanup failed; recoverable backup remains at {backup}: {error}",
            file=sys.stderr,
        )


def cleanup_staged_run(staging_dir: Path, run_dir: str) -> None:
    """只清理由本事务创建且仍位于目标同级的暂存目录。"""
    destination = _validate_run_directory(run_dir, create_parent=False)
    staging = Path(staging_dir)
    try:
        metadata = staging.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("pipeline staging path must be a real directory")
    validated = _validate_staging_directory(staging, destination)
    shutil.rmtree(validated)


def _validate_run_directory(run_dir: str, *, create_parent: bool) -> Path:
    if (
        not run_dir
        or run_dir in {".", "/", "//"}
        or "//" in run_dir
        or any(ord(character) < 32 for character in run_dir)
    ):
        raise ValueError("RUN_DIR must be a specific safe run directory")

    raw_path = Path(run_dir)
    if raw_path == Path(".") or raw_path == Path("/"):
        raise ValueError("RUN_DIR must be a specific safe run directory")
    if ".." in raw_path.parts:
        raise ValueError("RUN_DIR must be a specific safe run directory without '..'")
    absolute = raw_path if raw_path.is_absolute() else Path.cwd() / raw_path
    _reject_symlink_components(absolute)

    parent = absolute.parent
    if create_parent:
        parent.mkdir(parents=True, exist_ok=True)
        # mkdir 与后续解析之间再次检查，避免通过已存在的软链接父目录写出边界。
        _reject_symlink_components(parent)
    try:
        canonical_parent = parent.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError) as error:
        raise ValueError(f"RUN_DIR parent is not a directory: {parent}") from error

    destination = canonical_parent / absolute.name
    try:
        metadata = destination.lstat()
    except FileNotFoundError:
        return destination
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"RUN_DIR must not be a symbolic link: {destination}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"RUN_DIR exists but is not a directory: {destination}")
    return destination


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for index, part in enumerate(parts):
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"RUN_DIR path contains a symbolic link: {current}")
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"RUN_DIR parent is not a directory: {current}")


def _validate_staging_directory(staging_dir: Path, destination: Path) -> Path:
    staging = Path(staging_dir)
    try:
        metadata = staging.lstat()
    except FileNotFoundError as error:
        raise ValueError(
            f"pipeline staging directory does not exist: {staging}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("pipeline staging path must be a real directory")
    canonical = staging.resolve(strict=True)
    expected_prefix = f".{destination.name}{_STAGING_MARKER}"
    if canonical.parent != destination.parent or not canonical.name.startswith(
        expected_prefix
    ):
        raise ValueError("pipeline staging directory does not belong to RUN_DIR")
    return canonical


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish one complete benchmark run")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--run-dir", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("--run-dir", required=True)
    publish.add_argument("--staging-dir", type=Path, required=True)
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--run-dir", required=True)
    cleanup.add_argument("--staging-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.command == "prepare":
        print(prepare_run_staging(args.run_dir))
    elif args.command == "publish":
        publish_staged_run(args.staging_dir, args.run_dir)
    else:
        cleanup_staged_run(args.staging_dir, args.run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
