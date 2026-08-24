#!/usr/bin/env python3
"""Rebuild data/data.json from the per-round files.

    ./scripts/rebuild_dataset.py --ohc1-date 2024-07-22 --ohc2-date 2025-07-07

data/data.json is the union of the published rounds, one record per
simulation, plus the two columns the round files do not carry:

``OHC``
    Which round the result was submitted to, taken from the source file.
``Submission Date``
    When it was submitted.  The OHC1 and OHC2 spreadsheets did not record one,
    so every record of a round gets the date of the workshop its results were
    presented at, passed in per round.

Normally data/data.json only ever grows, by append-submissions.yml appending a
merged submission.  This script rewrites it from scratch instead, so it
refuses to run once submissions have been appended -- those records exist
nowhere else and a rebuild would drop them.  ``--force`` overrides that.

Standard library only, like the rest of scripts/.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ohc_schema as schema

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET = os.path.join(REPO, "data", "data.json")
LEDGER = os.path.join(REPO, "data", "submissions.lock.json")

# Round -> source file.  Add a line here when a round is published.
ROUNDS = [(1, os.path.join(REPO, "data", "data_OHC1.json")),
          (2, os.path.join(REPO, "data", "data_OHC2.json"))]


def appended_files(ledger_path):
    """Submissions the ledger says are already merged into the dataset."""
    if not os.path.exists(ledger_path):
        return []
    try:
        with open(ledger_path) as handle:
            return sorted(json.load(handle).get("appended", {}))
    except (OSError, ValueError):
        return []


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    for round_number, _ in ROUNDS:
        parser.add_argument("--ohc%d-date" % round_number, required=True,
                            metavar="YYYY-MM-DD",
                            dest="date_%d" % round_number,
                            help="Submission Date stamped on every OHC%d "
                                 "record" % round_number)
    parser.add_argument("--dataset", default=DATASET, metavar="FILE",
                        help="file to write (default: %(default)s)")
    parser.add_argument("--ledger", default=LEDGER, metavar="FILE",
                        help="append ledger to check (default: %(default)s)")
    parser.add_argument("--force", action="store_true",
                        help="rebuild even though submissions were appended")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="report what would be written and change nothing")
    args = parser.parse_args(argv)

    merged = appended_files(args.ledger)
    if merged and not args.force:
        print("error: %d submission(s) have been appended to %s and are not in "
              "the round files:" % (len(merged), os.path.basename(args.dataset)),
              file=sys.stderr)
        for name in merged:
            print("  %s" % name, file=sys.stderr)
        print("A rebuild would drop them.  Re-run with --force to do it "
              "anyway, and reset the ledger afterwards.", file=sys.stderr)
        return 1

    issues = 0
    records = []
    for round_number, path in ROUNDS:
        date = getattr(args, "date_%d" % round_number)
        checked, date_issues = schema.check_field(
            schema.BY_NAME["Submission Date"], date, "--ohc%d-date" % round_number)
        for issue in date_issues:
            print(issue, file=sys.stderr)
            issues += 1

        with open(path) as handle:
            source = json.load(handle)
        for record in source:
            stamped = dict(record)
            stamped["OHC"] = round_number
            stamped.setdefault("Submission Date", checked)
            records.append(stamped)
        print("OHC%d: %4d record(s) from %s, dated %s"
              % (round_number, len(source), os.path.basename(path), checked))

    if issues:
        return 1

    keys = {}
    for record in records:
        keys.setdefault(schema.record_key(record), []).append(record)
    collisions = sum(1 for rows in keys.values() if len(rows) > 1)
    if collisions:
        print("warning: %d record identit(ies) appear more than once -- a "
              "later submission of the same run will be reported as a "
              "duplicate" % collisions, file=sys.stderr)

    columns = set()
    for record in records:
        columns.update(record)
    ragged = [c for c in columns if any(c not in r for r in records)]
    if ragged:
        print("warning: column(s) missing from some records: %s"
              % ", ".join(sorted(ragged)), file=sys.stderr)

    print("---")
    print("%d record(s), %d column(s) -> %s"
          % (len(records), len(columns), args.dataset))
    if args.dry_run:
        print("(dry run -- nothing written)")
        return 0

    rows = ",\n".join(schema.dumps_record(record) for record in records)
    with open(args.dataset, "w") as handle:
        handle.write("[\n" + rows + "\n]\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
