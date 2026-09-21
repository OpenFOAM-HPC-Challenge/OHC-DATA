#!/usr/bin/env python3
"""Schema of a single OpenFOAM HPC Challenge result record.

This module is the one place that knows what a valid submission looks like.
``validate_submission.py`` uses it to check a contributed file, and
``append_submissions.py`` uses it to normalise, derive and serialise records
before they are appended to the global ``data/data.json``.

The field list mirrors the columns of ``data/data_OHC1.json`` -- the shape the
Overview notebook expects -- and splits them in four:

``REQUIRED``
    Raw, measured or declared quantities that nothing else can supply.
``OPTIONAL``
    Fields the historical dataset is itself missing for some entries (hardware
    details that not every site publishes, pre-processing timings that not
    every submission recorded).  ``null``, ``"N/A"``, ``"-"`` and ``"/"`` are
    all accepted and normalised to a canonical value.
``SUPPLIED``
    Fields a submission need not carry because the merge supplies them: ``OHC``
    from the current iteration and ``Submission Date`` from the merge time (see
    ``append_submissions.py``).  A submission *may* still carry them, in which
    case the submitted value is validated and kept; when absent they are left
    out here and filled on append, so the finished dataset always has both.
``DERIVED``
    Everything computable from the required fields.  A submission may omit
    these -- they are filled in on append.  When a submission *does* carry
    them they are kept as submitted and only cross-checked, because the
    historical spreadsheets computed a few of them from intermediates that are
    no longer available (see ``DERIVATION_TOLERANCE``).

Standard library only, like the rest of scripts/.
"""

import datetime
import json
import os

# --------------------------------------------------------------------------- #
# Field declarations
# --------------------------------------------------------------------------- #

REQUIRED = "required"
OPTIONAL = "optional"
SUPPLIED = "supplied"
DERIVED = "derived"

# Nominal cell count per mesh resolution, matching MESH_CELLS in benchmark.py
# and OHCParser.get_cells().
MESH_CELLS = {"coarse": 65000000, "medium": 110000000, "fine": 236000000}

# The challenge case is scored over a fixed number of SIMPLE iterations, and
# the *-To-Solution columns are normalised to it: they answer "what would this
# machine need for the standard case", not "what did this particular run take".
# That is why they are built from the time per iteration rather than from the
# measured wall clock, which covers however many iterations the run happened to
# do (2041 to 16000 across the published data).
NOMINAL_ITERATIONS = 4000

# Above this core count the solve is spread thin enough to finish in minutes,
# while mesh decomposition and I/O -- a mostly-fixed, poorly-scaling cost --
# stay hours long, so preprocessing legitimately dwarfs the solve.  The
# "pre-processing >> solver" smell is only a unit-slip signal below this scale;
# above it, it is just strong scaling and must not warn.
PREPROC_SCALE_CORES = 1024

# What the OHC1/OHC2 spreadsheets put in Run Consumed Energy when the run was
# not metered.  A true zero would fall off the log axes the notebook plots on.
UNREPORTED_ENERGY = 1e-32

# Spellings that mean "not reported" in the contributed spreadsheets.
NULL_TOKENS = {"", "n/a", "na", "none", "null", "-", "/", "nan", "?"}

# Relative disagreement between a submitted derived value and the recomputed
# one that is worth mentioning.  Warning only: the OHC1/OHC2 spreadsheets
# disagree with the formulas by up to ~30% on the energy-per-iteration family,
# so a mismatch is a smell, not proof of an error.
DERIVATION_TOLERANCE = 0.01


class Field(object):
    """One column of a result record."""

    def __init__(self, name, kind, role, enum=None, closed=False,
                 minimum=None, positive=False, default=None, unit=None,
                 doc=""):
        self.name = name
        self.kind = kind              # str | int | float | flag | number | cache
        self.role = role              # REQUIRED | OPTIONAL | DERIVED
        self.enum = enum              # known values, None when free-form
        self.closed = closed          # unknown value is an error, not a warning
        self.minimum = minimum
        self.positive = positive
        self.default = default        # substituted when an OPTIONAL is absent
        self.unit = unit
        self.doc = doc


