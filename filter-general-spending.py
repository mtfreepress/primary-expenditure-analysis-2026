#!/usr/bin/env python3
"""
Filter committee expenditure rows to the primary window for each cycle year.

For every file matching input/{year}/expenditures.csv, keep rows where Date Paid is:
  - on/after 10/01 of (year - 1)
  - on/before 05/31 of (year)

Writes:
  filtered/primary-spend/{year}.csv

Output files preserve the same columns as each input file.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

INPUT_ROOT = Path("input")
OUTPUT_ROOT = Path("filtered/primary-spend")


def _parse_mmddyyyy(value: str) -> date | None:
	text = (value or "").strip()
	if not text:
		return None

	parts = text.split("/")
	if len(parts) != 3:
		return None

	try:
		month = int(parts[0])
		day = int(parts[1])
		year = int(parts[2])
		return date(year, month, day)
	except ValueError:
		return None


def _filter_for_year(input_file: Path, year: int) -> tuple[int, int]:
	window_start = date(year - 1, 10, 1)
	window_end = date(year, 5, 31)

	OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
	output_file = OUTPUT_ROOT / f"{year}.csv"

	input_count = 0
	kept_count = 0

	with open(input_file, newline="", encoding="utf-8-sig") as src:
		reader = csv.DictReader(src)
		fieldnames = list(reader.fieldnames or [])

		with open(output_file, "w", newline="", encoding="utf-8") as dst:
			writer = csv.DictWriter(dst, fieldnames=fieldnames)
			writer.writeheader()

			for row in reader:
				input_count += 1
				paid_on = _parse_mmddyyyy(row.get("Date Paid", ""))
				if paid_on is None:
					continue
				if window_start <= paid_on <= window_end:
					writer.writerow(row)
					kept_count += 1

	return input_count, kept_count


def main() -> None:
	year_files: list[tuple[int, Path]] = []

	for path in sorted(INPUT_ROOT.glob("*/expenditures.csv")):
		try:
			year = int(path.parent.name)
		except ValueError:
			continue
		year_files.append((year, path))

	if not year_files:
		print("No files found matching input/{year}/expenditures.csv")
		return

	for year, input_file in year_files:
		total_rows, kept_rows = _filter_for_year(input_file, year)
		print(f"{year}: kept {kept_rows} of {total_rows} rows")


if __name__ == "__main__":
	main()
