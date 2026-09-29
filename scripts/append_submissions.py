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

Because a file the ledger lists is never appended again, the ledger is only
safe while the dataset keeps the rows those files put there.  Every run
therefore checks that first, and refuses to append when records it has already
accounted for have gone missing -- appending on top of a truncated or deleted
data.json would make the loss permanent, which is exactly how OHC-1's 237 rows
were lost once.  ``--allow-missing-rows`` is the override for a removal that
was intended.

A submission need not carry ``OHC`` or ``Submission Date``: the merge supplies
them with ``--ohc`` (the current iteration, when the pull request is opted in by
its label) and ``--submission-date`` (the merge date).  A submission that does
carry either keeps its own value.  A record left without one that nothing
supplies is refused, since the dataset must never have a hole in those columns.

Standard library only, like the rest of scripts/.
"""

import argparse
import collections
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


def shown(path):
    """`path` for a message: repository-relative, but never a '../..' chain."""
    rel = relative(path)
    return path if rel.startswith("../") else rel


# One walker for the whole repository, in the schema module: what counts as a
# submission -- a folder per machine, the sample excluded -- is the schema's
# business, and validate_submission.py needs the same answer.
discover = schema.discover_submissions


Gap = collections.namedtuple("Gap", "present recorded lost gaps")


def ledger_gap(appended, dataset_path):
    """Compare what the ledger claims is in the dataset with what is there.

    Returns a `Gap`: how many records the dataset holds (None when the file
    cannot be read at all), how many distinct record keys the ledger accounts
    for, how many of those are missing, and, per ledger entry, how many of its
    keys are gone.

    Entries written before keys were recorded contribute nothing, so an old
    ledger cannot raise a false alarm.  Several entries may record the same
    keys -- a round archive re-submitted under a new name, say -- which is why
    this compares keys rather than counting rows: the counts would add up to
    more than the dataset ever held.
    """
    try:
        records = schema.load_dataset(dataset_path)
    except (OSError, ValueError):
        records = None                  # unreadable: worse than empty
    present = None if records is None else set(
        schema.record_key(record) for record in records)

    recorded, lost, gaps = set(), set(), {}
    for key, entry in sorted(appended.items()):
        keys = set(entry.get("keys", []))
        recorded |= keys
        missing = keys if present is None else keys - present
        if missing:
            gaps[key] = len(missing)
            lost |= missing
    return Gap(present=None if present is None else len(present),
               recorded=len(recorded), lost=len(lost), gaps=gaps)


def check_ledger(appended, dataset_path, ledger_path, allow_missing=False):
    """Refuse to append onto a dataset that has lost already-merged rows.

    The ledger makes the append idempotent: a file it lists is never appended
    again.  That is only safe while the dataset keeps what those files put
    there -- if rows go missing (a hand-edited data.json, a deleted file, a
    bad merge), appending on top would quietly cement the loss, because
    nothing will ever re-add them.  This is how OHC-1's 237 rows disappeared
    once: data.json was deleted, and the next append rebuilt it from the one
    submission the ledger had not seen yet.

    Returns the number of missing records: 0 when the dataset is intact, and
    with `allow_missing` the shortfall is reported but tolerated.
    """
    gap = ledger_gap(appended, dataset_path)
    if not gap.gaps:
        return 0

    missing, gaps = gap.lost, gap.gaps
    dataset = shown(dataset_path)
    if gap.present is None:
        where = "it cannot be read"
    elif not gap.present:
        where = ("it is empty or absent, and the ledger accounts for %d "
                 "record(s)" % gap.recorded)
    else:
        where = ("it holds %d record(s), the ledger accounts for %d"
                 % (gap.present, gap.recorded))
    label = "warning" if allow_missing else "error"
    out = sys.stdout if allow_missing else sys.stderr
    print("%s: %d record(s) that %s says were appended are missing from %s -- "
          "%s." % (label, missing, shown(ledger_path), dataset, where),
          file=out)
    for key, count in sorted(gaps.items()):
        print("         %-45s %d row(s) missing" % (key, count), file=out)
    if allow_missing:
        print("         --allow-missing-rows given: appending anyway.",
              file=out)
        return 0
    print("       Nothing was appended: a file the ledger lists is never "
          "appended again, so adding to the dataset now would make the loss "
          "permanent.", file=out)
    print("       Restore the dataset (git show <commit>:%s), or -- if those "
          "rows were dropped on purpose -- remove the ledger entries that "
          "claim them and re-append those files.  --allow-missing-rows "
          "appends regardless." % dataset, file=out)
    return missing


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
    parser.add_argument("--allow-missing-rows", action="store_true",
                        dest="allow_missing_rows",
                        help="append even though the dataset has lost records "
                             "the ledger says were already appended. Only for "
                             "a deliberate removal -- otherwise the rows are "
                             "gone for good, since a file the ledger lists is "
                             "never appended again")
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

    # Before anything is written: is the dataset still holding everything the
    # ledger says was put there?  Appending onto a dataset that has lost rows
    # would cement the loss.
    if check_ledger(appended, args.dataset, args.ledger,
                    args.allow_missing_rows):
        return 1

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