FIELDS = [
    # -- provenance ------------------------------------------------------- #
    Field("Contributor ID", "str", REQUIRED,
          doc="Participant number assigned by the challenge organisers."),
    Field("Contributor Affiliation", "str", REQUIRED,
          doc="Institution or company the submission is credited to."),
    Field("File Name", "str", REQUIRED,
          doc="Name of the source result file this record was harvested from."),
    Field("Track", "str", REQUIRED, closed=True,
          enum=["Hardware Track", "Software Track"]),
    Field("OHC", "int", SUPPLIED, minimum=1,
          doc="Round of the challenge this result was submitted to. May be "
              "omitted: the merge stamps the current iteration on a submission "
              "that does not carry it."),
    Field("Submission Date", "date", SUPPLIED,
          doc="ISO date, YYYY-MM-DD. May be omitted: the merge stamps the date "
              "the pull request was merged. For the published rounds it is the "
              "workshop the results were presented at, since the spreadsheets "
              "did not record a date."),
    Field("Software Optimization Category", "str", REQUIRED,
          doc="'Other' for a plain Hardware Track run."),

    # -- case ------------------------------------------------------------- #
    Field("Mesh", "str", REQUIRED, closed=True,
          enum=["coarse", "medium", "fine"]),
    Field("Number of Cells", "number", REQUIRED, positive=True,
          doc="Must match the nominal cell count of the mesh."),
    Field("OpenFOAM Flavor", "str", REQUIRED, closed=True,
          enum=["ESI (.com)", "Foundation (.org)", "Other"]),
    Field("Decomposition Method", "str", OPTIONAL, default="N/A"),
    Field("Renumbering Method", "str", OPTIONAL, default="N/A"),

    # -- hardware --------------------------------------------------------- #
    Field("Number of Nodes", "int", REQUIRED, positive=True),
    Field("Number of CPU Cores", "int", REQUIRED, positive=True,
          doc="Total across all nodes, not per node."),
    Field("Number of GPU Devices", "int", OPTIONAL, minimum=0, default=0,
          doc="Total across all nodes; omit it for a CPU-only run."),
    Field("CPU Family", "str", REQUIRED),
    Field("CPU Model", "str", REQUIRED),
    Field("CPU Submodel", "str", OPTIONAL, default="N/A"),
    Field("CPU Generation", "str", REQUIRED),
    Field("CPU Memory Bandwidth [GB/s]", "float", OPTIONAL, positive=True,
          unit="GB/s", doc="Theoretical peak per node."),
    Field("GPU Model", "str", OPTIONAL, default="N/A",
          doc="Required once Number of GPU Devices is non-zero."),
    Field("GPU Memory Bandwidth [GB/s]", "float", OPTIONAL, positive=True,
          unit="GB/s", doc="Theoretical peak per device."),
    Field("Last-Level Cache", "cache", OPTIONAL, default="N/A",
          doc="Per node, e.g. '256MB' or 'on-package memory'."),
    Field("Network Interconnect", "str", OPTIONAL, default="N/A"),
    Field("Storage File-System", "str", OPTIONAL, default="N/A"),
    Field("System TDP [W]", "float", REQUIRED, positive=True, unit="W",
          doc="Thermal design power of the whole allocation, all nodes."),

    # -- measurement ------------------------------------------------------ #
    Field("Run Wall-Clock Time [s]", "float", REQUIRED, positive=True,
          unit="s", doc="Solver only, excluding pre-processing."),
    Field("Time per Iteration [s]", "float", REQUIRED, positive=True, unit="s"),
    Field("Pre-Processing Wall-Clock Time [s]", "number", OPTIONAL,
          minimum=0, unit="s",
          doc="decomposePar + renumberMesh + potentialFoam + applyBoundaryLayer."),
    Field("Run Consumed Energy [kWh]", "float", OPTIONAL, minimum=0,
          unit="kWh", default=UNREPORTED_ENERGY,
          doc="What the run actually drew, when it was metered. Required if "
              "Is Energy Reported is 1; leave it out otherwise. It is reported "
              "for the record and does not feed the energy columns below, "
              "which are normalised from the TDP so that entries stay "
              "comparable."),
    Field("Is Energy Reported", "flag", REQUIRED,
          doc="1 when the energy was measured, 0 when derived from the TDP."),
    Field("Is Partial", "flag", REQUIRED,
          doc="1 when the run stopped before the requested end time."),

    # -- derived ---------------------------------------------------------- #
    Field("Total Core Time [s]", "float", DERIVED, unit="s",
          doc="What the run cost: wall-clock x cores."),
    Field("Total Node Time [s]", "float", DERIVED, unit="s",
          doc="What the run cost: wall-clock x nodes."),
    Field("Time-To-Solution [h]", "float", DERIVED, unit="h",
          doc="4000 x time per iteration / 3600, i.e. the standard case at "
              "this machine's pace -- not the measured wall clock."),
    Field("Core-Time-To-Solution [h]", "float", DERIVED, unit="h",
          doc="Time-To-Solution x cores"),
    Field("Node-Time-To-Solution [h]", "float", DERIVED, unit="h",
          doc="Time-To-Solution x nodes"),
    Field("Energy-To-Solution [kWh]", "float", DERIVED, unit="kWh",
          doc="TDP x Time-To-Solution / 1000. Modelled from the TDP, not taken "
              "from Run Consumed Energy, so that metered and unmetered "
              "entries stay comparable."),
    Field("Energy per Iteration [J]", "float", DERIVED, unit="J",
          doc="TDP x time per iteration"),
    Field("Energy per Iteration [kJ]", "float", DERIVED, unit="kJ",
          doc="TDP x time per iteration / 1000"),
    Field("FVOPS", "float", DERIVED,
          doc="Finite VOlume Operations Per Second: cells / time per iteration."),
    Field("FVOPS per Node", "float", DERIVED, doc="FVOPS / nodes"),
    Field("FVOPS per Energy", "float", DERIVED,
          doc="FVOPS / (TDP x time per iteration)"),
    Field("FVOPS per Energy per Iteration", "float", DERIVED,
          doc="Same quantity as FVOPS per Energy; both column names exist in "
              "the published data, so both are written."),
    Field("FVOPS per kW", "float", DERIVED, doc="FVOPS / (TDP / 1000)"),
]

