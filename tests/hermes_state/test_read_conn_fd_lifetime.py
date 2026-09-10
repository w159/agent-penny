"""Reader threads must not leak their read-only SQLite connections.

Regression guard for the 2026-08-17 outage: SessionDB._read_conns holds a
strong reference to every per-thread read connection, and nothing removed a
connection when its thread died. The gateway (a weeks-long process that
starts reader threads continuously) reached its 1024-fd ceiling with ~490
dead threads' connections still open; from 12:14 UTC every ConnectWise
callback and cron run failed with OSError: [Errno 24] Too many open files,
and Agent Penny went silent for a full business day with nothing surfaced to
Teams.
"""
import os
import threading

import pytest

from hermes_state import SessionDB

THREADS = 40


@pytest.fixture
def wal_db(tmp_path):
    """A SessionDB with the per-thread read path actually engaged.

    The read path is WAL-only, and some bundled SQLite builds fall back to
    journal_mode=DELETE on a fresh file (the WAL-reset corruption guard), so
    a plain fixture silently tests nothing. Force WAL and skip when the build
    refuses it.
    """
    database = SessionDB(tmp_path / "state.db")
    mode = database._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        database.close()
        pytest.skip("SQLite build refuses WAL; the per-thread read path cannot engage")
    database._wal_active = True
    try:
        yield database
    finally:
        database.close()


def _open_handles(db_path) -> int:
    """This process's open fds pointing at the database or its sidecars."""
    fd_dir = f"/proc/{os.getpid()}/fd"
    prefix = str(db_path)
    count = 0
    for fd in os.listdir(fd_dir):
        try:
            target = os.readlink(f"{fd_dir}/{fd}")
        except OSError:
            continue  # fd closed between listdir and readlink
        if target.startswith(prefix):
            count += 1
    return count


def _read_once(db):
    with db._read_ctx() as conn:
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc fd introspection")
def test_dead_reader_threads_release_their_connections(wal_db, tmp_path):
    baseline = _open_handles(tmp_path / "state.db")

    for i in range(THREADS):
        thread = threading.Thread(target=_read_once, args=(wal_db,), name=f"reader-{i}")
        thread.start()
        thread.join()

    leaked = _open_handles(tmp_path / "state.db") - baseline
    # Each leaked connection costs two fds (the db and its -wal sidecar), so
    # the old behavior showed ~2x THREADS here.
    assert leaked <= 2, f"{leaked} handles still held by {THREADS} dead threads"
    assert len(wal_db._read_conns) <= 1


def test_a_live_thread_still_reuses_one_connection(wal_db):
    """The fix must not turn the per-thread cache into a per-query open."""
    seen = []

    def reader():
        for _ in range(5):
            seen.append(id(wal_db._get_read_conn()))

    thread = threading.Thread(target=reader)
    thread.start()
    thread.join()

    assert len(set(seen)) == 1, "a thread reopened its read connection mid-life"


def test_reads_still_return_data_after_the_owning_thread_is_gone(wal_db):
    """Closing a dead thread's connection must not disturb live readers."""
    wal_db.create_session(session_id="s1", source="cli")
    wal_db.append_message(session_id="s1", role="user", content="hello")

    for _ in range(5):
        thread = threading.Thread(target=_read_once, args=(wal_db,))
        thread.start()
        thread.join()

    result = []

    def reader():
        with wal_db._read_ctx() as conn:
            row = conn.execute(
                "SELECT content FROM messages WHERE session_id = 's1'"
            ).fetchone()
            result.append(row[0])

    thread = threading.Thread(target=reader)
    thread.start()
    thread.join()

    assert result == ["hello"]


def test_close_still_drains_connections_of_live_threads(tmp_path):
    """close() remains the backstop for threads that outlive the SessionDB."""
    database = SessionDB(tmp_path / "state.db")
    mode = database._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        database.close()
        pytest.skip("SQLite build refuses WAL; the per-thread read path cannot engage")
    database._wal_active = True

    opened = threading.Event()
    release = threading.Event()

    def holder():
        database._get_read_conn()
        opened.set()
        release.wait(timeout=10)

    thread = threading.Thread(target=holder)
    thread.start()
    opened.wait(timeout=10)

    assert len(database._read_conns) == 1
    database.close()
    assert database._read_conns == set()

    release.set()
    thread.join(timeout=10)
