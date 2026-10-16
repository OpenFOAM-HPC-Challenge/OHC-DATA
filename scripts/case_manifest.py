#!/usr/bin/env python3
"""The reference the run copies of the case are checked against.

``data/case-manifest.json`` pins the upstream occDrivAer case at ``CASE_REF``
(common.py): for every file that defines the simulation -- ``Allrun``,
``0.orig/``, ``system/`` and ``constant/`` without the mesh -- it keeps the
text and a digest of what OpenFOAM or bash reads in it.  A Hardware Track run
has to use that case unchanged, so postprocess.py compares every run copy with
the manifest and prints what differs.

The digest ignores comments and layout, so reformatting a file or commenting
on it is no change, while every keyword, value or ``#include`` is.  Not a
change either is what benchmark.py itself writes into a run copy: nCores,
decompositionMethod and nHierarchical in caseDefinition, the multiLevelCoeffs
of decomposeParDict, endTime (``--end-time``) and the MPI launcher of Allrun.
Nor is the choice of controlDict.withWrite over controlDict.noWrite, which the
case allows for benchmarking the I/O as well.

Allrun is adapted to the environment it runs in -- scheduler, OpenFOAM
installation, MPI -- so its other differences are reported as notes; only the
fvSolution variant it selects is a change of the case.

Maintainers write the manifest from the pinned case, which is downloaded
unless a checkout of it is given::

    ./case_manifest.py [CASE_DIR]

Standard library only, like the rest of these scripts.
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CASE_PATH, CASE_REF                         # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(REPO, "data", "case-manifest.json")

# What a run copy is compared on: Allrun, and these directories in full but
# for the mesh and the documentation.
CHECKED_DIRS = ("0.orig", "system", "constant")
SKIPPED = ("polyMesh",)

# Entries benchmark.py sets in a run copy, by file: their values are masked.
MASKED = (
    (r"system/include/caseDefinition$",
     ("nCores", "decompositionMethod", "nHierarchical")),
    (r"system/controlDict(\..*)?$", ("endTime",)),
)
MASK = "<set by benchmark.py>"

# The controlDict variants the case allows Allrun to select.
CONTROL_DICTS = ("controlDict.noWrite", "controlDict.withWrite")

# The launcher lines of Allrun, stock and as allrun_with_launcher() leaves
# them; either is the stock launch.
LAUNCHER_LINES = ('parEx="mpirun -np ${nProcs} ${OMPI_FLAGS}"',
                  'parEx="${MPI_LAUNCHER} ${nProcs} ${OMPI_FLAGS}"')


def case_files(root):
    """The checked files under `root`, as sorted '/'-separated paths."""
    found = []
    if os.path.isfile(os.path.join(root, "Allrun")):
        found.append("Allrun")
    for top in CHECKED_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(root, top)):
            dirnames[:] = [d for d in dirnames if d not in SKIPPED]
            for name in filenames:
                if name.endswith(".md"):
                    continue
                path = os.path.relpath(os.path.join(dirpath, name), root)
                found.append(path.replace(os.sep, "/"))
    return sorted(found)


def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _drop_block(text, keyword):
    """`text` without the `keyword { ... }` dictionary."""
    m = re.search(r"\b%s\s*\{" % keyword, text)
    if not m:
        return text
    depth, i = 0, m.end() - 1
    while i < len(text):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[:m.start()] + text[i + 1:]
        i += 1
    return text


def _foam_lines(rel, text):
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    text = _drop_block(text, "multiLevelCoeffs")
    for pattern, keys in MASKED:
        if re.match(pattern, rel):
            for key in keys:
                text = re.sub(r"(?m)^(\s*%s)\s[^;]*;" % key,
                              r"\1 %s;" % MASK, text)
    lines = []
    for line in text.splitlines():
        line = " ".join(re.sub(r"([;{}()\[\]])", r" \1 ", line).split())
        if line:
            lines.append(line)
    return lines


def _shell_lines(text):
    lines = []
    for line in text.splitlines():
        line = " ".join(re.sub(r"\s#.*$", "", line.strip()).split())
        if not line or line.startswith("#"):
            continue
        if re.match(r"(OMPI_FLAGS|MPI_LAUNCHER)=", line):
            continue
        if line in LAUNCHER_LINES:
            line = "parEx=%s" % MASK
        if line in ["controlDict=%s" % name for name in CONTROL_DICTS]:
            line = "controlDict=<%s>" % " or ".join(CONTROL_DICTS)
        lines.append(line)
    return lines


def normalize(rel, text):
    """The lines of `text` as they are compared: without comments, blank
    lines and layout, and with what benchmark.py sets masked."""
    if os.path.basename(rel).startswith("Allrun"):
        return _shell_lines(text)
    return _foam_lines(rel, text)


def digest(lines):
    return hashlib.sha256(" ".join(lines).encode("utf-8")).hexdigest()


def build(case_dir, ref=CASE_REF):
    """The manifest of the case in `case_dir`."""
    files = {}
    for rel in case_files(case_dir):
        text = read(os.path.join(case_dir, rel.replace("/", os.sep)))
        files[rel] = {"digest": digest(normalize(rel, text)), "text": text}
    # Allrun copies the variant it is set to over system/controlDict and
    # system/fvSolution, so after a run those hold the variant: any of the
    # allowed controlDict variants, and the fvSolution one Allrun selects.
    allowed = {"controlDict": list(CONTROL_DICTS),
               "fvSolution": [selected_fvsolution(
                   files.get("Allrun", {}).get("text", ""))]}
    for name, variants in sorted(allowed.items()):
        variants = ["system/" + v for v in variants if v
                    and "system/" + v in files]
        if variants and "system/" + name in files:
            files["system/" + name]["variants"] = variants
    return {"case_path": CASE_PATH, "case_ref": ref, "files": files}


def selected_fvsolution(allrun_text):
    """The fvSolution variant an Allrun copies into place, or None."""
    m = re.search(r"(?m)^\s*fvSolution=(\S+)", allrun_text)
    return m.group(1) if m else None


def load(path=MANIFEST):
    """The manifest, or None outside a checkout that has one."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def compare(run_dir, manifest):
    """How the case in `run_dir` differs from `manifest`.

    Returns [(rel, kind, diff lines)], kind one of 'changed', 'missing',
    'extra' and 'noted' -- a difference of Allrun that leaves the fvSolution
    variant it selects as it is; the diff, of the compared lines, only for
    'changed' and 'noted'.
    """
    files = manifest["files"]
    present = case_files(run_dir)
    found = []
    for rel in sorted(set(files) | set(present)):
        if rel not in present:
            found.append((rel, "missing", []))
            continue
        if rel not in files:
            found.append((rel, "extra", []))
            continue
        text = read(os.path.join(run_dir, rel.replace("/", os.sep)))
        lines = normalize(rel, text)
        candidates = [rel] + files[rel].get("variants", [])
        if digest(lines) in [files[c]["digest"] for c in candidates]:
            continue
        ref_rel = candidates[-1]
        diff = difflib.unified_diff(normalize(ref_rel, files[ref_rel]["text"]),
                                    lines, "manifest/" + ref_rel, rel,
                                    n=2, lineterm="")
        kind = "changed"
        if rel == "Allrun" and selected_fvsolution(text) == \
                selected_fvsolution(files[rel]["text"]):
            kind = "noted"
        found.append((rel, kind, list(diff)))
    return found


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Write data/case-manifest.json from the pinned case.")
    p.add_argument("case_dir", nargs="?",
                   help="a checkout of the case at %s (default: download it)"
                        % CASE_REF)
    p.add_argument("-o", "--output", default=MANIFEST,
                   help="where to write it (default: %(default)s)")
    args = p.parse_args(argv)

    if args.case_dir:
        manifest = build(args.case_dir)
    else:
        import benchmark
        with tempfile.TemporaryDirectory() as tmp:
            manifest = build(benchmark.prepare_case(tmp))
    with open(args.output, "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
        f.write("\n")
    print("wrote %s: %d file(s) of %s at %s"
          % (args.output, len(manifest["files"]), CASE_PATH, CASE_REF))


if __name__ == "__main__":
    main()
