#!/usr/bin/env python3
"""Validate contributed OHC result files against the repository schema.

Run it on the files a pull request adds under ``submissions/``::

    ./scripts/validate_submission.py submissions/42_OHC_DrivAer_coarse.json

It reports, per record, every field that is missing, mistyped, out of range or
inconsistent with its neighbours, and exits non-zero when any of those is an
error.  Warnings (a hardware string nobody has used before, a derived value
that disagrees with its formula) are printed but do not fail the run.

Useful switches:

    --markdown FILE   also write the report as Markdown, for a CI job summary
    --json FILE       machine-readable report
    --strict          treat warnings as errors
    --self-check      validate the existing dataset instead of a submission,
                      which is how the schema itself is regression-tested

Standard library only: a contributor can run this without installing anything.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ohc_schema as schema

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET = os.path.join(REPO, "data", "data.json")
# A submission's machine description is looked up next to the file and then up
# its own folder; the walk stops here, so it can never read the metadata of a
# neighbouring contributor's submission.
SUBMISSIONS = os.path.join(REPO, "submissions")
# data/data.json is the single source of truth, so it is the only file
# enum-learning and the self-check read.  The raw round archives
# (data/data_OHC1.json) are kept for provenance but add nothing here: their
# runs are already in data.json, normalised and stamped with OHC/Submission
# Date, so data.json's known values are a superset of theirs.
REFERENCE_DATA = [os.path.join(REPO, "data", "data.json")]


def supplied_fields(ohc=None, submission_date=None):
    """Build the `supplied` map from what the merge knows.

    Only the fields actually provided appear, so a record that already carries
    OHC or Submission Date keeps its own value.
    """
    supplied = {}
    if ohc is not None:
        supplied["OHC"] = ohc
    if submission_date is not None:
        supplied["Submission Date"] = submission_date
    return supplied


def validate_files(paths, dataset_path=DATASET, learn=True, ignore_keys=None,
                   supplied=None, submissions_dir=SUBMISSIONS):
    """Validate every path.  Returns a report dict.

    `ignore_keys` lists record keys that must not count as duplicates -- the
    rows a re-appended submission is about to replace, which are still in the
    dataset while it is being checked.

    `supplied` maps a SUPPLIED field (OHC, Submission Date) to the value the
    merge fills in when a record omits it; it is passed straight through to
    ``schema.validate_record`` so that stamped records carry the right natural
    key for duplicate detection.
    """
    known = schema.learn_enums(REFERENCE_DATA) if learn else {}
    ignore_keys = set(ignore_keys or ())
    existing = {}
    try:
        for record in schema.load_dataset(dataset_path):
            key = schema.record_key(record)
            if key not in ignore_keys:
                existing[key] = record
    except (OSError, ValueError) as exc:
        print("warning: cannot read %s (%s); duplicate detection is off"
              % (dataset_path, exc), file=sys.stderr)

    files = []
    seen_in_this_run = {}
    for path in paths:
        raw_records, issues = schema.load_submission(path, submissions_dir)
        accepted = []
        for where, raw in raw_records:
            record, record_issues = schema.validate_record(
                raw, where, known, supplied)
            key = schema.record_key(record)
            if key in existing:
                record_issues.append(schema.error(
                    where, "duplicates a record already in %s (%s) -- remove "
                           "it from the submission, or change what makes it "
                           "different" % (os.path.basename(dataset_path),
                                          schema.describe(record))))
            elif key in seen_in_this_run:
                record_issues.append(schema.error(
                    where, "duplicates %s in this pull request"
                           % seen_in_this_run[key]))
            else:
                seen_in_this_run[key] = where
            if not any(i.severity == "error" for i in record_issues):
                accepted.append(record)
            issues.extend(record_issues)

        files.append({
            "path": path,
            "records": len(raw_records),
            "accepted": accepted,
            "issues": issues,
            "errors": sum(1 for i in issues if i.severity == "error"),
            "warnings": sum(1 for i in issues if i.severity == "warning"),
        })

    return {
        "files": files,
        "errors": sum(f["errors"] for f in files),
        "warnings": sum(f["warnings"] for f in files),
        "records": sum(f["records"] for f in files),
        "accepted": sum(len(f["accepted"]) for f in files),
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def print_report(report):
    for entry in report["files"]:
        print("%s: %d record(s), %d error(s), %d warning(s)"
              % (entry["path"], entry["records"], entry["errors"],
                 entry["warnings"]))
        for issue in entry["issues"]:
            print("  %s" % issue)
    print("---")
    print("%d record(s) across %d file(s): %d error(s), %d warning(s)"
          % (report["records"], len(report["files"]), report["errors"],
             report["warnings"]))


def markdown_report(report, title="Submission validation"):
    """Render the report for a GitHub job summary or a pull-request comment."""
    lines = ["## %s" % title, ""]

    if not report["files"]:
        lines.append("No submission files changed in this pull request.")
        return "\n".join(lines) + "\n"

    if report["errors"]:
        lines.append("**%d error(s)** in %d record(s) -- the submission cannot "
                     "be appended yet." % (report["errors"], report["records"]))
    else:
        lines.append("All %d record(s) are valid." % report["records"])
    lines.append("")

    lines.append("| File | Records | Errors | Warnings |")
    lines.append("| --- | ---: | ---: | ---: |")
    for entry in report["files"]:
        lines.append("| `%s` | %d | %d | %d |"
                     % (entry["path"], entry["records"], entry["errors"],
                        entry["warnings"]))
    lines.append("")

    for entry in report["files"]:
        if not entry["issues"]:
            continue
        lines.append("### `%s`" % entry["path"])
        lines.append("")
        for issue in entry["issues"]:
            mark = "**error**" if issue.severity == "error" else "warning"
            lines.append("- %s -- %s: %s" % (mark, issue.where, issue.message))
        lines.append("")

    if not report["errors"] and report["accepted"]:
        lines.append("### Records that would be appended to `data/data.json`")
        lines.append("")
        lines.append("| Contributor | Track | Mesh | Nodes | Cores | "
                     "Time per iteration [s] | FVOPS |")
        lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: |")
        for entry in report["files"]:
            for record in entry["accepted"]:
                fvops = record.get("FVOPS")
                lines.append("| %s (%s) | %s | %s | %s | %s | %s | %s |" % (
                    record.get("Contributor ID"),
                    record.get("Contributor Affiliation"),
                    record.get("Track"), record.get("Mesh"),
                    record.get("Number of Nodes"),
                    record.get("Number of CPU Cores"),
                    record.get("Time per Iteration [s]"),
                    "{:,.0f}".format(fvops) if isinstance(fvops, float)
                    else "-"))
        lines.append("")
        lines.append("They are appended by the `append-submissions` workflow "
                     "once this pull request is merged.")
    return "\n".join(lines) + "\n"


def json_report(report):
    return json.dumps({
        "errors": report["errors"],
        "warnings": report["warnings"],
        "records": report["records"],
        "accepted": report["accepted"],
        "files": [{
            "path": f["path"],
            "records": f["records"],
            "accepted": len(f["accepted"]),
            "issues": [i.as_dict() for i in f["issues"]],
        } for f in report["files"]],
    }, indent=2)


def schema_markdown():
    """Render the field reference, so submissions/README.md cannot drift."""
    roles = [(schema.REQUIRED, "Required",
              "Every record must carry these."),
             (schema.OPTIONAL, "Optional",
              "May be omitted, `null`, `\"N/A\"`, `\"-\"` or `\"/\"`; the "
              "placeholder in the last column is stored instead."),
             (schema.SUPPLIED, "Supplied on merge",
              "May be omitted from a submission: the merge fills `OHC` from the "
              "current iteration and `Submission Date` from the merge date. A "
              "submitted value is validated and kept as submitted."),
             (schema.DERIVED, "Derived",
              "Computed on append when absent. A submitted value is kept as "
              "submitted and only cross-checked against the formula.")]

    lines = []
    for role, title, blurb in roles:
        fields = [f for f in schema.FIELDS if f.role == role]
        lines.append("### %s (%d)" % (title, len(fields)))
        lines.append("")
        lines.append(blurb)
        lines.append("")
        lines.append("| Field | Type | Accepted values / notes |")
        lines.append("| --- | --- | --- |")
        for field in fields:
            notes = []
            if field.enum:
                notes.append(", ".join("`%s`" % v for v in field.enum))
            if field.positive:
                notes.append("> 0")
            elif field.minimum is not None:
                notes.append(">= %s" % schema._fmt(field.minimum))
            if field.doc:
                notes.append(field.doc)
            if role == schema.OPTIONAL:
                notes.append("stored as `%s` when absent"
                             % json.dumps(field.default))
            lines.append("| `%s` | %s | %s |"
                         % (field.name, field.kind, " ".join(notes) or "--"))
        lines.append("")
    return "\n".join(lines)


START_MARKER = "<!-- fields:start -->"
END_MARKER = "<!-- fields:end -->"


def write_schema_doc(path, parser):
    """Replace the generated table in `path`, leaving the prose around it."""
    with open(path) as handle:
        text = handle.read()
    start, end = text.find(START_MARKER), text.find(END_MARKER)
    if start < 0 or end < start:
        parser.error("%s has no %s ... %s block to rewrite"
                     % (path, START_MARKER, END_MARKER))
    with open(path, "w") as handle:
        handle.write(text[:start] + START_MARKER + "\n\n"
                     + schema_markdown() + "\n" + text[end:])
    print("rewrote the field reference in %s" % path)
    return 0


# --------------------------------------------------------------------------- #
# Self-check
# --------------------------------------------------------------------------- #

def self_check(max_rejected=0, dataset_path=DATASET):
    """Validate the published dataset against the schema.

    Only data/data.json is checked: it is the single source of truth the
    workflows append to and the notebook reads, and the file carrying every
    column.  The raw round archives (data/data_OHC1.json) are kept for
    provenance and are not part of this check -- their runs are already in
    data.json, normalised.

    The historical spreadsheets are not perfectly self-consistent, so this
    reports rather than asserts -- but a schema change that suddenly rejects
    records that are already published shows up here immediately.
    """
    known = schema.learn_enums(REFERENCE_DATA)
    for path in REFERENCE_DATA:
        if path != dataset_path and os.path.exists(path):
            print("%s: %d source record(s), not validated"
                  % (path, len(schema.load_dataset(path))))

    records = schema.load_dataset(dataset_path)
    failures = 0
    for index, raw in enumerate(records):
        _, issues = schema.validate_record(
            raw, "%s[%d]" % (os.path.basename(dataset_path), index), known)
        record_errors = [i for i in issues if i.severity == "error"]
        if record_errors:
            failures += 1
            if failures <= 5:
                print("  %s" % record_errors[0])
    print("%s: %d/%d record(s) rejected"
          % (dataset_path, failures, len(records)))
    print("---")
    print("%d/%d published record(s) rejected by the current schema "
          "(allowed: %d)" % (failures, len(records), max_rejected))
    return max(0, failures - max_rejected)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", metavar="FILE",
                        help="submission files to validate")
    parser.add_argument("--dataset", default=DATASET,
                        help="global dataset checked for duplicates "
                             "(default: %(default)s)")
    parser.add_argument("--markdown", metavar="FILE",
                        help="write the report as Markdown as well")
    parser.add_argument("--json", metavar="FILE", dest="json_out",
                        help="write the report as JSON as well")
    parser.add_argument("--title", default="Submission validation",
                        help="heading of the Markdown report")
    parser.add_argument("--ohc", type=int, metavar="N",
                        help="OHC iteration to stamp on records that omit it, "
                             "as the merge does (default: leave absent)")
    parser.add_argument("--submission-date", metavar="YYYY-MM-DD",
                        dest="submission_date",
                        help="Submission Date to stamp on records that omit it, "
                             "as the merge does (default: leave absent)")
    parser.add_argument("--strict", action="store_true",
                        help="fail on warnings too")
    parser.add_argument("--self-check", action="store_true",
                        dest="self_check_mode",
                        help="validate the existing dataset instead")
    parser.add_argument("--schema-doc", nargs="?", const="-", metavar="FILE",
                        dest="schema_doc",
                        help="print the field reference as Markdown and exit; "
                             "with a FILE, rewrite it between that file's "
                             "<!-- fields:start --> and <!-- fields:end --> "
                             "markers instead")
    parser.add_argument("--max-rejected", type=int, default=0,
                        dest="max_rejected", metavar="N",
                        help="with --self-check, tolerate up to N already "
                             "published records the schema rejects")
    args = parser.parse_args(argv)

    if args.schema_doc:
        if args.schema_doc == "-":
            print(schema_markdown())
            return 0
        return write_schema_doc(args.schema_doc, parser)

    if args.self_check_mode:
        return 1 if self_check(args.max_rejected, args.dataset) else 0

    missing = [p for p in args.paths if not os.path.exists(p)]
    if missing:
        parser.error("no such file: %s" % ", ".join(missing))

    supplied = supplied_fields(args.ohc, args.submission_date)
    report = validate_files(args.paths, args.dataset, supplied=supplied)
    print_report(report)

    if args.markdown:
        with open(args.markdown, "w") as handle:
            handle.write(markdown_report(report, args.title))
    if args.json_out:
        with open(args.json_out, "w") as handle:
            handle.write(json_report(report) + "\n")

    if report["errors"]:
        return 1
    if args.strict and report["warnings"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