BY_NAME = dict((f.name, f) for f in FIELDS)
FIELD_ORDER = [f.name for f in FIELDS]

# The natural key of a result: one contributor's one run of one configuration.
# The two timings are part of it because the configuration alone does not
# identify a run -- 44 rows of the published data repeat a contributor, track,
# mesh, node and core count from the same source file, and differ only in what
# the run measured.  Those are repeat runs, not duplicates, and a submission
# containing them has to pass.
IDENTITY_FIELDS = [
    "OHC", "Contributor ID", "Track", "Software Optimization Category", "Mesh",
    "Number of Nodes", "Number of CPU Cores", "Number of GPU Devices",
    "File Name", "Time per Iteration [s]", "Run Wall-Clock Time [s]",
]

# Envelope keys accepted around the list of records.
ENVELOPE_RECORDS = ("simulations", "records", "data", "results")

SCHEMA_ID = "ohc-submission/v1"

# The machine description that sits next to an envelope submission.  A
# submission directory holds one folder per machine, and each of those carries
# the metadata of that machine -- what `benchmark.py --postProcess` writes as a
# copy of its workspace's own meta.yaml, so the hardware behind the numbers
# stays readable next to them.  The alternative spellings are accepted because
# a contributor who simply copies the workspace file over should not be stopped
# by its name (submissionMeta.yaml is what older workspaces call it).
META_NAMES = ("meta.yaml", "meta.yml", "submissionMeta.yaml")

# meta.yaml key -> the field of the JSON envelope's `meta` block that must
# agree with it.  The JSON is what gets ingested; the YAML is provenance, so a
# disagreement is a warning that the two drifted apart, not an error.
META_YAML_FIELDS = [
    ("participant_id", "Contributor ID"),
    ("affiliation", "Contributor Affiliation"),
    ("track", "Track"),
    ("cpu_family", "CPU Family"),
    ("cpu_model", "CPU Model"),
    ("cpu_submodel", "CPU Submodel"),
    ("cpu_generation", "CPU Generation"),
    ("last_level_cache", "Last-Level Cache"),
    ("network_interconnect", "Network Interconnect"),
    ("storage_file_system", "Storage File-System"),
]


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #

class Issue(object):
    """One validation finding, tied to the record and field that raised it."""

    def __init__(self, severity, where, message):
        self.severity = severity      # "error" | "warning"
        self.where = where            # human-readable location
        self.message = message

    def __str__(self):
        return "%s: %s: %s" % (self.severity, self.where, self.message)

    def as_dict(self):
        return {"severity": self.severity, "where": self.where,
                "message": self.message}


def error(where, message):
    return Issue("error", where, message)


def warning(where, message):
    return Issue("warning", where, message)


# --------------------------------------------------------------------------- #
# Value coercion
# --------------------------------------------------------------------------- #

def is_missing(value):
    """True for the many spellings of 'this field was not reported'."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in NULL_TOKENS
    return False


def coerce_number(value):
    """Return `value` as a float, or None when it is not numeric.

    Booleans are rejected on purpose: JSON true/false in a numeric column is
    almost always a mistake, and Python would silently read them as 1/0.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def coerce_int(value):
    """Return `value` as an int when it is integral, else None."""
    number = coerce_number(value)
    if number is None or number != int(number):
        return None
    return int(number)


def parse_duration(text):
    """Read a spreadsheet duration ('00:15:20.447000') as seconds, else None.

    Several OHC1 submissions reported the pre-processing time as a formatted
    duration rather than a number, so the seconds columns accept both.
    """
    parts = text.strip().split(":")
    if not 2 <= len(parts) <= 3:
        return None
    seconds = 0.0
    for part in parts:
        try:
            component = float(part)
        except ValueError:
            return None
        if component < 0:
            return None
        seconds = seconds * 60.0 + component
    return seconds


