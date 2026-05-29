#!/usr/bin/env python3
"""
Analyze contested-primary expenditure CSVs to find top beneficiaries and top spenders.

Inputs:
  output/contested-primaries/by-committee/*.csv

Outputs:
  output/analysis/top-candidates.csv
	output/analysis/top-races.csv
  output/analysis/top-by-candidate/{Matched Candidate}.csv
  output/analysis/top-committees/overall.csv
  output/analysis/top-committees/democrats.csv
  output/analysis/top-committees/republicans.csv
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

SOURCE_DIR = Path("output/contested-primaries/by-committee")
ANALYSIS_DIR = Path("output/analysis")
TOP_BY_CANDIDATE_DIR = ANALYSIS_DIR / "top-by-candidate"
TOP_COMMITTEES_DIR = ANALYSIS_DIR / "top-committees"
TOP_CANDIDATES_FILE = ANALYSIS_DIR / "top-candidates.csv"
TOP_RACES_FILE = ANALYSIS_DIR / "top-races.csv"

TOP_CANDIDATE_FIELDS = ["candidate", "district", "party", "amount"]
TOP_RACE_FIELDS = ["district", "party", "amount"]
TOP_COMMITTEE_FIELDS = ["Committee", "amount", "numOfCandidates"]


def _safe_filename(name: str) -> str:
	cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
	cleaned = cleaned.strip(". ")
	return cleaned or "UNKNOWN"


def _parse_amount(value: str | None) -> Decimal:
	try:
		return Decimal((value or "0").replace(",", "").strip())
	except (InvalidOperation, AttributeError):
		return Decimal("0")


def _format_amount(value: Decimal) -> str:
	return format(value.quantize(Decimal("0.01")), "f")


def _party_buckets(party: str | None) -> set[str]:
	text = (party or "").upper()
	buckets: set[str] = set()
	if "DEM" in text:
		buckets.add("democrats")
	if "REP" in text:
		buckets.add("republicans")
	return buckets


def _split_multi_value(value: str | None) -> list[str]:
	parts = [part.strip() for part in (value or "").split("|")]
	return [part for part in parts if part]


def _explode_match_fields(row: dict) -> list[tuple[str, str, str]]:
	candidates = _split_multi_value(row.get("Matched Candidate"))
	districts = _split_multi_value(row.get("Matched District"))
	parties = _split_multi_value(row.get("Matched Party"))

	if not candidates:
		return []

	length = max(len(candidates), len(districts), len(parties), 1)
	exploded: list[tuple[str, str, str]] = []
	for index in range(length):
		candidate = candidates[index] if index < len(candidates) else candidates[-1]
		district = districts[index] if index < len(districts) else (districts[-1] if districts else "")
		party = parties[index] if index < len(parties) else (parties[-1] if parties else "")
		exploded.append((candidate, district, party))
	return exploded


def _clear_csvs(directory: Path) -> None:
	if not directory.exists():
		return
	for csv_file in directory.glob("*.csv"):
		csv_file.unlink()


def _load_rows() -> tuple[list[dict], list[str]]:
	rows: list[dict] = []
	fieldnames: list[str] = []

	for csv_file in sorted(SOURCE_DIR.glob("*.csv")):
		with open(csv_file, newline="", encoding="utf-8") as handle:
			reader = csv.DictReader(handle)
			if not fieldnames:
				fieldnames = list(reader.fieldnames or [])
			rows.extend(list(reader))

	return rows, fieldnames


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	with open(path, "w", newline="", encoding="utf-8") as handle:
		writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
		writer.writeheader()
		writer.writerows(rows)


def main() -> None:
	rows, source_fields = _load_rows()

	TOP_BY_CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
	TOP_COMMITTEES_DIR.mkdir(parents=True, exist_ok=True)
	_clear_csvs(TOP_BY_CANDIDATE_DIR)
	_clear_csvs(TOP_COMMITTEES_DIR)

	candidate_totals: dict[tuple[str, str, str], Decimal] = defaultdict(lambda: Decimal("0"))
	candidate_rows: dict[str, list[dict]] = defaultdict(list)

	committee_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
	committee_candidates: dict[str, set[str]] = defaultdict(set)

	bucket_totals: dict[str, dict[str, Decimal]] = {
		"democrats": defaultdict(lambda: Decimal("0")),
		"republicans": defaultdict(lambda: Decimal("0")),
	}
	bucket_candidates: dict[str, dict[str, set[str]]] = {
		"democrats": defaultdict(set),
		"republicans": defaultdict(set),
	}

	for row in rows:
		committee = (row.get("Committee") or "").strip() or "UNKNOWN"
		amount = _parse_amount(row.get("Amount"))

		committee_totals[committee] += amount
		exploded_matches = _explode_match_fields(row)

		for candidate, district, party in exploded_matches:
			committee_candidates[committee].add(candidate)
			candidate_key = (candidate, district, party)
			candidate_totals[candidate_key] += amount
			candidate_rows[candidate].append(row)

			for bucket in _party_buckets(party):
				bucket_totals[bucket][committee] += amount
				bucket_candidates[bucket][committee].add(candidate)

	top_candidates = [
		{
			"candidate": candidate,
			"district": district,
			"party": party,
			"amount": _format_amount(total),
		}
		for (candidate, district, party), total in candidate_totals.items()
	]
	top_candidates.sort(
		key=lambda row: (
			-Decimal(row["amount"]),
			row["candidate"],
			row["district"],
			row["party"],
		)
	)
	_write_csv(TOP_CANDIDATES_FILE, top_candidates, TOP_CANDIDATE_FIELDS)

	race_totals: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
	for (candidate, district, party), total in candidate_totals.items():
		race_totals[(district, party)] += total

	top_races = [
		{
			"district": district,
			"party": party,
			"amount": _format_amount(total),
		}
		for (district, party), total in race_totals.items()
	]
	top_races.sort(key=lambda row: (-Decimal(row["amount"]), row["district"], row["party"]))
	_write_csv(TOP_RACES_FILE, top_races, TOP_RACE_FIELDS)

	for candidate, candidate_file_rows in sorted(candidate_rows.items()):
		_write_csv(TOP_BY_CANDIDATE_DIR / f"{_safe_filename(candidate)}.csv", candidate_file_rows, source_fields)

	overall_rows = [
		{
			"Committee": committee,
			"amount": _format_amount(total),
			"numOfCandidates": str(len(committee_candidates.get(committee, set()))),
		}
		for committee, total in committee_totals.items()
	]
	overall_rows.sort(key=lambda row: (-Decimal(row["amount"]), row["Committee"]))
	_write_csv(TOP_COMMITTEES_DIR / "overall.csv", overall_rows, TOP_COMMITTEE_FIELDS)

	for bucket in ("democrats", "republicans"):
		bucket_rows = [
			{
				"Committee": committee,
				"amount": _format_amount(total),
				"numOfCandidates": str(len(bucket_candidates[bucket].get(committee, set()))),
			}
			for committee, total in bucket_totals[bucket].items()
		]
		bucket_rows.sort(key=lambda row: (-Decimal(row["amount"]), row["Committee"]))
		_write_csv(TOP_COMMITTEES_DIR / f"{bucket}.csv", bucket_rows, TOP_COMMITTEE_FIELDS)


if __name__ == "__main__":
	main()
