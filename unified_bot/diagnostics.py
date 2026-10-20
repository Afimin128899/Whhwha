"""Read-only adapters for the three legacy projects.

No Telegram user sessions are opened, copied, parsed or modified.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import sqlite3
from typing import BinaryIO
from urllib.parse import quote
from zipfile import BadZipFile, ZipFile


MAX_ARCHIVE_SIZE = 20 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2000


@dataclass(frozen=True)
class ArchiveSummary:
    session_files: int
    metadata_files: int
    tdata_entries: int
    other_files: int


def inspect_archive(data: bytes | BinaryIO) -> ArchiveSummary:
    """Count archive entry names only; never extract or read their contents."""
    if isinstance(data, bytes):
        if len(data) > MAX_ARCHIVE_SIZE:
            raise ValueError("Архив слишком большой (максимум 20 МиБ).")
        source: BinaryIO = BytesIO(data)
    else:
        source = data
        source.seek(0, 2)
        if source.tell() > MAX_ARCHIVE_SIZE:
            raise ValueError("Архив слишком большой (максимум 20 МиБ).")
        source.seek(0)

    try:
        with ZipFile(source) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise ValueError("В архиве слишком много элементов.")
            names = [entry.filename.replace("\\", "/") for entry in infos if not entry.is_dir()]
    except BadZipFile as exc:
        raise ValueError("Файл не является корректным ZIP-архивом.") from exc

    return ArchiveSummary(
        session_files=sum(name.lower().endswith(".session") for name in names),
        metadata_files=sum(name.lower().endswith(".json") for name in names),
        tdata_entries=sum("tdata/" in f"/{name.lower()}" for name in names),
        other_files=sum(not name.lower().endswith((".session", ".json")) and
                        "tdata/" not in f"/{name.lower()}" for name in names),
    )


def _readonly_connection(db_path: Path) -> sqlite3.Connection:
    # mode=ro means a missing database cannot silently be created.
    uri = "file:" + quote(str(db_path.resolve()), safe="/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=2)


def _group_counts(db_path: Path, table: str) -> dict[str, int] | None:
    if not db_path.is_file():
        return None
    try:
        with _readonly_connection(db_path) as con:
            rows = con.execute(f"SELECT status, COUNT(*) FROM {table} GROUP BY status").fetchall()
        return {str(status): int(count) for status, count in rows}
    except (sqlite3.Error, OSError):
        return None


def session_project_counts(db_path: Path) -> dict[str, int] | None:
    """Session project queue status counts (not session bodies or paths)."""
    return _group_counts(db_path, "queue")


def whhwha_project_counts(db_path: Path) -> dict[str, int] | None:
    """Whhwha project status counts (no user data or check URLs)."""
    return _group_counts(db_path, "sessions")


def jdjdj_inventory(directory: Path) -> dict[str, int] | None:
    """Local file-extension inventory without opening Telegram session files."""
    if not directory.is_dir():
        return None
    try:
        return {
            "session": sum(1 for p in directory.rglob("*.session") if p.is_file()),
            "zip": sum(1 for p in directory.rglob("*.zip") if p.is_file()),
            "tdata_dirs": sum(1 for p in directory.rglob("tdata") if p.is_dir()),
        }
    except OSError:
        return None


def _format_counts(counts: dict[str, int] | None) -> str:
    if counts is None:
        return "не подключён / нет доступных данных"
    if not counts:
        return "записей пока нет"
    return ", ".join(f"{key}: {value}" for key, value in sorted(counts.items()))


def build_audit_report(*, jdjdj_dir: Path | None, session_db: Path | None, whhwha_db: Path | None) -> str:
    return "\n".join([
        "📋 Диагностика проектов (только чтение)",
        f"Jdjdj · файлы: {_format_counts(jdjdj_inventory(jdjdj_dir) if jdjdj_dir else None)}",
        f"Session · очередь: {_format_counts(session_project_counts(session_db) if session_db else None)}",
        f"Whhwha · статусы: {_format_counts(whhwha_project_counts(whhwha_db) if whhwha_db else None)}",
        "",
        "Файлы авторизации не открывались, данные не изменялись.",
    ])