def check_field(field, value, where):
    """Type/range-check one value.  Returns (normalised value, issues)."""
    issues = []

    if field.kind in ("int", "flag"):
        number = coerce_int(value)
        if number is None:
            issues.append(error(where, "expected a whole number, got %s"
                                % json.dumps(value)))
            return value, issues
        if field.kind == "flag" and number not in (0, 1):
            issues.append(error(where, "expected 0 or 1, got %d" % number))
            return number, issues
        value = number

    elif field.kind in ("float", "number"):
        number = coerce_number(value)
        if number is None and field.unit == "s" and isinstance(value, str):
            number = parse_duration(value)
            if number is not None:
                issues.append(warning(where, "read %s as %s s -- please report "
                                             "seconds as a number"
                                      % (json.dumps(value), _fmt(number))))
                value = number
        if number is None:
            issues.append(error(where, "expected a number, got %s"
                                % json.dumps(value)))
            return value, issues
        # 'number' keeps an integral input integral so the appended record
        # looks like the surrounding data instead of gaining a stray '.0'.
        value = number if field.kind == "float" else _keep_integral(value, number)

    elif field.kind == "date":
        if not isinstance(value, str):
            issues.append(error(where, "expected an ISO date string, got %s"
                                % json.dumps(value)))
            return value, issues
        value = value.strip()
        try:
            datetime.date(*(int(part) for part in value.split("-")))
        except (TypeError, ValueError):
            issues.append(error(where, "expected a date as YYYY-MM-DD, got %s"
                                % json.dumps(value)))
        return value, issues

    elif field.kind == "cache":
        # Historically either '256MB' or a bare megabyte count.
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            issues.append(error(where, "expected a size string or a number, got %s"
                                % json.dumps(value)))
            return value, issues

    else:                                             # "str"
        if not isinstance(value, str):
            issues.append(error(where, "expected a string, got %s"
                                % json.dumps(value)))
            return value, issues
        value = value.strip()
        if not value:
            issues.append(error(where, "is empty"))
            return value, issues

    number = coerce_number(value)
    if number is not None:
        if field.positive and number <= 0:
            issues.append(error(where, "must be greater than zero, got %s"
                                % _fmt(number)))
        elif field.minimum is not None and number < field.minimum:
            issues.append(error(where, "must be >= %s, got %s"
                                % (_fmt(field.minimum), _fmt(number))))

    if field.enum and isinstance(value, str) and value not in field.enum:
        text = "%s is not one of: %s" % (json.dumps(value),
                                         _listing(field.enum))
        issues.append(error(where, text) if field.closed
                      else warning(where, text + " (new value -- fine if "
                                                 "intentional)"))

    return value, issues


def _keep_integral(original, number):
    """Integral inputs stay ints; 65000000.0 in a source file stays a float."""
    if isinstance(original, int):
        return int(number)
    return number


def _listing(values, limit=8):
    """Join known values for an error message, without printing all of them."""
    values = sorted(values)
    if len(values) <= limit:
        return ", ".join(values)
    return "%s, ... (%d more)" % (", ".join(values[:limit]), len(values) - limit)


def _fmt(number):
    """Format a number the way the messages read best: 4 not 4.0."""
    if isinstance(number, float) and number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return repr(number)


# --------------------------------------------------------------------------- #
# Known values, learned from the dataset that already exists
# --------------------------------------------------------------------------- #

# Free-form fields whose contributed values are worth comparing against what
# the dataset already contains -- a typo ('hierarquical') shows up as a value
# nobody has used before.
LEARNED_ENUM_FIELDS = [
    "CPU Family", "CPU Model", "CPU Generation", "GPU Model",
    "Software Optimization Category", "Decomposition Method",
    "Renumbering Method", "Storage File-System",
]


def learn_enums(dataset_paths):
    """Collect the values each LEARNED_ENUM_FIELDS column already holds."""
    seen = dict((name, set()) for name in LEARNED_ENUM_FIELDS)
    for path in dataset_paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path) as handle:
                records = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            for name in seen:
                value = record.get(name)
                if isinstance(value, str) and not is_missing(value):
                    seen[name].add(value.strip())
    return dict((k, v) for k, v in seen.items() if v)


# --------------------------------------------------------------------------- #
# Loading a submission file
# --------------------------------------------------------------------------- #

def load_submission(path, root=None):
    """Read a submission file into (records, issues).

    Accepts three shapes, so a submission can be hand-written or generated:

        [ {record}, ... ]                      bare list
        {record}                               single record
        {"schema": ..., "meta": {...},         envelope; `meta` supplies the
         "simulations": [ {record}, ... ]}     fields shared by every run

    The envelope shape is one machine's scaling sweep, so it is also checked
    against the machine's ``meta.yaml`` (see ``check_meta_yaml``), searched up
    to `root` -- the submissions directory.  The other two shapes are what the
    aggregate round archives use, where a single machine description would be
    meaningless, and are left alone.
    """
    issues = []
    try:
        with open(path) as handle:
            document = json.load(handle)
    except OSError as exc:
        return [], [error(path, "cannot be read: %s" % exc)]
    except ValueError as exc:
        return [], [error(path, "is not valid JSON: %s" % exc)]

    if isinstance(document, list):
        raw = document
        meta = {}
    elif isinstance(document, dict):
        key = next((k for k in ENVELOPE_RECORDS if k in document), None)
        if key is None:
            raw, meta = [document], {}          # a single bare record
        else:
            raw = document[key]
            meta = document.get("meta") or {}
            schema = document.get("schema")
            if schema is not None and schema != SCHEMA_ID:
                issues.append(warning(path, "schema is %s, this repository "
                                            "speaks %s" % (json.dumps(schema),
                                                           SCHEMA_ID)))
            if not isinstance(meta, dict):
                issues.append(error(path, "'meta' must be an object"))
                meta = {}
            issues.extend(check_meta_yaml(path, meta, path, root))
    else:
        return [], [error(path, "must contain an object or a list of objects")]

    if not isinstance(raw, list):
        return [], issues + [error(path, "the record list must be an array")]
    if not raw:
        return [], issues + [error(path, "contains no records")]

    records = []
    for index, entry in enumerate(raw):
        where = "%s[%d]" % (os.path.basename(path), index)
        if not isinstance(entry, dict):
            issues.append(error(where, "must be an object"))
            continue
        merged = dict(meta)                     # per-record values win
        merged.update(entry)
        records.append((where, merged))
    return records, issues


