#!/usr/bin/env python3
"""Append validated submissions to the global data/data.json.

    ./scripts/append_submissions.py --all                # everything new
    ./scripts/append_submissions.py submissions/42.json  # one file
    ./scripts/append_submissions.py --all --dry-run      # show, change nothing

Every file that has been merged is recorded in ``data/submissions.lock.json``
together with the SHA-256 of its contents, which makes the append idempotent:
running it twice appends nothing the second time, so the workflow can be
re-run, and a failed run can be retried, without duplicating rows.

The append is textual -- the existing rows in data/data.json are never
re-serialised -- so the diff of a merged submission is exactly the lines it
adds.  ``--update`` is the exception: it drops the rows a previously appended
file contributed and re-appends them, which does rewrite the whole file.

A submission need not carry ``OHC`` or ``Submission Date``: the merge supplies
them with ``--ohc`` (the current iteration, when the pull request is opted in by
its label) and ``--submission-date`` (the merge date).  A submission that does
carry either keeps its own value.  A record left without one that nothing
supplies is refused, since the dataset must never have a hole in those columns.

Standard library only, like the rest of scripts/.
"""

import argparse
import glob
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ohc_schema as schema
import validate_submission as validator

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET = os.path.join(REPO, "data", "data.json")
LEDGER = os.path.join(REPO, "data", "submissions.lock.json")
SUBMISSIONS = os.path.join(REPO, "submissions")


def sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def load_ledger(path):
    if not os.path.exists(path):
        return {"schema": "ohc-submission-ledger/v1", "appended": {}}
    with open(path) as handle:
        ledger = json.load(handle)
    ledger.setdefault("appended", {})
    return ledger


def save_ledger(path, ledger):
    with open(path, "w") as handle:
        json.dump(ledger, handle, indent=2, sort_keys=True)
        handle.write("\n")


def relative(path):
    """Ledger keys are repository-relative, so they survive a checkout."""
    return os.path.relpath(os.path.abspath(path), REPO).replace(os.sep, "/")


# Subdirectories of submissions/ that hold JSON which is not a submission.
NOT_SUBMISSIONS = ("example",)


def discover(submissions_dir):
    """Every submission under submissions/, at any depth.

    Contributors group their files in a folder of their own, so this recurses
    -- except into the directories listed in NOT_SUBMISSIONS, which hold the
    sample submission that must never reach the dataset.
    """
    found = []
    for root, dirs, names in os.walk(submissions_dir):
        dirs[:] = sorted(d for d in dirs
                         if os.path.relpath(os.path.join(root, d),
                                            submissions_dir).split(os.sep)[0]
                         not in NOT_SUBMISSIONS)
        found.extend(os.path.join(root, n) for n in names if n.endswith(".json"))
    return sorted(found)


