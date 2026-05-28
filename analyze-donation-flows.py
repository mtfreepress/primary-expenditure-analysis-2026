#!/usr/bin/env python3
"""
Build cashflow and contribution-only files.

Contribution routing in cashflows
──────────────────────────────────
In committee-contributions.csv the `Committee` field is the RECIPIENT and
`Entity Name` is the DONOR.  For cashflow analysis we want to show money
LEAVING each organisation, so:

  • Contributions where Entity Name fuzzy-matches a known committee
    → filed under Entity Name  (outflow from the donor)
  • Contributions where Entity Name is an individual or unknown org
    → filed under Committee    (inflow to the recipient)
  • Expenditures always filed under Committee (the spender), Amount negated.

Outputs
───────
1. output/cashflows-contested-primaries/by-committee/{committee}.csv
   Expenditures: rows from contested-primary output (by Committee, negated).
   Contributions: rows where Entity Name fuzzy-matches a contested-primary
                  committee (filed under Entity Name).
   Remaining contributions to contested-primary committees (individuals, etc.)
                  are filed under Committee as inflows.

2. output/cashflows-total/by-committee/{committee}.csv
   Same logic but for ALL expenditures and ALL contributions.

3. output/contributions-contested-primaries/{committee}.csv
   All contribution rows filed by their Committee field (the recipient),
   limited to contested-primary committees.

4. output/contributions-all-committees/{committee}.csv
   All contribution rows filed by their Committee field (the recipient).

Type column inserted after Committee: "contribution" or "expenditure".
Expenditure Amount values are negated (money out).
"""

from __future__ import annotations

import csv
import re
import shutil
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

INPUT_DIR = Path("input")
CONTRIBUTIONS_FILE = INPUT_DIR / "committee-contributions.csv"
EXPENDITURES_FILE = INPUT_DIR / "committee-expenditures.csv"
CONTESTED_PRIMARY_DIR = Path("output/contested-primaries/by-committee")

OUT_CF_CONTESTED = Path("output/cashflows-contested-primaries/by-committee")
OUT_CF_TOTAL = Path("output/cashflows-total/by-committee")
OUT_CONTRIB_CONTESTED = Path("output/contributions-contested-primaries")
OUT_CONTRIB_ALL = Path("output/contributions-all-committees")

# Fuzzy threshold for matching Entity Name against known committee names.
# 0.85 catches abbreviation differences and minor typos without false positives.
FUZZY_COMMITTEE_THRESHOLD = 0.85

# ── Column layout ─────────────────────────────────────────────────────────────

_SHARED_PREFIX = [
    "Committee", "Type", "Reporting Period", "Report Type", "Date Paid",
    "Entity Name", "First Name", "Middle Initial", "Last Name",
    "Addr Line1", "City", "State", "Zip", "Zip4", "Amount",
]

_CONTRIB_ONLY = [
    "Country", "Occupation", "Employer", "Contribution Type", "Amount Type",
    "Purpose", "Total To Date",
    "Refund Transaction Type", "Refund Original Transaction Date",
    "Refund Original Transaction Total", "Refund Original Transaction Descr",
    "Previous Transaction (Y/N)",
    "Fundraiser Name", "Fundraiser Location", "Fundraiser Attendees",
    "Fundraiser Tickets Sold", "Election Type",
    "Total Primary", "Total General", "type",
]

_EXPEND_ONLY = [
    "Expenditure Type", "Candidate Issue",
    "Expenditure Platform", "Expenditure Quantity",
    "Expenditure Specific Services", "Attachment",
    "# of Donors - Individuals", "Total $ - Individual",
    "# of Donors - Committees", "Total $ - Committee",
    "Expenditure Paid Communications Platform",
    "Expenditure Paid Communications Quantity",
    "Expenditure Paid Communications Subject Matter",
]

_CONTESTED_EXTRA = [
    "Matched Candidate", "Matched District", "Matched Party", "Matched Race",
]

FIELDS_CF_TOTAL = _SHARED_PREFIX + _CONTRIB_ONLY + _EXPEND_ONLY
FIELDS_CF_CONTESTED = FIELDS_CF_TOTAL + _CONTESTED_EXTRA
FIELDS_CONTRIBUTIONS = _SHARED_PREFIX + _CONTRIB_ONLY

# ── Helpers ───────────────────────────────────────────────────────────────────