# --------------------------------------------------------------------------- #
# The machine's meta.yaml
# --------------------------------------------------------------------------- #

def load_meta_yaml(path):
    """Read a flat ``key: value`` YAML mapping.

    Deliberately tiny, and the same reader `benchmark.py` writes with: the
    metadata file is flat, and depending on PyYAML would break this
    repository's standard-library-only contract.  Nested YAML is not rejected,
    it is simply not understood -- an indented line is skipped.
    """
    meta = {}
    with open(path) as handle:
        for line in handle:
            if line[:1] in (" ", "\t"):        # not a top-level key
                continue
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or ":" not in stripped:
                continue
            key, value = stripped.split(":", 1)
            value = value.strip()
            if (value[:1] in ('"', "'") and value[-1:] == value[:1]
                    and len(value) > 1):
                value = value[1:-1]
            meta[key.strip()] = value
    return meta


def meta_search_dirs(path, root=None):
    """The directories a submission's meta.yaml may live in, nearest first.

    The machine folder's own meta.yaml wins, then the submission folder's --
    so a submission covering one machine may keep a single meta.yaml at its
    top instead of one per folder.  The walk stops *below* `root`, the
    submissions directory: one contributor's metadata must never be read as
    another's, and a file dropped straight into submissions/ has nowhere of
    its own to describe, which is what the layout in submissions/README.md
    exists to avoid.

    Outside `root` (a contributor validating a file in their workspace) only
    the file's own directory is looked at, since there is no submission folder
    to bound the walk.
    """
    directory = os.path.dirname(os.path.abspath(path))
    if not root:
        return [directory]
    root = os.path.abspath(root)
    if directory == root:
        return []                   # straight in submissions/: no folder of
                                    # its own, so nothing describes it
    if not directory.startswith(root + os.sep):
        return [directory]          # somewhere else entirely
    dirs = []
    while directory.startswith(root + os.sep):
        dirs.append(directory)
        directory = os.path.dirname(directory)
    return dirs


def find_meta_yaml(path, root=None):
    """The meta.yaml governing submission `path`, or None."""
    for directory in meta_search_dirs(path, root):
        for name in META_NAMES:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def check_meta_yaml(path, meta_block, where, root=None):
    """Check that `path`'s machine description is there and agrees with it.

    An envelope submission is one machine's scaling sweep, so the machine it
    ran on has to be described next to it; a missing meta.yaml is an error.
    What it says is then compared with the `meta` block of the JSON, which is
    the copy that actually reaches data/data.json -- the two disagreeing means
    one of them was edited alone, which is worth a warning.
    """
    meta_path = find_meta_yaml(path, root)
    if meta_path is None:
        return [error(where, "no meta.yaml next to it: every machine in a "
                             "submission carries one, describing the hardware "
                             "the runs were made on. `benchmark.py "
                             "--postProcess` writes it; see "
                             "submissions/README.md")]

    shown = os.path.basename(meta_path)
    try:
        meta_yaml = load_meta_yaml(meta_path)
    except OSError as exc:
        return [error(where, "%s cannot be read: %s" % (shown, exc))]
    if not meta_yaml:
        return [error(where, "%s has no 'key: value' fields" % shown)]

    issues = []
    placeholders = sorted(k for k, v in meta_yaml.items()
                          if str(v).startswith("FIXME"))
    if placeholders:
        issues.append(warning(where, "%s still has placeholder values: %s"
                                     % (shown, _listing(placeholders))))

    for key, field in META_YAML_FIELDS:
        theirs, ours = meta_yaml.get(key), meta_block.get(field)
        if (is_missing(theirs) or is_missing(ours)
                or str(theirs).startswith("FIXME")):
            continue
        if str(theirs).strip().lower() != str(ours).strip().lower():
            issues.append(warning(
                where, "%s says %s: %s, the submission says %s: %s"
                       % (shown, key, json.dumps(str(theirs)), field,
                          json.dumps(str(ours)))))
    return issues


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def backfill(raw, where):
    """Fill in raw fields another column already implies.  Warns, never errors.

    One rule so far.  A handful of published runs recorded no wall clock -- the
    spreadsheet carries a zero in its place -- while Time-To-Solution survived,
    because that column is built from the time per iteration and never touched
    the wall clock.  Reading it back gives the length of a full
    NOMINAL_ITERATIONS run at the pace this one managed.

    For a partial run that is *longer* than the run actually lasted, which is
    why it is worth a warning: the two cost columns derived from it, Total Core
    and Total Node Time, then describe the standard case rather than what was
    really spent.  Those two are dropped here when they are zero, since they
    are zero for exactly the same reason and are recomputed further down.
    """
    issues = []
    record = dict(raw)

    wct = coerce_number(record.get("Run Wall-Clock Time [s]"))
    tts = coerce_number(record.get("Time-To-Solution [h]"))
    if (wct is None or wct <= 0) and tts and tts > 0:
        # Time-To-Solution is published rounded, so the multiplication brings
        # back a tail of float noise; a wall clock is not meaningful below
        # milliseconds anyway.
        recovered = round(tts * 3600.0, 3)
        record["Run Wall-Clock Time [s]"] = recovered
        dropped = [name for name in ("Total Core Time [s]", "Total Node Time [s]")
                   if (coerce_number(record.get(name)) or 0) <= 0
                   and name in record]
        for name in dropped:
            del record[name]
        issues.append(warning(
            "%s 'Run Wall-Clock Time [s]'" % where,
            "is %s, so it was taken from Time-To-Solution instead: %s s, the "
            "length of a %d-iteration run at the reported time per iteration. "
            "A partial run did not last that long%s"
            % ("missing" if wct is None else _fmt(wct),
               _fmt(recovered), NOMINAL_ITERATIONS,
               ", and %s were recomputed from it" % " and ".join(dropped)
               if dropped else "")))
    return record, issues


