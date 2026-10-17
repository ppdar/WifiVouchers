"""Windows CSV roll-file reader.

Reads a voucher roll file using Windows conventions. The roll file contains
a "Roll <number>" header line and one quoted CSV voucher code per line.
"""

import csv
import re
from pathlib import Path


def read_csv_roll(roll_path):
    """Read a voucher roll CSV using Windows line handling.

    Returns (roll_number, voucher_codes). Raises ValueError when the roll
    number or any voucher codes cannot be read.
    """
    roll_number = None
    voucher_codes = []

    with Path(roll_path).open("r", encoding="utf-8-sig", newline="") as roll_file:
        for line in roll_file:
            if roll_number is None:
                match = re.search(r"\bRoll\s+(\d+)\b", line, re.IGNORECASE)
                if match is not None:
                    roll_number = int(match.group(1))

            if line.lstrip().startswith('"'):
                try:
                    row = next(csv.reader([line], skipinitialspace=True))
                except csv.Error as exc:
                    raise ValueError(
                        "The selected roll file contains invalid CSV."
                    ) from exc
                if row and row[0].strip():
                    voucher_codes.append(row[0].strip())

    if roll_number is None:
        raise ValueError("The selected roll file does not specify a roll number.")
    if not voucher_codes:
        raise ValueError("The selected roll file contains no voucher codes.")
    return roll_number, voucher_codes
