import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from sync_taco_data import load_taco_rows, open_taco_db, upsert_taco_rows  # noqa: E402


class TacoSyncTests(unittest.TestCase):
    def test_upsert_is_idempotent_and_updates_existing_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "taco_daily.sqlite"
            with open_taco_db(db_path) as conn:
                first = upsert_taco_rows(
                    conn,
                    [
                        {"date": "2026-06-10", "value": -1.0, "event_strength_score": 0.0, "raw": {"contributions": {"approval": 1.0}}},
                        {"date": "2026-06-11", "value": -2.0, "event_strength_score": 1.0, "raw": {"contributions": {"approval": 2.0}}},
                    ],
                )
                second = upsert_taco_rows(
                    conn,
                    [{"date": "2026-06-11", "value": -3.0, "event_strength_score": 1.0, "raw": {"contributions": {"approval": 3.0}}}],
                )
                count = conn.execute("SELECT COUNT(*) FROM taco_daily").fetchone()[0]

            rows = load_taco_rows(db_path)
            self.assertEqual(first, 2)
            self.assertEqual(second, 1)
            self.assertEqual(count, 2)
            self.assertEqual(rows[-1]["date"], "2026-06-11")
            self.assertEqual(rows[-1]["value"], -3.0)
            self.assertEqual(rows[-1]["contributions"]["approval"], 3.0)


if __name__ == "__main__":
    unittest.main()