def validate_record(raw, where, known=None, supplied=None):
    """Check one raw record.  Returns (normalised record, issues).

    The normalised record carries every field in FIELD_ORDER: required values
    as submitted, absent optionals as their canonical placeholder, and derived
    values either as submitted or recomputed.

    ``supplied`` maps a SUPPLIED field name (OHC, Submission Date) to the value
    the merge fills in when the submission omits it -- the current iteration and
    the merge date.  A SUPPLIED field the submission omits and ``supplied`` does
    not cover is simply left absent here: it is not an error at validation time,
    but ``append_submissions.py`` refuses to append a record still missing one.
    """
    known = known or {}
    supplied = supplied or {}
    raw, issues = backfill(raw, where)
    record = {}

    unknown = [k for k in raw if k not in BY_NAME]
    if unknown:
        issues.append(error(where, "unknown field(s): %s -- check the spelling "
                                   "against submissions/README.md"
                            % ", ".join(sorted(json.dumps(k) for k in unknown))))

    for field in FIELDS:
        present = field.name in raw
        value = raw.get(field.name)
        place = "%s '%s'" % (where, field.name)

        if not present or is_missing(value):
            if field.role == REQUIRED:
                issues.append(error(place, "is required but %s"
                                    % ("missing" if not present
                                       else "reported as %s" % json.dumps(value))))
            elif field.role == OPTIONAL:
                record[field.name] = field.default
            elif field.role == SUPPLIED and not is_missing(supplied.get(field.name)):
                filled, field_issues = check_field(
                    field, supplied[field.name], "%s (supplied)" % place)
                issues.extend(field_issues)
                if not any(i.severity == "error" for i in field_issues):
                    record[field.name] = filled
            continue

        checked = field
        if field.name in known and not field.enum:
            checked = Field(field.name, field.kind, field.role,
                            enum=sorted(known[field.name]), closed=False,
                            minimum=field.minimum, positive=field.positive,
                            default=field.default)
        value, field_issues = check_field(checked, value, place)
        issues.extend(field_issues)
        if not any(i.severity == "error" for i in field_issues):
            record[field.name] = value

    issues.extend(cross_check(record, where))
    # A record that is already broken would otherwise drown in "cannot be
    # derived" notes, one per formula that depends on the field it is missing.
    quiet = any(i.severity == "error" for i in issues)
    derived, derived_issues = derive(record, where, quiet)
    issues.extend(derived_issues)
    return derived, issues


