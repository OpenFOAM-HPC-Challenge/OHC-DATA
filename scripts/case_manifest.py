#!/usr/bin/env python3
"""The reference the run copies of the case are checked against.

The case is the upstream occDrivAer case on the develop branch of the
OpenFOAM HPC committee repository.  ``<workspace>/case-manifest.json``
records it as it was downloaded into the workspace: the commit the archive
was made from and, for every file that defines the simulation -- ``Allrun``,
``0.orig/``, ``system/`` and ``constant/`` without the mesh -- the text and a
digest of what OpenFOAM or bash reads in it.  A Hardware Track run has to use
that case unchanged, so postprocess.py compares every run copy with the
manifest and prints what differs.

The manifest is written once per workspace, from the downloaded archive
rather than the extracted case, so editing the case after the download does
not change it.  It is always the upstream case: runs copied from a case of
your own (``benchmark.py --case``) are checked against develop as well.

The digest ignores comments and layout, so reformatting a file or commenting
on it is no change, while every keyword, value or ``#include`` is.  Not a
change either is what benchmark.py itself writes into a run copy: nCores,
decompositionMethod and nHierarchical in caseDefinition, the multiLevelCoeffs
of decomposeParDict, endTime (``--end-time``) and the MPI launcher of an
Allrun that does not take it from the environment yet.  Nor is the choice of
controlDict.withWrite over controlDict.noWrite, which the case allows for
benchmarking the I/O as well.

Allrun is adapted to the environment it runs in -- scheduler, OpenFOAM
installation, MPI -- so its other differences are reported as notes; only the
fvSolution variant it selects is a change of the case.

postprocess.py writes the manifest when benchmark.py has not; so does::

    ./case_manifest.py --workspace PATH

Standard library only, like the rest of these scripts.
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (CASE_ARCHIVE_URL, CASE_BRANCH, CASE_PATH,  # noqa: E402
                    CASE_PROJECT_URL, CASE_TARBALL, CASE_WEB_URL)

# The manifest's file name in the workspace.
MANIFEST_NAME = "case-manifest.json"

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

# The launcher lines of Allrun: with mpirun hard-coded, as the case had it
# at first, and with the launcher from the environment, as the case has it
# now and allrun_with_launcher() makes of the first; either is the stock
# launch.
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


def build(case_dir, ref):
    """The manifest of the case in `case_dir`, the upstream one at `ref`."""
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
    return {"source": CASE_PROJECT_URL, "branch": CASE_BRANCH,
            "case_path": CASE_PATH, "case_ref": ref, "files": files}


def selected_fvsolution(allrun_text):
    """The fvSolution variant an Allrun copies into place, or None."""
    m = re.search(r"(?m)^\s*fvSolution=(\S+)", allrun_text)
    value = m.group(1) if m else None
    if value and len(value) >= 2 and value[0] in "'\"" \
            and value[-1] == value[0]:
        value = value[1:-1]
    return value


def case_digest(manifest):
    """One digest of the whole case: of its files' digests, by path."""
    return digest(["%s:%s" % (rel, entry["digest"])
                   for rel, entry in sorted(manifest["files"].items())])


def archive_commit(tarball):
    """The commit `tarball` was made from, or None.

    git archive, which GitLab serves archives with, stores it in the pax
    header, also when the archive was asked for by branch name.
    """
    with tarfile.open(tarball, "r:gz") as tar:
        commit = tar.pax_headers.get("comment", "")
    return commit if re.match(r"[0-9a-f]{40}$", commit) else None


def from_archive(tarball):
    """The manifest of the case in `tarball`, or None when the archive does
    not name the commit it was made from."""
    import benchmark
    commit = archive_commit(tarball)
    if commit is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        case_dir = benchmark.extract(tarball, tmp, locate=benchmark.find_case,
                                     what="case")
        return build(case_dir, commit)


def manifest_path(workspace):
    return os.path.join(workspace, MANIFEST_NAME)


def load(path):
    """The manifest at `path`, or None when there is none."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def reference(workspace):
    """The manifest the runs of `workspace` are checked against, or None.

    Written on first use from the case archive in the workspace -- the one
    benchmark.py extracts the case from, downloaded here when the runs were
    copied from a case of your own -- and kept, so a workspace stays checked
    against the commit it was set up with.  None, with the reason printed,
    when develop cannot be reached.
    """
    import benchmark
    path = manifest_path(workspace)
    manifest = load(path)
    if manifest is not None:
        return manifest

    tarball = os.path.join(workspace, CASE_TARBALL)
    os.makedirs(os.path.dirname(tarball), exist_ok=True)
    try:
        benchmark.download(CASE_ARCHIVE_URL, tarball)
    except OSError as exc:
        print("warning: cannot download the case from %s: %s"
              % (CASE_WEB_URL, exc), file=sys.stderr)
        return None
    manifest = from_archive(tarball)
    if manifest is None:
        print("warning: %s names no commit, so it is not the archive of "
              "%s" % (tarball, CASE_WEB_URL), file=sys.stderr)
        return None
    with open(path, "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
        f.write("\n")
    return manifest


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
        description="Write the manifest of the upstream case into a "
                    "workspace, downloading the case if it is not there.")
    p.add_argument("--workspace", default=os.path.join(os.getcwd(),
                                                       "workspace"),
                   metavar="PATH",
                   help="the benchmark.py workspace (default: ./workspace)")
    args = p.parse_args(argv)

    manifest = reference(args.workspace)
    if manifest is None:
        sys.exit("error: no manifest written")
    print("%s: %d file(s) of %s at %s"
          % (manifest_path(args.workspace), len(manifest["files"]),
             CASE_PATH, manifest["case_ref"]))


if __name__ == "__main__":
    main()