def rewrite_dataset(path, records):
    """Rewrite the whole dataset.  Only --update needs this."""
    rows = ",\n".join(schema.dumps_record(record) for record in records)
    with open(path, "w") as handle:
        handle.write("[\n" + rows + "\n]\n" if records else "[]\n")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", metavar="FILE",
                        help="submission files to append")
    parser.add_argument("--all", action="store_true", dest="append_all",
                        help="append every file under submissions/ that the "
                             "ledger has not seen yet")
    parser.add_argument("--submissions", default=SUBMISSIONS, metavar="DIR",
                        help="directory scanned by --all (default: %(default)s)")
    parser.add_argument("--dataset", default=DATASET, metavar="FILE",
                        help="global dataset to append to (default: %(default)s)")
    parser.add_argument("--ledger", default=LEDGER, metavar="FILE",
                        help="record of what has been appended "
                             "(default: %(default)s)")
    parser.add_argument("--ohc", type=int, metavar="N",
                        help="OHC iteration stamped on any record that omits it "
                             "-- the current round, passed by the merge from the "
                             "OHC_CURRENT variable when the pull request is "
                             "opted in by its label")
    parser.add_argument("--submission-date", metavar="YYYY-MM-DD",
                        dest="submission_date",
                        help="Submission Date stamped on any record that omits "
                             "it -- the date the pull request was merged")
    parser.add_argument("--update", action="store_true",
                        help="re-append a file whose contents changed since it "
                             "was merged; rewrites the whole dataset")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="report what would happen and change nothing")
    parser.add_argument("--summary", metavar="FILE",
                        help="write a Markdown summary of the append")
    args = parser.parse_args(argv)

    if args.append_all:
        candidates = discover(args.submissions)
    else:
        candidates = list(args.paths)
    if not args.append_all and not candidates:
        parser.error("give at least one file, or --all")

    ledger = load_ledger(args.ledger)
    appended = ledger["appended"]

    pending, superseded, unchanged = [], [], []
    for path in candidates:
        key = relative(path)
        digest = sha256(path)
        entry = appended.get(key)
        if entry is None:
            pending.append((path, key, digest))
        elif entry.get("sha256") == digest:
            unchanged.append(key)
        else:
            superseded.append((path, key, digest, entry))

    if superseded and not args.update:
        for _, key, _, _ in superseded:
            print("error: %s changed after it was appended. Re-run with "
                  "--update to replace the rows it contributed, or submit the "
                  "correction as a new file." % key, file=sys.stderr)
        return 1

    to_process = pending + [(p, k, d) for p, k, d, _ in superseded]
    if not to_process:
        print("nothing to append (%d file(s) already merged)" % len(unchanged))
        if args.summary:
            with open(args.summary, "w") as handle:
                handle.write("## Append to `data/data.json`\n\nNothing new to "
                             "append.\n")
        return 0

    # The rows a superseded file contributed are about to be replaced, so they
    # must not make their own replacements look like duplicates.
    drop = set()
    for _, _, _, entry in superseded:
        drop.update(entry.get("keys", []))

    supplied = validator.supplied_fields(args.ohc, args.submission_date)
    report = validator.validate_files([p for p, _, _ in to_process],
                                      args.dataset, ignore_keys=drop,
                                      supplied=supplied)
    validator.print_report(report)
    if report["errors"]:
        print("error: refusing to append -- fix the errors above",
              file=sys.stderr)
        return 1

    # OHC and Submission Date are optional in a submission but never in the
    # dataset.  Anything the submission left out is filled from --ohc/--date
    # above; a record still missing one here has nothing to fall back on -- the
    # pull request was not opted in to the current round, or no merge date was
    # passed -- so appending it would put a hole in data/data.json.
    unstamped = []
    for entry in report["files"]:
        for record in entry["accepted"]:
            missing = [name for name in ("OHC", "Submission Date")
                       if schema.is_missing(record.get(name))]
            if missing:
                unstamped.append((entry["path"], record, missing))
    if unstamped:
        for path, record, missing in unstamped:
            print("error: %s: %s is missing %s and nothing supplied it -- pass "
                  "--ohc / --submission-date, or add the field to the "
                  "submission" % (path, schema.describe(record),
                                  " and ".join(missing)), file=sys.stderr)
        return 1

    records = schema.load_dataset(args.dataset)
    dropped = 0
    if drop:
        kept = [r for r in records if schema.record_key(r) not in drop]
        dropped = len(records) - len(kept)
        records = kept

    new_records = []
    for entry in report["files"]:
        new_records.extend(entry["accepted"])

    print("---")
    for entry in report["files"]:
        for record in entry["accepted"]:
            print("  + %s" % schema.describe(record))
    print("appending %d record(s) from %d file(s)%s"
          % (len(new_records), len(to_process),
             ", dropping %d superseded row(s)" % dropped if dropped else ""))

    if args.dry_run:
        print("(dry run -- nothing written)")
    elif superseded:
        rewrite_dataset(args.dataset, records + new_records)
    else:
        schema.append_records(args.dataset, new_records)

    by_path = dict((entry["path"], entry) for entry in report["files"])
    for path, key, digest in to_process:
        accepted = by_path[path]["accepted"]
        appended[key] = {
            "sha256": digest,
            "records": len(accepted),
            "keys": [schema.record_key(r) for r in accepted],
        }
    if not args.dry_run:
        save_ledger(args.ledger, ledger)

    if args.summary:
        lines = ["## Append to `data/data.json`", ""]
        lines.append("%d record(s) from %d file(s):"
                     % (len(new_records), len(to_process)))
        lines.append("")
        for path, key, _ in to_process:
            lines.append("- `%s` -- %d record(s)" % (key, len(by_path[path]["accepted"])))
        if dropped:
            lines.append("")
            lines.append("%d superseded row(s) were replaced." % dropped)
        with open(args.summary, "w") as handle:
            handle.write("\n".join(lines) + "\n")

    # Consumed by the workflow for the commit message.
    print("::appended::%d" % len(new_records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