def _safe_filename(name: str) -> str:
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return sanitized.strip(". ") or "UNKNOWN"


def _norm(name: str) -> str:
    """Uppercase, strip punctuation, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", "", name.upper())).strip()


def _negate_amount(row: dict) -> dict:
    out = dict(row)
    try:
        out["Amount"] = str(-abs(float(out["Amount"])))
    except (ValueError, TypeError):
        pass
    return out


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ── Committee name index + fuzzy lookup ───────────────────────────────────────


class CommitteeIndex:
    """
    Normalised committee-name lookup with fuzzy fallback.

    Build once from a collection of exact committee names.  Call .lookup(name)
    to resolve an arbitrary string to the canonical exact name, or None if no
    match meets the threshold.
    """

    def __init__(self, exact_names: set[str]) -> None:
        self._norm_to_exact: dict[str, str] = {_norm(c): c for c in exact_names}
        self._norms: list[str] = list(self._norm_to_exact)
        # Cache to avoid recomputing for identical inputs
        self._cache: dict[str, str | None] = {}

    def lookup(self, name: str) -> str | None:
        if not name.strip():
            return None
        if name in self._cache:
            return self._cache[name]
        key = _norm(name)
        # Exact normalised match
        if key in self._norm_to_exact:
            result = self._norm_to_exact[key]
            self._cache[name] = result
            return result
        # Fuzzy match
        best_r, best_norm = 0.0, ""
        for kn in self._norms:
            r = SequenceMatcher(None, key, kn).ratio()
            if r > best_r:
                best_r, best_norm = r, kn
        result = self._norm_to_exact[best_norm] if best_r >= FUZZY_COMMITTEE_THRESHOLD else None
        self._cache[name] = result
        return result

    @property
    def exact_names(self) -> set[str]:
        return set(self._norm_to_exact.values())


# ── Data loading ──────────────────────────────────────────────────────────────


def load_contributions() -> list[dict]:
    with open(CONTRIBUTIONS_FILE, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row["Type"] = "contribution"
    return rows


def load_expenditures() -> list[dict]:
    with open(EXPENDITURES_FILE, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row["Type"] = "expenditure"
    return rows


def load_contested_expenditures() -> list[dict]:
    rows: list[dict] = []
    for csv_file in sorted(CONTESTED_PRIMARY_DIR.glob("*.csv")):
        with open(csv_file, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                row["Type"] = "expenditure"
                rows.append(row)
    return rows


def build_all_committee_index(
    contributions: list[dict], expenditures: list[dict]
) -> CommitteeIndex:
    names: set[str] = set()
    for row in contributions:
        if c := row.get("Committee", "").strip():
            names.add(c)
    for row in expenditures:
        if c := row.get("Committee", "").strip():
            names.add(c)
    return CommitteeIndex(names)


def build_contested_committee_index(contested_rows: list[dict]) -> CommitteeIndex:
    names = {row["Committee"].strip() for row in contested_rows if row.get("Committee", "").strip()}
    return CommitteeIndex(names)


# ── Cashflow builder ──────────────────────────────────────────────────────────


def build_cashflows(
    contributions: list[dict],
    expenditures: list[dict],     # full expenditure set (only used when not contested mode)
    committee_index: CommitteeIndex,
    out_dir: Path,
    fieldnames: list[str],
    *,
    contested_exp_rows: list[dict] | None = None,
) -> tuple[int, int, int]:
    """
    Write cashflow CSVs.

    Expenditures are filed under their Committee (spender), amount negated.
    Contributions are filed under Entity Name if Entity Name fuzzy-matches a
    known committee (donor outflow view); otherwise under Committee (recipient
    inflow view — covers individual donors and unrecognised orgs).

    Returns (n_files, n_exp_rows, n_contrib_rows).
    """
    by_committee: dict[str, list[dict]] = defaultdict(list)

    # ── Expenditures ──────────────────────────────────────────────────────────
    exp_source = contested_exp_rows if contested_exp_rows is not None else expenditures
    for row in exp_source:
        key = row.get("Committee", "UNKNOWN").strip() or "UNKNOWN"
        by_committee[key].append(_negate_amount(row))
    n_exp = sum(len(v) for v in by_committee.values())

    # ── Contributions ─────────────────────────────────────────────────────────
    # In contested mode: only include contributions where Entity Name is a
    # contested-primary committee (outflow from that PAC to whoever received it).
    # Inflows from individuals or non-contested orgs are excluded — those belong
    # in cashflows-total, not the focused contested-primary view.
    contested_mode = contested_exp_rows is not None
    n_contrib = 0
    n_rerouted = 0
    for row in contributions:
        entity_name = row.get("Entity Name", "").strip()
        matched = committee_index.lookup(entity_name)
        if matched:
            # Entity Name is a known committee → file under the DONOR (Entity Name)
            # `matched` is already the canonical name from the index
            by_committee[matched].append(row)
            n_rerouted += 1
            n_contrib += 1
        elif not contested_mode:
            # Not contested mode: individual donor or unrecognised org →
            # file as inflow under the recipient (Committee field)
            raw_key = row.get("Committee", "UNKNOWN").strip() or "UNKNOWN"
            key = committee_index.lookup(raw_key) or raw_key
            by_committee[key].append(row)
            n_contrib += 1

    for committee, rows in sorted(by_committee.items()):
        _write_csv(out_dir / f"{_safe_filename(committee)}.csv", rows, fieldnames)

    print(
        f"  {len(by_committee)} files | "
        f"{n_exp} expenditure rows | "
        f"{n_contrib} contribution rows ({n_rerouted} rerouted to donor file)"
    )
    return len(by_committee), n_exp, n_contrib


# ── Contribution-only builder ─────────────────────────────────────────────────


def build_contributions_by_committee(
    contributions: list[dict],
    out_dir: Path,
    *,
    filter_index: CommitteeIndex | None = None,
) -> tuple[int, int]:
    """
    Write contribution-only CSVs filed by Committee (the recipient).

    If filter_index is given, only write files for committees in that index
    (matched fuzzily against the Committee field).
    """
    by_committee: dict[str, list[dict]] = defaultdict(list)
    for row in contributions:
        committee = row.get("Committee", "UNKNOWN").strip() or "UNKNOWN"
        if filter_index is not None:
            # Resolve the committee name through the index so the filename
            # matches the canonical name used elsewhere.
            canonical = filter_index.lookup(committee)
            if canonical is None:
                continue
            committee = canonical
        by_committee[committee].append(row)

    for committee, rows in sorted(by_committee.items()):
        _write_csv(out_dir / f"{_safe_filename(committee)}.csv", rows, FIELDS_CONTRIBUTIONS)

    n_rows = sum(len(v) for v in by_committee.values())
    print(f"  {len(by_committee)} files | {n_rows} contribution rows")
    return len(by_committee), n_rows


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    # Wipe all output directories so stale files from previous runs are removed.
    for out_dir in (OUT_CF_CONTESTED, OUT_CF_TOTAL, OUT_CONTRIB_CONTESTED, OUT_CONTRIB_ALL):
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

    print("Loading data...", flush=True)
    contributions = load_contributions()
    expenditures = load_expenditures()
    contested_exp = load_contested_expenditures()
    print(
        f"  {len(contributions)} contributions | "
        f"{len(expenditures)} expenditures | "
        f"{len(contested_exp)} contested-primary expenditures across "
        f"{len({r['Committee'] for r in contested_exp})} committees"
    )

    all_idx = build_all_committee_index(contributions, expenditures)
    contested_idx = build_contested_committee_index(contested_exp)
    print(
        f"  {len(all_idx.exact_names)} known committees total | "
        f"{len(contested_idx.exact_names)} contested-primary committees"
    )

    print("\nBuilding cashflows-contested-primaries...", flush=True)
    build_cashflows(
        contributions, expenditures,
        contested_idx,
        OUT_CF_CONTESTED,
        FIELDS_CF_CONTESTED,
        contested_exp_rows=contested_exp,
    )

    print("Building cashflows-total...", flush=True)
    build_cashflows(
        contributions, expenditures,
        all_idx,
        OUT_CF_TOTAL,
        FIELDS_CF_TOTAL,
    )

    print("Building contributions-contested-primaries...", flush=True)
    build_contributions_by_committee(
        contributions,
        OUT_CONTRIB_CONTESTED,
        filter_index=contested_idx,
    )

    print("Building contributions-all-committees...", flush=True)
    build_contributions_by_committee(
        contributions,
        OUT_CONTRIB_ALL,
    )

    print("\nDone.")


if __name__ == "__main__":
    main()