def cross_check(record, where):
    """Relationships between fields that no single-field check can see."""
    issues = []
    get = record.get

    mesh, cells = get("Mesh"), coerce_number(get("Number of Cells"))
    if mesh in MESH_CELLS and cells is not None:
        if int(cells) != MESH_CELLS[mesh]:
            issues.append(error("%s 'Number of Cells'" % where,
                                "the %s mesh has %d cells, got %s"
                                % (mesh, MESH_CELLS[mesh], _fmt(cells))))

    nodes, cores = get("Number of Nodes"), get("Number of CPU Cores")
    if isinstance(nodes, int) and isinstance(cores, int) and nodes and cores:
        if cores < nodes:
            issues.append(error("%s 'Number of CPU Cores'" % where,
                                "fewer cores (%d) than nodes (%d) -- cores are "
                                "counted across the whole allocation"
                                % (cores, nodes)))
        elif cores % nodes:
            issues.append(warning("%s 'Number of CPU Cores'" % where,
                                  "%d cores do not divide evenly over %d nodes"
                                  % (cores, nodes)))

    gpus = get("Number of GPU Devices")
    if isinstance(gpus, int) and gpus > 0 and is_missing(get("GPU Model")):
        issues.append(error("%s 'GPU Model'" % where,
                            "is required when the run used %d GPU device(s)"
                            % gpus))
    if gpus == 0 and not is_missing(get("GPU Model")):
        issues.append(warning("%s 'GPU Model'" % where,
                              "names %s but Number of GPU Devices is 0"
                              % json.dumps(get("GPU Model"))))

    wct = coerce_number(get("Run Wall-Clock Time [s]"))
    tpi = coerce_number(get("Time per Iteration [s]"))
    if wct and tpi and tpi > 0:
        # Not a check that the run did NOMINAL_ITERATIONS -- the published data
        # spans 2041 to 16000, which is exactly why the scored columns are
        # normalised.  This only catches the two timings being in different
        # units, which puts the count orders of magnitude out.
        iterations = wct / tpi
        if not 100 <= iterations <= 100000:
            issues.append(warning("%s 'Run Wall-Clock Time [s]'" % where,
                                  "divided by the time per iteration gives "
                                  "%.0f iterations, which is hard to believe "
                                  "-- are both in seconds?" % iterations))
        if tpi > wct:
            issues.append(error("%s 'Time per Iteration [s]'" % where,
                                "is longer than the whole run"))

    # Run Consumed Energy is a measurement, not an input to any of the derived
    # energy columns -- those come from the TDP.  So the only thing to check is
    # that it agrees with the flag that says whether it was measured at all.
    energy = coerce_number(get("Run Consumed Energy [kWh]"))
    tdp = coerce_number(get("System TDP [W]"))
    metered = energy is not None and energy > 1e-9
    if get("Is Energy Reported") == 1 and not metered:
        issues.append(error("%s 'Run Consumed Energy [kWh]'" % where,
                            "is required when Is Energy Reported is 1"))
    elif get("Is Energy Reported") == 0 and metered:
        issues.append(warning("%s 'Is Energy Reported'" % where,
                              "is 0, but an energy of %s kWh is given -- set "
                              "the flag to 1 if the run really was metered"
                              % _fmt(energy)))
    if metered and tdp and wct:
        # The band is wide on purpose: the published measurements run from
        # 0.58x to 2.42x of the TDP envelope, so only a unit slip stands out.
        modelled = tdp * wct / 3.6e6
        if modelled > 0 and not 0.05 <= energy / modelled <= 3.0:
            issues.append(warning("%s 'Run Consumed Energy [kWh]'" % where,
                                  "is %.2gx TDP x wall-clock (%s kWh) -- check "
                                  "the unit"
                                  % (energy / modelled, _fmt(round(modelled, 6)))))

    pre = coerce_number(get("Pre-Processing Wall-Clock Time [s]"))
    # At scale a fast solve with hours of decomposition/I-O is expected, not a
    # unit slip, so the ratio only means something below PREPROC_SCALE_CORES.
    big_scale = isinstance(cores, int) and cores >= PREPROC_SCALE_CORES
    if pre is not None and wct and pre > 10 * wct and not big_scale:
        issues.append(warning("%s 'Pre-Processing Wall-Clock Time [s]'" % where,
                              "is more than ten times the solver time"))
    return issues


# --------------------------------------------------------------------------- #
# Derived quantities
# --------------------------------------------------------------------------- #

def derived_values(record):
    """Compute every DERIVED field from the raw ones, skipping the impossible.

    Two different quantities live in here, and the difference is the whole
    point of the columns:

    * ``Total Core/Node Time`` is what the run *cost*: the measured wall clock
      times the hardware it occupied.
    * the ``*-To-Solution`` and energy columns are what the machine *would*
      need for the standard case: NOMINAL_ITERATIONS iterations at the
      measured time per iteration, drawing the system TDP throughout.  They
      deliberately ignore the wall clock -- runs stop at different iteration
      counts -- and the metered ``Run Consumed Energy``, which not every site
      reports, so that every entry is comparable with every other.

    FVOPS -- Finite VOlume Operations Per Second -- is cells advanced per
    second, after Galeazzo et al., IOP Conf. Ser.: Mater. Sci. Eng. 1312,
    012009 (2024).

    Every formula here reproduces all 385 published records exactly.
    """
    number = coerce_number
    wct = number(record.get("Run Wall-Clock Time [s]"))
    tpi = number(record.get("Time per Iteration [s]"))
    cells = number(record.get("Number of Cells"))
    cores = number(record.get("Number of CPU Cores"))
    nodes = number(record.get("Number of Nodes"))
    tdp = number(record.get("System TDP [W]"))

    values = {}
    if wct is not None and cores:
        values["Total Core Time [s]"] = wct * cores
    if wct is not None and nodes:
        values["Total Node Time [s]"] = wct * nodes

    # The normalised family: one standard case at this machine's pace.
    if tpi:
        tts = NOMINAL_ITERATIONS * tpi / 3600.0
        values["Time-To-Solution [h]"] = tts
        if cores:
            values["Core-Time-To-Solution [h]"] = tts * cores
        if nodes:
            values["Node-Time-To-Solution [h]"] = tts * nodes
        if tdp:
            energy_per_iteration = tdp * tpi
            values["Energy-To-Solution [kWh]"] = tdp * tts / 1000.0
            values["Energy per Iteration [J]"] = energy_per_iteration
            values["Energy per Iteration [kJ]"] = energy_per_iteration / 1000.0

    fvops = cells / tpi if (cells and tpi) else None
    if fvops is not None:
        values["FVOPS"] = fvops
        if nodes:
            values["FVOPS per Node"] = fvops / nodes
        if tdp:
            values["FVOPS per kW"] = fvops / (tdp / 1000.0)
            values["FVOPS per Energy"] = fvops / (tdp * tpi)
            values["FVOPS per Energy per Iteration"] = fvops / (tdp * tpi)
    return values


