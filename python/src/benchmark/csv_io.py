from __future__ import annotations

import csv
import os
import secrets
import stat
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any


def write_csv_atomically(
    path: Path,
    rows: Iterable[dict[str, Any]],
    fieldnames: list[str],
    stringify: Callable[[Any], str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    descriptor, temporary_path = _create_temporary_file(path)
    if existing_mode is not None:
        os.fchmod(descriptor, existing_mode)
    try:
        # 临时文件与目标位于同一文件系统，完成后可用原子替换发布。
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: stringify(row.get(key)) for key in fieldnames})
        os.replace(temporary_path, path)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            # 清理失败不能覆盖真正的序列化或发布错误。
            pass
        raise


def _create_temporary_file(path: Path) -> tuple[int, Path]:
    for _ in range(100):
        temporary_path = path.parent / (f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            # O_EXCL 防止跟随已有路径；0o666 由进程 umask 收紧到正常输出权限。
            descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o666,
            )
        except FileExistsError:
            continue
        return descriptor, temporary_path
    raise FileExistsError(f"Could not allocate a temporary file for {path}")
