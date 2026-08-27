from datetime import datetime, timezone
import json
from typing import cast, TypedDict
from codeswarm import paths

import aiosqlite

SQLITE_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
"""Matches SQLite's CURRENT_TIMESTAMP, which is UTC."""


def decode_session_meta(value: str | None) -> dict[str, object]:
    """Read a session metadata blob without letting a damaged record break UI flow."""
    if not value:
        return {}
    try:
        meta = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return meta if isinstance(meta, dict) else {}


class Session(TypedDict):
    """Agent session fields."""

    id: int
    """Primary key."""
    agent: str
    """Title of the agent."""
    agent_identity: str
    """Agent identity."""
    agent_session_id: str
    """Agent's session id."""
    title: str
    """Title of session."""
    protocol: str
    """Protocol used."""
    prompt_count: int
    """Number of prompts sent."""
    created_at: str
    """Time session was created."""
    last_used: str
    """Time sesison was last used."""
    meta_json: str
    """Text field containing JSON meta."""


class DB:
    """CodeSwarm database, for anything that isn't strictly configuration."""

    def __init__(self):
        self.path = paths.get_state() / "codeswarm.db"

    def open(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self.path)

    async def create(self) -> bool:
        """Create the tables if requried."""
        try:
            async with self.open() as db:
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        agent TEXT NOT NULL,
                        agent_identity TEXT NOT NULL,
                        agent_session_id TEXT NOT NULL,                                
                        title TEXT NOT NULL,      
                        protocol TEXT NOT NULL, 
                        prompt_count INTEGER DEFAULT 0,                
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        meta_json TEXT DEFAULT '{}'
                    )
                    """
                )
        except aiosqlite.Error:
            return False
        return True

    async def session_new(
        self,
        title: str,
        agent: str,
        agent_identity: str,
        agent_session_id: str,
        protocol: str = "acp",
        meta: dict[str, object] | None = None,
    ) -> int | None:
        meta_json = json.dumps(meta or {})
        try:
            async with self.open() as db:
                cursor = await db.execute(
                    """
                    INSERT INTO sessions (title, agent, agent_identity, agent_session_id, protocol, meta_json) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        title,
                        agent,
                        agent_identity,
                        agent_session_id,
                        protocol,
                        meta_json,
                    ),
                )
                await db.commit()
                return cursor.lastrowid
        except aiosqlite.Error:
            return None

    async def session_update_last_used(self, id: int) -> bool:
        """Update the last used timestamp.

        Args:
            id: Session ID.

        Returns:
            Boolenan that indicates success.
        """
        # Must match the CURRENT_TIMESTAMP default of the column: last_used is
        # TEXT and compared as a string, so a mixed format sorts incorrectly.
        now_utc = datetime.now(timezone.utc).strftime(SQLITE_TIMESTAMP_FORMAT)
        try:
            async with self.open() as db:
                await db.execute(
                    "UPDATE sessions SET last_used = ? WHERE id = ?",
                    (
                        now_utc,
                        id,
                    ),
                )
                await db.commit()
        except aiosqlite.Error:
            return False
        return True

    async def session_update_meta(self, id: int, meta: dict[str, object]) -> bool:
        """Replace the session's meta JSON blob.

        Args:
            id: Session ID.
            meta: New meta dict, serialized to JSON.

        Returns:
            Boolenan that indicates success.
        """
        try:
            async with self.open() as db:
                await db.execute(
                    "UPDATE sessions SET meta_json = ? WHERE id = ?",
                    (
                        json.dumps(meta),
                        id,
                    ),
                )
                await db.commit()
        except aiosqlite.Error:
            return False
        return True

    async def session_get(self, id: int) -> Session | None:
        """Get a sesison from its ID (PK).

        Args:
            session_id: The ID field (PK, not the agent_session_id)

        Returns:
            A Session if one is found, or `None`.
        """
        try:
            async with self.open() as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT * from sessions WHERE id = ?", (id,))
                row = await cursor.fetchone()
        except aiosqlite.Error:
            return None
        if row is None:
            return None
        session = cast(Session, dict(row))
        return session

    async def session_get_recent(self, max_results: int = 100) -> list[Session] | None:
        """Get the most recent sessions."""
        try:
            async with self.open() as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    # Rows written by earlier versions hold an ISO timestamp
                    # whose "T" separator sorts after a space. Normalize so a
                    # legacy row is still ordered by its actual time.
                    """SELECT * from sessions
                    ORDER BY replace(last_used, 'T', ' ') DESC
                    LIMIT ?""",
                    (max_results,),
                )
                rows = await cursor.fetchall()
        except aiosqlite.Error:
            return None
        sessions = [cast(Session, dict(row)) for row in rows]
        return sessions
