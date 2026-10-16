#!/usr/bin/env python3
"""Shared configuration and helpers for the occDrivAer benchmark drivers.

``benchmark.py`` sets runs up, ``postprocess.py`` harvests them again; this
module holds what both of them need -- the mesh catalogue, the paths inside a
case, a handful of file/text helpers and the ``argparse`` types and options
they have in common.  Standard library only, like its two callers.
"""

import argparse
import os
import re

# Mesh resolution -> (tarball name, human label).  URLs and cell counts are
# taken from the case README / Zenodo record 15012221.
MESHES = {
    "coarse": ("polyMesh_65M.tar.gz", "65M"),
    "medium": ("polyMesh_110M.tar.gz", "110M"),
    "fine": ("polyMesh_236M.tar.gz", "236M"),
}

ZENODO_URL = "https://zenodo.org/records/15012221/files/{}?download=1"

# Cell count per mesh resolution, used for the FVOPS throughput metric.  These
# are the nominal counts the challenge reports, matching OHCParser.get_cells().
MESH_CELLS = {"coarse": 65e6, "medium": 110e6, "fine": 236e6}

# The directory holding these scripts is treated as the pristine source case
# when it is one -- i.e. when the scripts were dropped into a case checkout.
# Otherwise benchmark.py downloads the case (see CASE_ARCHIVE_URL).
CASE_DIR = os.path.dirname(os.path.abspath(__file__))

CASE_DEFINITION = os.path.join("system", "include", "caseDefinition")

# Upstream home of the case itself, in the OpenFOAM HPC committee repository.
CASE_PROJECT_URL = "https://develop.openfoam.com/committees/hpc"
# Pinned to a commit: a Hardware Track run uses this case unchanged, which
# postprocess.py checks against data/case-manifest.json, written from it.
CASE_REF = "84c262431117f5c921db8335e368a12d0e9fa3f0"
CASE_PATH = "incompressible/simpleFoam/occDrivAerStaticMesh"
CASE_WEB_URL = "%s/-/tree/%s/%s" % (CASE_PROJECT_URL, CASE_REF, CASE_PATH)

# GitLab serves any subdirectory of a repository as its own tarball, so the
# case comes down in one request and without a git client.  The archive still
# carries the full path inside it, hence find_case() rather than a fixed
# strip-components count.
CASE_ARCHIVE_URL = ("%s/-/archive/%s/hpc-%s.tar.gz?path=%s"
                    % (CASE_PROJECT_URL, CASE_REF, CASE_REF, CASE_PATH))


def is_case(path):
    """True when `path` looks like an occDrivAer case directory."""
    return os.path.isfile(os.path.join(path, CASE_DEFINITION))


# --------------------------------------------------------------------------- #
# File / text helpers
# --------------------------------------------------------------------------- #

def read_text(path):
    """Return the contents of `path`, or None if it does not exist."""
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return None


def display_path(path, base):
    """Shorten `path` against `base`, but never into a '../../..' chain."""
    rel = os.path.relpath(path, base)
    return path if rel.startswith(os.pardir) else rel


def sanitize(text):
    """Reduce `text` to something safe for a file or directory name."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)).strip("_")
    return cleaned or "run"


# --------------------------------------------------------------------------- #
# Shared CLI surface
# --------------------------------------------------------------------------- #

def parse_posint_list(value):
    out = []
    for tok in value.replace(",", " ").split():
        n = int(tok)
        if n < 1:
            raise argparse.ArgumentTypeError("value must be >= 1: %s" % tok)
        out.append(n)
    if not out:
        raise argparse.ArgumentTypeError("no values given")
    return out


def parse_start(value):
    parts = [int(x) for x in value.replace(",", " ").split()]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("start must be 3 integers, e.g. 1,1,1")
    return parts


def parse_meshes(value):
    if value.lower() == "all":
        return list(MESHES)
    out = []
    for tok in value.replace(",", " ").split():
        if tok not in MESHES:
            raise argparse.ArgumentTypeError(
                "unknown mesh %r (choose from %s or 'all')"
                % (tok, ", ".join(MESHES)))
        out.append(tok)
    return out


# The --decomposition names: OpenFOAM's method first, then what varies within
# it.  Every one but the default lives in run directories suffixed with its
# name (512-scotch), so decompositions sit side by side in one workspace and
# postprocess.py can tell their sweeps apart.
DECOMPOSITIONS = ("hierarchical-doubling", "hierarchical-optimal", "scotch",
                  "multilevel-scotch", "multilevel-metis", "multilevel-kahip")
DEFAULT_DECOMPOSITION = DECOMPOSITIONS[0]


def run_dirname(name, decomposition):
    """A run directory's name: `name` (the nCores or nodes it is keyed on),
    suffixed with the decomposition unless that is the default."""
    if decomposition == DEFAULT_DECOMPOSITION:
        return str(name)
    return "%s-%s" % (name, decomposition)


def parse_run_dirname(dirname, decomposition):
    """The number a run directory of `decomposition` is keyed on, or None
    when `dirname` is not one of that decomposition's run directories."""
    m = re.match(r"(\d+)(?:-(.+))?$", dirname)
    if not m or (m.group(2) or DEFAULT_DECOMPOSITION) != decomposition:
        return None
    return int(m.group(1))


def check_method_available(method="scotch"):
    """(ok, message) for whether `method` is really linked into OpenFOAM.

    OpenFOAM ships dummy stubs that link but abort at run time, so the
    presence of a library name is not enough -- it must not be the dummy.
    """
    libbin = os.environ.get("FOAM_LIBBIN")
    if not libbin:
        return None, "FOAM_LIBBIN unset; source the OpenFOAM bashrc to check"
    if method in ("hierarchical", "simple", "none"):
        return True, "%s is built in" % method
    real = os.path.join(libbin, "lib%sDecomp.so" % method)
    dummy = os.path.join(libbin, "dummy", "lib%sDecomp.so" % method)
    if os.path.exists(real):
        return True, "%s available (%s)" % (method, real)
    if os.path.exists(dummy):
        return False, ("only the dummy stub for %s is present (%s); it links "
                       "but aborts at run time -- build ThirdParty %s first"
                       % (method, dummy, method))
    return False, "no %sDecomp library found under %s" % (method, libbin)


def add_shared_arguments(p):
    """Add the options both drivers take: which meshes, and where they live."""
    p.add_argument("--mesh", type=parse_meshes, default=list(MESHES),
                   metavar="LIST",
                   help="mesh resolution(s): any of fine,medium,coarse or "
                        "'all' (default: all)")
    p.add_argument("--workspace", default=os.path.join(os.getcwd(), "workspace"),
                   metavar="PATH",
                   help="workspace directory holding meshes/ and runs/ "
                        "(default: ./workspace)")
    return p
