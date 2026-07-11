from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from .domain import ProposalCategory, canonical_json, new_id
from .settings import Settings


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.database_path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 5000")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            connection.close()
            raise RuntimeError("SQLite foreign-key enforcement could not be enabled")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        connection = self.connect()
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
            migration_root = resources.files("highlight_clipper.migrations")
            migrations = sorted(item for item in migration_root.iterdir() if item.name.endswith(".sql"))
            for migration in migrations:
                version_text, _, _ = migration.name.partition("_")
                version = int(version_text)
                if version in applied:
                    continue
                sql = migration.read_text(encoding="utf-8")
                escaped_name = migration.name.replace("'", "''")
                escaped_time = utc_now().replace("'", "''")
                try:
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n"
                        f"{sql}\n"
                        "INSERT INTO schema_migrations(version, name, applied_at) "
                        f"VALUES ({version}, '{escaped_name}', '{escaped_time}');\n"
                        "COMMIT;"
                    )
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
        finally:
            connection.close()

    def integrity_check(self) -> None:
        connection = self.connect()
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"SQLite integrity check failed: {result}")
        finally:
            connection.close()

    def ensure_default_profile(self) -> str:
        with self.transaction(immediate=True) as connection:
            current = connection.execute(
                "SELECT id FROM creator_profile_revisions ORDER BY revision_number DESC LIMIT 1"
            ).fetchone()
            if current:
                return str(current["id"])
            profile_id = new_id("profile")
            durations = {
                "reaction": [15, 60],
                "comedy": [20, 90],
                "story": [45, 180],
                "opinion": [30, 180],
                "explanation": [60, 240],
            }
            priorities = {category.value: 1 for category in ProposalCategory}
            connection.execute(
                "INSERT INTO creator_profile_revisions "
                "(id, revision_number, languages_json, category_priorities_json, "
                "desired_content, avoided_content, preferred_durations_json, created_at) "
                "VALUES (?, 1, ?, ?, '', '', ?, ?)",
                (
                    profile_id,
                    canonical_json(["fi", "en"]),
                    canonical_json(priorities),
                    canonical_json(durations),
                    utc_now(),
                ),
            )
            return profile_id

    def fetch_one(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Row | None:
        connection = self.connect()
        try:
            return connection.execute(sql, parameters).fetchone()
        finally:
            connection.close()

    def fetch_all(self, sql: str, parameters: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        connection = self.connect()
        try:
            return list(connection.execute(sql, parameters).fetchall())
        finally:
            connection.close()

    def backup_to(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(f"{destination.name}.partial")
        if partial.exists():
            partial.unlink()
        source = self.connect()
        target = sqlite3.connect(partial)
        try:
            source.backup(target, pages=256)
            target.execute("PRAGMA integrity_check")
            target.commit()
        finally:
            target.close()
            source.close()
        partial.replace(destination)

    @staticmethod
    def row_json(row: sqlite3.Row) -> dict[str, Any]:
        result: dict[str, Any] = dict(row)
        for key, value in tuple(result.items()):
            if key.endswith("_json") and isinstance(value, str):
                result[key.removesuffix("_json")] = json.loads(value)
                del result[key]
        return result
