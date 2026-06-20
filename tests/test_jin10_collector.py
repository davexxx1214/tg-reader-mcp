import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from collect_jin10_messages import (  # noqa: E402
    init_db,
    latest_message_id,
    parse_date_bound,
    upsert_messages,
)


class Jin10CollectorTests(unittest.TestCase):
    def test_parse_date_bound_uses_hong_kong_day_window(self):
        start = parse_date_bound("2026-04-18", is_end=False)
        end = parse_date_bound("2026-06-17", is_end=True)

        self.assertEqual(start.isoformat(), "2026-04-17T16:00:00+00:00")
        self.assertEqual(end.isoformat(), "2026-06-17T16:00:00+00:00")

    def test_upsert_messages_is_idempotent_and_tracks_latest_id(self):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        fetched_at = datetime(2026, 6, 17, 14, 0, tzinfo=timezone.utc)
        messages = [
            {
                "id": 10,
                "date": datetime(2026, 6, 17, 13, 0, tzinfo=timezone.utc),
                "text": "测试消息",
                "views": 3,
            }
        ]

        first = upsert_messages(conn, "jinshishuju_bot", messages, fetched_at)
        second = upsert_messages(conn, "jinshishuju_bot", messages, fetched_at)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(latest_message_id(conn, "jinshishuju_bot"), 10)
        row = conn.execute(
            "SELECT date_hk, text FROM jin10_messages WHERE channel = ? AND message_id = ?",
            ("jinshishuju_bot", 10),
        ).fetchone()
        self.assertEqual(row, ("2026-06-17T21:00:00+08:00", "测试消息"))


if __name__ == "__main__":
    unittest.main()
