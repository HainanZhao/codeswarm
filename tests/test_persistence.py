import asyncio
import glob
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeswarm import atomic
from codeswarm.db import DB


class AtomicWriteTests(unittest.TestCase):
    def test_content_is_flushed_to_disk_before_the_rename(self) -> None:
        """A rename is atomic; the bytes are not durable until they are synced."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "codeswarm.json")
            synced: list[int] = []
            real_fsync = os.fsync

            def record_fsync(fd: int) -> None:
                synced.append(fd)
                real_fsync(fd)

            with patch("codeswarm.atomic.os.fsync", side_effect=record_fsync):
                atomic.write(path, '{"ui": {}}')

            self.assertEqual(len(synced), 1)
            self.assertEqual(Path(path).read_text(), '{"ui": {}}')

    def test_a_failed_replace_does_not_leave_a_temporary_file_behind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # Replacing a file onto an existing directory always fails.
            path = os.path.join(directory, "codeswarm.json")
            os.mkdir(path)

            with self.assertRaises(atomic.AtomicWriteError):
                atomic.write(path, "{}")

            self.assertEqual(glob.glob(os.path.join(directory, ".*")), [])

    def test_replacing_a_file_preserves_its_existing_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "codeswarm.json")
            Path(path).write_text("{}")
            os.chmod(path, 0o644)

            atomic.write(path, '{"replaced": true}')

            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
            self.assertEqual(Path(path).read_text(), '{"replaced": true}')

    def test_a_new_file_is_created_private_to_the_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "codeswarm.json")

            atomic.write(path, "{}")

            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class SessionOrderingTests(unittest.TestCase):
    def test_owner_update_replaces_adapter_identity_and_preserves_metadata(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(os.environ, {"XDG_STATE_HOME": state_dir}):
                    db = DB()
                    self.assertTrue(await db.create())
                    session_pk = await db.session_new(
                        "workspace",
                        "Claude",
                        "claude.com",
                        "claude-session",
                        meta={"roster": ["claude.com", "openai.com"]},
                    )
                    assert session_pk is not None

                    self.assertTrue(
                        await db.session_update_owner(
                            session_pk,
                            agent="Codex",
                            agent_identity="openai.com",
                            agent_session_id="codex-session",
                            protocol="acp",
                            meta={
                                "roster": ["openai.com", "claude.com"],
                                "agent_data": {"identity": "openai.com"},
                            },
                        )
                    )
                    session = await db.session_get(session_pk)
                    assert session is not None
                    self.assertEqual(session["agent"], "Codex")
                    self.assertEqual(session["agent_identity"], "openai.com")
                    self.assertEqual(session["agent_session_id"], "codex-session")
                    self.assertEqual(session["protocol"], "acp")
                    self.assertEqual(
                        json.loads(session["meta_json"])["roster"],
                        ["openai.com", "claude.com"],
                    )

        asyncio.run(scenario())

    def test_recent_sessions_order_by_actual_time_across_timestamp_formats(
        self,
    ) -> None:
        """`last_used` is TEXT, so a mixed format sorts as a string, not a time."""

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(os.environ, {"XDG_STATE_HOME": state_dir}):
                    db = DB()
                    self.assertTrue(await db.create())

                    older = await db.session_new(
                        "older", "Agent", "test.agent", "session-old"
                    )
                    newer = await db.session_new(
                        "newer", "Agent", "test.agent", "session-new"
                    )
                    assert older is not None and newer is not None

                    # A legacy row written by an earlier version, plus rows
                    # left at the CURRENT_TIMESTAMP default.
                    async with db.open() as connection:
                        await connection.execute(
                            "UPDATE sessions SET last_used = ? WHERE id = ?",
                            ("2020-01-02T03:04:05.123456+00:00", older),
                        )
                        await connection.execute(
                            "UPDATE sessions SET last_used = ? WHERE id = ?",
                            ("2020-01-02 23:00:00", newer),
                        )
                        await connection.commit()

                    sessions = await db.session_get_recent()
                    assert sessions is not None
                    self.assertEqual(
                        [session["title"] for session in sessions],
                        ["newer", "older"],
                    )

        asyncio.run(scenario())

    def test_touching_a_session_makes_it_the_most_recent(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as state_dir:
                with patch.dict(os.environ, {"XDG_STATE_HOME": state_dir}):
                    db = DB()
                    self.assertTrue(await db.create())

                    first = await db.session_new(
                        "first", "Agent", "test.agent", "session-1"
                    )
                    await db.session_new("second", "Agent", "test.agent", "session-2")
                    assert first is not None

                    self.assertTrue(await db.session_update_last_used(first))

                    sessions = await db.session_get_recent()
                    assert sessions is not None
                    self.assertEqual(sessions[0]["title"], "first")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
