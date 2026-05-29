#!/usr/bin/env python3
"""
Analyze Montana primary expenditures to identify spending on contested primary races.

Reads:
  input/committee-expenditures.csv
  input/contested-primaries-candidates.csv

Writes:
  output/contested-primaries/by-committee/{Committee}.csv
  One file per committee; every row that references ≥1 contested-primary candidate.
  Original fields are preserved plus four appended columns:
    Matched Candidate | Matched District | Matched Party | Matched Race

    output/all-contested-primary.csv
    A single consolidated file containing all matched contested-primary rows.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

INPUT_DIR = Path("input")
OUTPUT_DIR = Path("output/contested-primaries/by-committee")
ALL_CONTESTED_FILE = Path("output/all-contested-primary.csv")
EXPENDITURES_FILE = INPUT_DIR / "committee-expenditures.csv"
CANDIDATES_FILE = INPUT_DIR / "contested-primaries-candidates.csv"

# Minimum similarity ratio for fuzzy last-name matching (typo tolerance).
# 0.70 catches transposition typos (BUTTERY/BUTTREY) and near-misses (STANKEY/STANEK).
FUZZY_LAST_THRESHOLD = 0.70
# Minimum similarity ratio for fuzzy full-name matching (disambiguation)
FUZZY_FULL_THRESHOLD = 0.75

# ── Normalization helpers ─────────────────────────────────────────────────────


def _norm(text: str) -> str:
    """Strip, uppercase, strip leading asterisks used for incumbents."""
    return re.sub(r"\*", "", text or "").strip().upper()


def _norm_name(text: str) -> str:
    """Uppercase, keep only alpha + spaces, collapse whitespace."""
    t = _norm(text)
    t = re.sub(r"[^A-Z\s]", "", t)
    return re.sub(r"\s+", " ", t).strip()


# ── District parsing ──────────────────────────────────────────────────────────

# Matches: HD-28, HD28, HD 28, House District 28, House 28
#          SD-9,  SD9,  SD 9,  Senate District 9,  Senate 9
_DIST_RE = re.compile(
    r"\b(?:house\s+(?:district\s*)?|hd\s*[-\s]?)(\d+)"
    r"|\b(?:senate\s+(?:district\s*)?|sd\s*[-\s]?)(\d+)",
    re.IGNORECASE,
)


def _extract_district(text: str) -> tuple[str | None, str]:
    """
    Find the first district code in text.
    Returns (canonical_key, text_with_district_removed).
    canonical_key: 'HD28', 'SD9', or None.
    """
    m = _DIST_RE.search(text)
    if not m:
        return None, text
    hd_num, sd_num = m.group(1), m.group(2)
    key = f"HD{int(hd_num)}" if hd_num else f"SD{int(sd_num)}"
    cleaned = (text[: m.start()] + " " + text[m.end() :]).strip(" (),;")
    return key, cleaned


def _dist_key_from_field(district: str) -> str:
    """
    Convert the 'District' field from contested-primaries-candidates.csv
    to a canonical key: 'HD28', 'SD9', 'STATE', '1ST CONGRESSIONAL', etc.
    """
    d = district.strip().upper()
    m = re.match(r"HOUSE DISTRICT (\d+)", d)
    if m:
        return f"HD{int(m.group(1))}"
    m = re.match(r"SENATE DISTRICT (\d+)", d)
    if m:
        return f"SD{int(m.group(1))}"
    return d  # STATE, 1ST CONGRESSIONAL, 2ND CONGRESSIONAL, PSC DISTRICT N, etc.


# ── Candidate loading ─────────────────────────────────────────────────────────


def load_candidates() -> tuple[dict, dict, dict]:
    """
    Load contested-primaries-candidates.csv and build lookup indexes.

    Returns:
      by_district  dict[dist_key]  -> list[candidate]
      by_last      dict[last_name] -> list[candidate]
      by_full      dict[norm_name] -> candidate  (exact full-name lookup)
    """
    by_district: dict[str, list] = defaultdict(list)
    by_last: dict[str, list] = defaultdict(list)
    by_full: dict[str, dict] = {}

    with open(CANDIDATES_FILE, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            norm = _norm_name(row["Name"])
            if not norm:
                continue
            parts = norm.split()
            dk = _dist_key_from_field(row["District"])
            cand = {
                "name_norm": norm,
                "name_display": row["Name"].strip().lstrip("*").strip(),
                "last_name": parts[-1],
                "first_name": parts[0],
                "district_key": dk,
                "district_raw": row["District"].strip(),
                "race": row["Race"].strip(),
                "party": row["Party Preference"].strip(),
            }
            by_district[dk].append(cand)
            by_last[parts[-1]].append(cand)
            by_full[norm] = cand

    return by_district, by_last, by_full


# ── Issue field parsing ───────────────────────────────────────────────────────

# Prefixes to strip from each candidate reference before matching.
# Handles: Support/Oppose/Supporting/Opposing/Candidate/Rep./Sen./Dr.
_PREFIX_RE = re.compile(
    r"^(?:support(?:ing)?\s+|supopt\s+|oppose[ds]?\s+|opposing\s+"
    r"|candidate\s+|rep\.\s+|rep\s+|sen\.\s+|sen\s+|dr\.\s+|dr\s+"
    r"|for\s+(?=[A-Z]))+",
    re.IGNORECASE,
)

# Patterns that are definitely not candidate names – skip them outright.
_JUNK_RE = re.compile(
    r"^(?:se[e]?\s*attach|ballot\s*init|levy|general\s*fund|special\s*proj"
    r"|contribut|in\s*support\s*of\s*ballot|educat|all\s*republican"
    r"|missoula\s*county|below$|above$|montana\s*law|nonpartisan)",
    re.IGNORECASE,
)


def _split_issue(issue: str) -> list[str]:
    """Split a Candidate Issue value into individual candidate references."""
    parts = re.split(r"[;,]|\band\b", issue, flags=re.IGNORECASE)
    return [p.strip(" ().,;:\"'") for p in parts if p.strip(" ().,;:\"'")]


def _match_one(
    ref: str,
    by_district: dict,
    by_last: dict,
    by_full: dict,
    known_lasts: list[str],
) -> list[dict]:
    """
    Match a single candidate reference string to candidate(s) in the
    contested primaries list.

    Matching priority:
      1. Exact normalized full name
      2. Last name + district (exact)
      3. Unambiguous last name (only one candidate with that last name)
      4. First + last disambiguation: exact first, then prefix, then fuzzy full name
      5. Last + district tiebreaker (multiple share last name)
      6. Fuzzy last name (typo handling) ± district
    """
    if not ref or _JUNK_RE.match(ref):
        return []

    # Strip role prefixes (Support, Oppose, Rep., etc.)
    cleaned = _PREFIX_RE.sub("", ref).strip()
    if not cleaned:
        return []

    # Extract district code; get name remainder
    dist_key, name_part = _extract_district(cleaned)
    name_norm = _norm_name(name_part)

    if not name_norm or len(name_norm) < 3:
        return []

    words = name_norm.split()
    last = words[-1]
    first = words[0] if len(words) >= 2 else None

    # ── 1. Exact full name ────────────────────────────────────────────────────
    if name_norm in by_full:
        return [by_full[name_norm]]

    # ── 2. Last name + district (exact) ──────────────────────────────────────
    if dist_key:
        for c in by_district.get(dist_key, []):
            if c["last_name"] == last:
                return [c]

    last_matches = by_last.get(last, [])

    # ── 3. Unambiguous last name ──────────────────────────────────────────────
    if len(last_matches) == 1:
        return last_matches[:]

    # ── 4. Disambiguation when multiple candidates share a last name ──────────
    if len(last_matches) > 1 and first:
        # 4a. Exact first name
        exact_first = [c for c in last_matches if c["first_name"] == first]
        if len(exact_first) == 1:
            return exact_first

        # 4b. First-name prefix (e.g. "Jed" → "Jedediah", "Mike" → "Michael")
        prefix_matches = [
            c
            for c in last_matches
            if c["first_name"].startswith(first) or first.startswith(c["first_name"])
        ]
        if len(prefix_matches) == 1:
            return prefix_matches

        # 4c. Fuzzy full-name comparison (e.g. "Jebediah Hinkle" → "Jedediah Hinkle")
        ratios = [
            (SequenceMatcher(None, name_norm, c["name_norm"]).ratio(), c)
            for c in last_matches
        ]
        best_r, best_c = max(ratios, key=lambda x: x[0])
        second_best = sorted(ratios, key=lambda x: x[0])[-2][0] if len(ratios) > 1 else 0
        # Require a clear winner (best must be notably better than runner-up)
        if best_r >= FUZZY_FULL_THRESHOLD and (best_r - second_best) >= 0.05:
            return [best_c]

    # ── 5. Last + district tiebreaker ────────────────────────────────────────
    if len(last_matches) > 1 and dist_key:
        dist_narrowed = [c for c in last_matches if c["district_key"] == dist_key]
        if len(dist_narrowed) == 1:
            return dist_narrowed

    # ── 6. Fuzzy last name (handles typos: Buttery→Buttrey, Lentz→Lenz) ──────
    if last not in by_last and len(last) >= 4:
        best_r, best_last = 0.0, ""
        for known in known_lasts:
            r = SequenceMatcher(None, last, known).ratio()
            if r > best_r:
                best_r, best_last = r, known

        if best_r >= FUZZY_LAST_THRESHOLD and best_last:
            fuzzy_matches = by_last[best_last]
            # With district: try to narrow down
            if dist_key:
                dm = [c for c in fuzzy_matches if c["district_key"] == dist_key]
                if len(dm) == 1:
                    return dm
            # Try first-name disambiguation (e.g. George Nikolakos → George Nikolakakos)
            if first:
                fn = [c for c in fuzzy_matches if c["first_name"] == first]
                if len(fn) == 1:
                    return fn
            # Unambiguous fuzzy last name
            if len(fuzzy_matches) == 1:
                return fuzzy_matches[:]

    # ── 7. Short-prefix fallback (handles "Barry Usher For Montana" etc.) ──────
    if len(words) >= 3:
        for n in (2, 3):
            if n >= len(words):
                break
            partial = " ".join(words[:n])
            if partial in by_full:
                return [by_full[partial]]
            partial_last = words[n - 1]
            pm = by_last.get(partial_last, [])
            if len(pm) == 1:
                return pm[:]
            if dist_key:
                dm2 = [c for c in pm if c["district_key"] == dist_key]
                if len(dm2) == 1:
                    return dm2

    return []


def match_issue(
    issue: str,
    by_district: dict,
    by_last: dict,
    by_full: dict,
    known_lasts: list[str],
) -> list[dict]:
    """Return all contested-primary candidates referenced in a Candidate Issue string."""
    if not issue.strip():
        return []
    seen: set[str] = set()
    results: list[dict] = []
    for ref in _split_issue(issue):
        for c in _match_one(ref, by_district, by_last, by_full, known_lasts):
            if c["name_norm"] not in seen:
                seen.add(c["name_norm"])
                results.append(c)
    return results


# ── Output helpers ────────────────────────────────────────────────────────────


def _safe_filename(name: str) -> str:
    """Sanitize a committee name for use as a filesystem filename."""
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return sanitized.strip(". ") or "UNKNOWN"


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    print("Loading candidates...", flush=True)
    by_district, by_last, by_full = load_candidates()
    known_lasts = list(by_last.keys())
    total = sum(len(v) for v in by_district.values())
    print(f"  {total} candidates across {len(by_district)} districts")

    print("Loading expenditures...", flush=True)
    with open(EXPENDITURES_FILE, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames: list[str] = list(reader.fieldnames or [])
        rows = list(reader)
    print(f"  {len(rows)} expenditure rows")

    extra_cols = ["Matched Candidate", "Matched District", "Matched Party", "Matched Race"]
    out_fields = fieldnames + extra_cols

    by_committee: dict[str, list[dict]] = defaultdict(list)
    all_matched_rows: list[dict] = []
    matched_count = 0
    unmatched_issues: list[str] = []

    for row in rows:
        issue = row.get("Candidate Issue", "").strip()
        if not issue:
            continue

        candidates = match_issue(issue, by_district, by_last, by_full, known_lasts)

        if not candidates:
            unmatched_issues.append(issue)
            continue

        matched_count += 1
        out = dict(row)
        out["Matched Candidate"] = " | ".join(c["name_display"] for c in candidates)
        out["Matched District"] = " | ".join(c["district_raw"] for c in candidates)
        out["Matched Party"] = " | ".join(c["party"] for c in candidates)
        out["Matched Race"] = " | ".join(c["race"] for c in candidates)
        committee = row.get("Committee", "UNKNOWN").strip() or "UNKNOWN"
        by_committee[committee].append(out)
        all_matched_rows.append(out)

    print(f"  {matched_count} rows matched → {len(by_committee)} committees")

    # Write per-committee CSVs
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for committee, committee_rows in sorted(by_committee.items()):
        path = OUTPUT_DIR / f"{_safe_filename(committee)}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(committee_rows)

    print(f"Wrote {len(by_committee)} files to {OUTPUT_DIR}/")

    # Write one consolidated CSV with all matched rows.
    ALL_CONTESTED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ALL_CONTESTED_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_matched_rows)
    print(f"Wrote consolidated file to {ALL_CONTESTED_FILE}")

    # Show a sample of unmatched non-empty Candidate Issue values (for debugging)
    unique_unmatched = sorted(set(unmatched_issues))
    if unique_unmatched:
        print(f"\n{len(unique_unmatched)} unique unmatched Candidate Issue values (sample):")
        for v in unique_unmatched[:20]:
            print(f"  {v!r}")
        if len(unique_unmatched) > 20:
            print(f"  … and {len(unique_unmatched) - 20} more")


if __name__ == "__main__":
    main()
