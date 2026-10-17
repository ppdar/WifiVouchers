import csv
import re
import sqlite3
from contextlib import closing
from pathlib import Path


class VoucherRollImporter:
    def __init__(self, database_path):
        self.database_path = Path(database_path)

    @classmethod
    def read_roll(cls, roll_path):
        """Read a voucher roll via the platform-specific CSV reader."""
        from .platform_dispatch import read_csv_roll

        roll_number, voucher_codes = read_csv_roll(roll_path)
        return roll_number, voucher_codes

    def _ensure_schema(self, connection):
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS vouchers (
                code TEXT NOT NULL,
                roll INTEGER NOT NULL,
                printout TEXT
            )
            """
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(vouchers)")
        }
        if "printout" not in columns:
            connection.execute("ALTER TABLE vouchers ADD COLUMN printout TEXT")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_vouchers_roll_code "
            "ON vouchers (roll, code)"
        )

    def import_roll(self, roll_path):
        roll_number, voucher_codes = self.read_roll(roll_path)

        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                self._ensure_schema(connection)
                changes_before_import = connection.total_changes
                connection.executemany(
                    """
                    INSERT INTO vouchers (code, roll, printout)
                    SELECT ?, ?, 'no'
                    WHERE NOT EXISTS (
                        SELECT 1 FROM vouchers WHERE code = ? AND roll = ?
                    )
                    """,
                    (
                        (code, roll_number, code, roll_number)
                        for code in voucher_codes
                    ),
                )
                imported_count = connection.total_changes - changes_before_import
                available_count = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM vouchers
                    WHERE COALESCE(printout, 'no') != 'yes'
                    """
                ).fetchone()[0]

        duplicate_count = len(voucher_codes) - imported_count
        return roll_number, imported_count, duplicate_count, available_count

    def get_next_voucher(self, roll_number):
        with closing(sqlite3.connect(self.database_path)) as connection:
            self._ensure_schema(connection)
            return connection.execute(
                """
                SELECT rowid, code
                FROM vouchers
                WHERE roll = ? AND COALESCE(printout, 'no') != 'yes'
                ORDER BY rowid
                LIMIT 1
                """,
                (roll_number,),
            ).fetchone()

    def get_available_count(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            self._ensure_schema(connection)
            return connection.execute(
                """
                SELECT COUNT(*)
                FROM vouchers
                WHERE COALESCE(printout, 'no') != 'yes'
                """
            ).fetchone()[0]

    def mark_printed(self, roll_number, voucher_id):
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                self._ensure_schema(connection)
                cursor = connection.execute(
                    """
                    UPDATE vouchers
                    SET printout = 'yes'
                    WHERE rowid = ? AND roll = ?
                      AND COALESCE(printout, 'no') != 'yes'
                    """,
                    (voucher_id, roll_number),
                )
                return cursor.rowcount == 1
