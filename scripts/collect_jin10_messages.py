#!/usr/bin/env python3
"""Backfill and incrementally collect Jin10 bot messages into SQLite."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from telethon import TelegramClient
from telethon.tl.types import Message

from _config import load_config

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback
    ZoneInfo = None


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
DEFAULT_DB_PATH = ROOT_DIR / "data" / "jin10_messages.sqlite"
DEFAULT_CHANNEL = "jinshishuju_bot"
DEFAULT_TG_API_ID = os.getenv("TG_API_ID", "94575")
DEFAULT_TG_API_HASH = os.getenv("TG_API_HASH", "a3406de8d171bb422bb6ddf3bbd800e2")
HK_TZ = ZoneInfo("Asia/Hong_Kong") if ZoneInfo is not None else timezone(timedelta(hours=8))


def parse_date_bound(value: str, *, is_end: bool) -> datetime:
    raw = value.strip()
    if not raw:
        raise ValueError("date bound cannot be empty")
    if len(raw) == 10:
        day = date.fromisoformat(raw)
        local = datetime.combine(day, time.min, tzinfo=HK_TZ)
        if is_end:
            local += timedelta(days=1)
        return local.astimezone(timezone.utc)
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=HK_TZ)
    return parsed.astimezone(timezone.utc)


def resolve_session_file(session_path: Optional[str]) -> Path:
    config = load_config()
    telegram = config.get("telegram", {}) if isinstance(config, dict) else {}
    raw = (
        session_path
        or os.getenv("TG_SESSION_PATH")
        or str(telegram.get("session_path") or "")
        or str(ROOT_DIR / "tg_session.session")
    )
    path = Path(raw).expanduser().resolve()
    if path.suffix != ".session":
        path = path.with_suffix(".session")
    if not path.exists():
        raise FileNotFoundError(f"Telegram session file not found: {path}")
    return path


def make_session_copy(session_file: Path) -> Path:
    target = session_file.with_name(f"tg_session_collect_{os.getpid()}_{uuid.uuid4().hex[:8]}.session")
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tg_session_collect_",
        suffix=".tmp",
        dir=str(session_file.parent),
    )
    os.close(fd)
    try:
        shutil.copy2(str(session_file), tmp_path)
        os.replace(tmp_path, str(target))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jin10_messages (
            channel TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            date_utc TEXT NOT NULL,
            date_hk TEXT NOT NULL,
            text TEXT NOT NULL,
            views INTEGER,
            raw_json TEXT NOT NULL,
            fetched_at_utc TEXT NOT NULL,
            PRIMARY KEY (channel, message_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jin10_messages_date_utc ON jin10_messages(date_utc)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jin10_collect_state (
            channel TEXT PRIMARY KEY,
            latest_message_id INTEGER,
            latest_message_date_utc TEXT,
            last_backfill_start_utc TEXT,
            last_backfill_end_utc TEXT,
            updated_at_utc TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _message_date(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _message_payload(message: Dict[str, Any], fetched_at: datetime) -> Dict[str, Any]:
    dt_utc = _message_date(message["date"])
    return {
        "message_id": int(message["id"]),
        "date_utc": dt_utc.isoformat(),
        "date_hk": dt_utc.astimezone(HK_TZ).isoformat(),
        "text": str(message.get("text") or ""),
        "views": message.get("views"),
        "raw_json": json.dumps(
            {
                "id": int(message["id"]),
                "date": dt_utc.isoformat(),
                "text": str(message.get("text") or ""),
                "views": message.get("views"),
            },
            ensure_ascii=False,
        ),
        "fetched_at_utc": fetched_at.astimezone(timezone.utc).isoformat(),
    }


def update_state(
    conn: sqlite3.Connection,
    channel: str,
    *,
    backfill_start_utc: Optional[str] = None,
    backfill_end_utc: Optional[str] = None,
) -> None:
    row = conn.execute(
        """
        SELECT message_id, date_utc
        FROM jin10_messages
        WHERE channel = ?
        ORDER BY message_id DESC
        LIMIT 1
        """,
        (channel,),
    ).fetchone()
    now_utc = datetime.now(timezone.utc).isoformat()
    latest_id = int(row[0]) if row else None
    latest_date = str(row[1]) if row else None
    conn.execute(
        """
        INSERT INTO jin10_collect_state (
            channel, latest_message_id, latest_message_date_utc,
            last_backfill_start_utc, last_backfill_end_utc, updated_at_utc
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel) DO UPDATE SET
            latest_message_id = excluded.latest_message_id,
            latest_message_date_utc = excluded.latest_message_date_utc,
            last_backfill_start_utc = COALESCE(excluded.last_backfill_start_utc, jin10_collect_state.last_backfill_start_utc),
            last_backfill_end_utc = COALESCE(excluded.last_backfill_end_utc, jin10_collect_state.last_backfill_end_utc),
            updated_at_utc = excluded.updated_at_utc
        """,
        (channel, latest_id, latest_date, backfill_start_utc, backfill_end_utc, now_utc),
    )
    conn.commit()


def upsert_messages(
    conn: sqlite3.Connection,
    channel: str,
    messages: Iterable[Dict[str, Any]],
    fetched_at: datetime,
) -> int:
    inserted = 0
    for message in messages:
        if not message.get("text"):
            continue
        payload = _message_payload(message, fetched_at)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO jin10_messages (
                channel, message_id, date_utc, date_hk, text, views, raw_json, fetched_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel,
                payload["message_id"],
                payload["date_utc"],
                payload["date_hk"],
                payload["text"],
                payload["views"],
                payload["raw_json"],
                payload["fetched_at_utc"],
            ),
        )
        inserted += int(cursor.rowcount == 1)
    conn.commit()
    update_state(conn, channel)
    return inserted


def latest_message_id(conn: sqlite3.Connection, channel: str) -> Optional[int]:
    row = conn.execute(
        "SELECT MAX(message_id) FROM jin10_messages WHERE channel = ?",
        (channel,),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def message_count(conn: sqlite3.Connection, channel: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM jin10_messages WHERE channel = ?",
        (channel,),
    ).fetchone()
    return int(row[0] or 0)


async def _connect_client(session_path: Optional[str]) -> tuple[TelegramClient, Path]:
    session_file = resolve_session_file(session_path)
    session_copy = make_session_copy(session_file)
    config = load_config()
    telegram_api = config.get("telegram_api", {}) if isinstance(config, dict) else {}
    api_id = int(os.getenv("TG_API_ID") or telegram_api.get("api_id") or DEFAULT_TG_API_ID)
    api_hash = str(os.getenv("TG_API_HASH") or telegram_api.get("api_hash") or DEFAULT_TG_API_HASH)
    client = TelegramClient(
        str(session_copy.with_suffix("")),
        api_id,
        api_hash,
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telegram session is not authorized. Run login.py first.")
    return client, session_copy


def _cleanup_session_copy(session_copy: Path) -> None:
    for suffix in (".session", ".session-journal"):
        path = session_copy.with_suffix(suffix)
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass


async def fetch_backfill_messages(
    *,
    channel: str,
    start_utc: datetime,
    end_utc: datetime,
    session_path: Optional[str],
    page_size: int,
) -> List[Dict[str, Any]]:
    client, session_copy = await _connect_client(session_path)
    try:
        entity = await client.get_entity(channel)
        messages: List[Dict[str, Any]] = []
        offset_date: Optional[datetime] = end_utc
        while True:
            batch: List[Dict[str, Any]] = []
            async for msg in client.iter_messages(entity, limit=page_size, offset_date=offset_date):
                if not (isinstance(msg, Message) and msg.text):
                    continue
                msg_date = msg.date.astimezone(timezone.utc)
                if msg_date < start_utc:
                    return messages
                if msg_date < end_utc:
                    batch.append(
                        {
                            "id": msg.id,
                            "date": msg_date,
                            "text": msg.text,
                            "views": msg.views,
                        }
                    )
            if not batch:
                return messages
            messages.extend(batch)
            offset_date = batch[-1]["date"]
            if len(batch) < page_size:
                return messages
    finally:
        await client.disconnect()
        _cleanup_session_copy(session_copy)


async def fetch_incremental_messages(
    *,
    channel: str,
    min_id: Optional[int],
    session_path: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    client, session_copy = await _connect_client(session_path)
    try:
        entity = await client.get_entity(channel)
        messages: List[Dict[str, Any]] = []
        # Process oldest unseen messages first so a bounded run never skips a gap.
        kwargs: Dict[str, Any] = {"limit": limit, "reverse": True}
        if min_id is not None:
            kwargs["min_id"] = min_id
        async for msg in client.iter_messages(entity, **kwargs):
            if not (isinstance(msg, Message) and msg.text):
                continue
            messages.append(
                {
                    "id": msg.id,
                    "date": msg.date.astimezone(timezone.utc),
                    "text": msg.text,
                    "views": msg.views,
                }
            )
        return messages
    finally:
        await client.disconnect()
        _cleanup_session_copy(session_copy)


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    init_db(conn)
    return conn


def run_backfill(args: argparse.Namespace) -> Dict[str, Any]:
    start_utc = parse_date_bound(args.start, is_end=False)
    end_utc = parse_date_bound(args.end, is_end=True)
    if end_utc <= start_utc:
        raise ValueError("--end must be after --start")

    messages = asyncio.run(
        fetch_backfill_messages(
            channel=args.channel,
            start_utc=start_utc,
            end_utc=end_utc,
            session_path=args.session,
            page_size=args.page_size,
        )
    )
    fetched_at = datetime.now(timezone.utc)
    with open_db(Path(args.db)) as conn:
        inserted = upsert_messages(conn, args.channel, messages, fetched_at)
        update_state(
            conn,
            args.channel,
            backfill_start_utc=start_utc.isoformat(),
            backfill_end_utc=end_utc.isoformat(),
        )
        total = message_count(conn, args.channel)
        latest_id = latest_message_id(conn, args.channel)
    return {
        "mode": "backfill",
        "channel": args.channel,
        "db": str(Path(args.db).resolve()),
        "window_start_utc": start_utc.isoformat(),
        "window_end_exclusive_utc": end_utc.isoformat(),
        "fetched": len(messages),
        "inserted": inserted,
        "total_rows": total,
        "latest_message_id": latest_id,
    }


def run_incremental(args: argparse.Namespace) -> Dict[str, Any]:
    with open_db(Path(args.db)) as conn:
        min_id = latest_message_id(conn, args.channel)
    messages = asyncio.run(
        fetch_incremental_messages(
            channel=args.channel,
            min_id=min_id,
            session_path=args.session,
            limit=args.limit,
        )
    )
    fetched_at = datetime.now(timezone.utc)
    with open_db(Path(args.db)) as conn:
        inserted = upsert_messages(conn, args.channel, messages, fetched_at)
        total = message_count(conn, args.channel)
        latest_id = latest_message_id(conn, args.channel)
    return {
        "mode": "incremental",
        "channel": args.channel,
        "db": str(Path(args.db).resolve()),
        "previous_latest_message_id": min_id,
        "fetched": len(messages),
        "inserted": inserted,
        "total_rows": total,
        "latest_message_id": latest_id,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect Jin10 Telegram bot messages into SQLite")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite output path")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL, help="Telegram channel/bot username")
    parser.add_argument("--session", default="", help="Telegram .session path")
    sub = parser.add_subparsers(dest="command", required=True)

    backfill = sub.add_parser("backfill", help="Backfill a date window")
    backfill.add_argument("--start", default="2026-04-18", help="Start date/time, date-only uses Asia/Hong_Kong")
    backfill.add_argument("--end", default="2026-06-17", help="End date/time, date-only is inclusive HK date")
    backfill.add_argument("--page-size", type=int, default=100, help="Telegram page size")

    incremental = sub.add_parser("incremental", help="Collect messages newer than the latest stored id")
    incremental.add_argument("--limit", type=int, default=500, help="Maximum new messages to scan")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.session == "":
        args.session = None
    if args.command == "backfill":
        result = run_backfill(args)
    elif args.command == "incremental":
        result = run_incremental(args)
    else:  # pragma: no cover
        parser.error(f"unknown command: {args.command}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
