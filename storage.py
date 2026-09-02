"""
Livello di persistenza del bot.

Usa SQLite per salvare:
  - le ricerche Vinted monitorate (una o più per chat Telegram)
  - gli ID degli articoli già notificati, per non avvisare due volte
    lo stesso annuncio.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Search:
    id: int
    chat_id: int
    name: str
    url: str
    active: bool
    baseline_done: bool
    created_at: float


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS searches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    baseline_done INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS seen_items (
                    search_id INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    seen_at REAL NOT NULL,
                    PRIMARY KEY (search_id, item_id)
                );

                CREATE INDEX IF NOT EXISTS idx_seen_items_search
                    ON seen_items (search_id);
                """
            )
            conn.commit()
        finally:
            conn.close()

    # ---------------------------------------------------------------
    # Ricerche
    # ---------------------------------------------------------------

    def add_search(self, chat_id: int, name: str, url: str) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO searches (chat_id, name, url, active, baseline_done, created_at) "
                "VALUES (?, ?, ?, 1, 0, ?)",
                (chat_id, name, url, time.time()),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def remove_search(self, chat_id: int, search_id: int) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM searches WHERE id = ? AND chat_id = ?",
                (search_id, chat_id),
            )
            conn.execute("DELETE FROM seen_items WHERE search_id = ?", (search_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def set_active(self, chat_id: int, search_id: int, active: bool) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE searches SET active = ? WHERE id = ? AND chat_id = ?",
                (1 if active else 0, search_id, chat_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def list_searches(self, chat_id: int) -> list[Search]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM searches WHERE chat_id = ? ORDER BY id", (chat_id,)
            ).fetchall()
            return [self._row_to_search(r) for r in rows]
        finally:
            conn.close()

    def list_active_searches(self) -> list[Search]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM searches WHERE active = 1 ORDER BY id"
            ).fetchall()
            return [self._row_to_search(r) for r in rows]
        finally:
            conn.close()

    def mark_baseline_done(self, search_id: int) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE searches SET baseline_done = 1 WHERE id = ?", (search_id,)
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_search(row: sqlite3.Row) -> Search:
        return Search(
            id=row["id"],
            chat_id=row["chat_id"],
            name=row["name"],
            url=row["url"],
            active=bool(row["active"]),
            baseline_done=bool(row["baseline_done"]),
            created_at=row["created_at"],
        )

    # ---------------------------------------------------------------
    # Articoli già visti
    # ---------------------------------------------------------------

    def get_seen_ids(self, search_id: int, item_ids: list[str]) -> set[str]:
        if not item_ids:
            return set()
        conn = self._connect()
        try:
            placeholders = ",".join("?" for _ in item_ids)
            rows = conn.execute(
                f"SELECT item_id FROM seen_items "
                f"WHERE search_id = ? AND item_id IN ({placeholders})",
                (search_id, *item_ids),
            ).fetchall()
            return {r["item_id"] for r in rows}
        finally:
            conn.close()

    def mark_seen(self, search_id: int, item_ids: list[str]) -> None:
        if not item_ids:
            return
        conn = self._connect()
        try:
            now = time.time()
            conn.executemany(
                "INSERT OR IGNORE INTO seen_items (search_id, item_id, seen_at) "
                "VALUES (?, ?, ?)",
                [(search_id, iid, now) for iid in item_ids],
            )
            conn.commit()
        finally:
            conn.close()