def derive(record, where, quiet=False):
    """Fill in absent derived fields; cross-check the ones already present.

    `quiet` drops the note about a field that could not be derived, for a
    record whose raw fields are already known to be wrong.
    """
    issues = []
    computed = derived_values(record)
    result = dict(record)

    for field in FIELDS:
        if field.role != DERIVED:
            continue
        value = computed.get(field.name)
        if field.name in record and not is_missing(record[field.name]):
            submitted = coerce_number(record[field.name])
            if value is not None and submitted is not None:
                if _relative(submitted, value) > DERIVATION_TOLERANCE:
                    issues.append(warning(
                        "%s '%s'" % (where, field.name),
                        "submitted as %s, the formula gives %s -- keeping the "
                        "submitted value" % (_fmt(submitted),
                                             _fmt(round(value, 6)))))
            continue
        if value is None:
            if not quiet:
                issues.append(warning("%s '%s'" % (where, field.name),
                                      "cannot be derived from the submitted "
                                      "fields and was left out"))
            continue
        result[field.name] = value
    return result, issues


def _relative(a, b):
    """Relative difference of two numbers, robust around zero."""
    if a is None or b is None:
        return 0.0
    if abs(b) < 1e-12:
        return 0.0 if abs(a) < 1e-12 else float("inf")
    return abs(a - b) / abs(b)


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #

def record_key(record):
    """The natural key of a record, used to spot a resubmitted run."""
    parts = []
    for name in IDENTITY_FIELDS:
        value = record.get(name)
        number = coerce_number(value)
        parts.append(_fmt(number) if number is not None
                     else str(value).strip().lower())
    return "|".join(parts)


def describe(record):
    """One-line description of a record, for reports and commit messages."""
    return "OHC%s / %s / %s / %s mesh / %s nodes / %s cores" % (
        record.get("OHC"), record.get("Contributor ID"), record.get("Track"),
        record.get("Mesh"), record.get("Number of Nodes"),
        record.get("Number of CPU Cores"))


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #

# pandas wrote data/data.json with double_precision=10, so a derived value
# printed at full double precision would stand out among its neighbours.  Very
# small magnitudes are left alone: a few entries carry 1e-32 as a stand-in for
# "no energy reported", and rounding would turn those into a plain zero.
ROUND_DECIMALS = 10
ROUND_FLOOR = 1e-6


def round_like_dataset(value):
    if isinstance(value, float) and abs(value) >= ROUND_FLOOR:
        return round(value, ROUND_DECIMALS)
    return value


def ordered(record):
    """Reorder a record the way data/data.json orders its columns."""
    result = {}
    for name in sorted(FIELD_ORDER):
        if name in record:
            result[name] = round_like_dataset(record[name])
    for name in sorted(record):
        if name not in result:
            result[name] = round_like_dataset(record[name])
    return result


def dumps_record(record, indent=4, level=1):
    """Serialise one record in the style pandas' to_json(indent=4) writes.

    Existing rows in data/data.json were produced by pandas, which separates
    keys from values without a space and escapes forward slashes.  Matching
    that keeps an appended row indistinguishable from its neighbours -- and,
    more importantly, keeps the diff of an append to just the new lines.
    """
    text = json.dumps(ordered(record), indent=indent, separators=(",", ":"),
                      ensure_ascii=True)
    text = text.replace("/", "\\/")
    pad = " " * (indent * level)
    return "\n".join(pad + line if line else line for line in text.splitlines())


def append_records(path, records):
    """Append records to a pandas-style JSON array file, in place.

    The existing bytes are never rewritten: the closing bracket is replaced by
    the new rows plus a fresh bracket, so `git diff` shows additions only.
    """
    try:
        with open(path) as handle:
            text = handle.read()
    except IOError:
        text = "[\n]"                    # first submission into a fresh dataset

    stripped = text.rstrip()
    if not stripped.endswith("]"):
        raise ValueError("%s does not end in a JSON array" % path)
    body = stripped[:-1].rstrip()
    if not body.endswith("[") and not body.endswith(","):
        body += ","                       # separate the last existing row

    rows = ",\n".join(dumps_record(record) for record in records)
    with open(path, "w") as handle:
        handle.write(body + "\n" + rows + "\n]\n")


def load_dataset(path):
    """Read the global dataset, tolerating a not-yet-created file."""
    if not os.path.exists(path):
        return []
    with open(path) as handle:
        return json.load(handle)
