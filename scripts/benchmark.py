#!/usr/bin/env python3
"""Benchmark automation for the occDrivAer static-mesh case.

This is a self-contained driver (Python standard library only, no external
modules) for setting up strong-scaling benchmark runs of the OpenFOAM HPC
Challenge occDrivAer case.  It

  1. downloads and extracts the requested polyMesh resolution(s)
     (fine / medium / coarse) from Zenodo -- by default all three,
  2. builds one self-contained case copy per (mesh, nCores) combination under
     ``<workspace>/runs/<mesh>/<nCores>/``, with the polyMesh symlinked to a
     shared, downloaded-once copy,
  3. prepares the hierarchical decomposition for each nCores using the
     ``decompositionIter`` doubling strategy and writes it into that copy's
     ``system/include/caseDefinition``,
  4. renders a SLURM batch script from ``--slurm-template`` (or a built-in
     default) that supplies the sbatch directives and calls ``Allrun``,
  5. optionally submits every generated script with ``sbatch`` (``--submit``).

With ``--postProcess`` it instead harvests finished (or partial) runs back out
of the workspace: it parses the ``logFiles/`` written by ``Allrun`` together
with the ``postProcessing/`` function-object output and emits one submission
file per mesh -- by default the full YAML document, following the data model of
the OpenFOAM HPC Challenge result spreadsheets (``META Data`` /
``Simulations`` / ``Aero Forces``), and with ``--format json`` the flat JSON
this repository's ``submissions/`` directory takes (see
``submissions/README.md``).

The harvest is laid out as one directory per submission and, inside it, one
per machine::

    submissions/<YYYYMMDD>_<handle>/<machine>/
        meta.yaml
        OHC1_DrivAer_<machine>_<mesh>.json

``<handle>`` comes from ``--suffix`` (or ``legend_suffix`` in the metadata) and
``<machine>`` from ``--machine``, which defaults to the scheduler's cluster
name and can be turned off with ``--machine -``.  Harvesting a second machine
with the same ``--suffix`` puts it beside the first, so one submission can
cover several machines, each with the ``meta.yaml`` describing its hardware.

Examples
--------
    # Prepare all meshes at 128 and 256 cores (does not submit)
    ./benchmark.py --ncores 128,256

    # Only the coarse mesh, a scaling sweep, submitted to the queue
    ./benchmark.py --mesh coarse --ncores 32,64,128,256,512 \
                   --slurm-template slurm.template --submit

    # Custom workspace location
    ./benchmark.py --ncores 512 --workspace /scratch/$USER/occDrivAer

    # Harvest the finished runs into YAML submissions
    ./benchmark.py --postProcess --workspace /scratch/$USER/occDrivAer

    # ... or into the JSON a submissions/ pull request is made of
    ./benchmark.py --postProcess --workspace /scratch/$USER/occDrivAer \
                   --format json
"""

import argparse
import datetime
import json
import itertools
import os
import re
import shutil
import socket
import string
import subprocess
import sys
import tarfile
import urllib.request

# Optional: decomposition methods that need more than a coefficient triplet
# (currently multiLevel) live in a sibling module.  Absent is fine -- the two
# geometric strategies below are self-contained.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import decompositionPlugin
except ImportError:
    decompositionPlugin = None

# --------------------------------------------------------------------------- #
# Static configuration
# --------------------------------------------------------------------------- #

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

# Per-node topology defaults for each hwloc object a placement sweep can map
# by, for the 2x AMD EPYC 9655 nodes this case is tuned on: 8 NUMA domains of
# 24 cores, which are themselves 24 L3 slices (CCDs) of 8 cores.  Mapping by
# NUMA alone packs ranks into the low L3 slices and leaves the rest idle, so
# the finer object is often the one worth sweeping.  Override per site with
# --domains-per-node / --cores-per-domain.
BIND_OBJECTS = {
    "numa":    {"domains_per_node": 8,  "cores_per_domain": 24},
    "l3cache": {"domains_per_node": 24, "cores_per_domain": 8},
    "package": {"domains_per_node": 2,  "cores_per_domain": 96},
}

# Directory that contains this script is treated as the pristine source case.
CASE_DIR = os.path.dirname(os.path.abspath(__file__))

CASE_DEFINITION = os.path.join("system", "include", "caseDefinition")

# Names never copied into a run directory (large, generated, or irrelevant).
IGNORE_NAMES = {
    ".git", "figures", "workspace", "logFiles", "postProcessing",
    "polyMesh", "meshes", "runs", "__pycache__",
    "benchmark.py", "slurm.template",
}

# Built-in SLURM template used when --slurm-template is not given.  Placeholders
# use string.Template syntax ($name); bash ${VAR} / $VAR expansions are left
# untouched because their names are not in the substitution mapping.
DEFAULT_SLURM_TEMPLATE = """\
#!/bin/bash -l
#SBATCH --job-name=$jobname
#SBATCH --ntasks=$ncores
#SBATCH --ntasks-per-node=$tpn
#SBATCH --nodes=$nodes
#SBATCH --partition=arm
#SBATCH --output=%x-%j.out
#SBATCH --error=%x-%j.err
$extrasbatch

export OMPI_FLAGS="$ompiflags"

cd $casedir
./Allrun
"""


# --------------------------------------------------------------------------- #
# Decomposition
# --------------------------------------------------------------------------- #

class decompositionIter:
    """Infinite iterator yielding hierarchical decomposition triplets.

    Starting from `start` (e.g. [2,1,1] for a 2,1,1 base decomposition), each
    call to __next__ returns the current triplet and then doubles the next
    dimension in a cyclic x->y->z->x order.  This matches OpenFOAM's
    hierarchicalCoeffs doubling strategy for strong-scaling studies.
    """

    def __init__(self, start=[1, 1, 1], multiplier=2):
        self.current = list(start)
        self.multiplier = multiplier
        self.dimIter = itertools.cycle([0, 1, 2])

    def __iter__(self):
        return self

    def __next__(self):
        triplet = list(self.current)
        dim = next(self.dimIter)
        self.current[dim] *= self.multiplier
        return triplet


def balanced_triplet(ncores):
    """Fallback decomposition for core counts unreachable by pure doubling.

    Distributes the prime factors of `ncores` across the three dimensions,
    always growing the currently smallest dimension, so the product is exactly
    `ncores` and the triplet is as balanced as possible.
    """
    factors = []
    n, d = ncores, 2
    while d * d <= n:
        while n % d == 0:
            factors.append(d)
            n //= d
        d += 1
    if n > 1:
        factors.append(n)

    dims = [1, 1, 1]
    for f in sorted(factors, reverse=True):
        i = dims.index(min(dims))
        dims[i] *= f
    return dims


def domain_extents(case_dir=None):
    """Bounding-box side lengths (Lx, Ly, Lz) read from blockMeshDict.

    Returns None if the dictionary cannot be parsed, so callers can fall back
    to the index-space heuristic.
    """
    path = os.path.join(case_dir or CASE_DIR, "system", "blockMeshDict")
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return None

    bounds = {}
    for name in ("xMin", "xMax", "yMin", "yMax", "zMin", "zMax"):
        m = re.search(r"(?m)^\s*%s\s+(-?[0-9.eE+-]+)\s*;" % name, text)
        if not m:
            return None
        bounds[name] = float(m.group(1))

    extents = (bounds["xMax"] - bounds["xMin"],
               bounds["yMax"] - bounds["yMin"],
               bounds["zMax"] - bounds["zMin"])
    return extents if all(e > 0 for e in extents) else None


def optimal_triplet(ncores, extents):
    """Factorisation of `ncores` minimising the inter-subdomain interface area.

    A balanced split is only right for a cubic domain.  This case is 120 x 44
    x 20, so the cut counts should follow the side lengths -- otherwise the
    subdomains are long slabs and the halo is far larger than it needs to be.
    """
    lx, ly, lz = extents
    divisors = [d for d in range(1, ncores + 1) if ncores % d == 0]
    best, best_area = None, None
    for nx in divisors:
        rest = ncores // nx
        for ny in (d for d in divisors if rest % d == 0 and d <= rest):
            nz = rest // ny
            area = ((nx - 1) * ly * lz + (ny - 1) * lx * lz
                    + (nz - 1) * lx * ly)
            if best_area is None or area < best_area:
                best, best_area = (nx, ny, nz), area
    return list(best)


def hierarchical_for(ncores, start):
    """Return the hierarchical triplet [nx, ny, nz] whose product is `ncores`.

    Uses the decompositionIter doubling strategy first (the intended path for
    power-of-two core counts); otherwise falls back to a balanced factorisation
    and warns, since the doubling sequence only produces power-of-two products.
    """
    it = decompositionIter(start=start)
    for _ in range(1024):  # generous bound; products grow exponentially
        triplet = next(it)
        product = triplet[0] * triplet[1] * triplet[2]
        if product == ncores:
            return triplet
        if product > ncores:
            break

    triplet = balanced_triplet(ncores)
    print("  ! %d not reachable by doubling from %s; using balanced %s"
          % (ncores, start, tuple(triplet)))
    return triplet


# --------------------------------------------------------------------------- #
# Mesh download / extraction
# --------------------------------------------------------------------------- #

def download(url, dest):
    """Stream `url` to `dest`, skipping if already present. Atomic via .part."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print("  = already downloaded: %s" % os.path.basename(dest))
        return

    part = dest + ".part"
    print("  > downloading %s" % url)
    req = urllib.request.Request(url, headers={"User-Agent": "benchmark.py"})
    with urllib.request.urlopen(req) as resp, open(part, "wb") as out:
        total = int(resp.headers.get("Content-Length", 0))
        read = 0
        chunk = 1 << 20  # 1 MiB
        while True:
            buf = resp.read(chunk)
            if not buf:
                break
            out.write(buf)
            read += len(buf)
            if total:
                pct = 100.0 * read / total
                sys.stdout.write("\r    %6.1f%% (%d/%d MiB)"
                                 % (pct, read >> 20, total >> 20))
            else:
                sys.stdout.write("\r    %d MiB" % (read >> 20))
            sys.stdout.flush()
    sys.stdout.write("\n")
    os.rename(part, dest)


def find_polymesh(root):
    """Locate a polyMesh directory (one containing 'points') under `root`."""
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.basename(dirpath) == "polyMesh" and (
                "points" in filenames or "points.gz" in filenames):
            return dirpath
    return None


def extract(tarball, destdir):
    """Extract `tarball` into `destdir`, returning the polyMesh path."""
    existing = find_polymesh(destdir)
    if existing:
        print("  = already extracted: %s" % existing)
        return existing

    print("  > extracting %s" % os.path.basename(tarball))
    with tarfile.open(tarball, "r:gz") as tar:
        # 'data' filter (py>=3.12) guards against path traversal; fall back
        # gracefully on older interpreters that lack the argument.
        try:
            tar.extractall(destdir, filter="data")
        except TypeError:
            tar.extractall(destdir)

    polymesh = find_polymesh(destdir)
    if not polymesh:
        raise RuntimeError("no polyMesh found after extracting %s" % tarball)
    return polymesh


def prepare_mesh(mesh, meshes_dir):
    """Download and extract one mesh, returning the shared polyMesh path."""
    tarname, label = MESHES[mesh]
    print("[mesh] %s (%s cells)" % (mesh, label))
    tarball = os.path.join(meshes_dir, tarname)
    extract_dir = os.path.join(meshes_dir, mesh)
    os.makedirs(extract_dir, exist_ok=True)

    download(ZENODO_URL.format(tarname), tarball)
    return extract(tarball, extract_dir)


# --------------------------------------------------------------------------- #
# Case copy / configuration
# --------------------------------------------------------------------------- #

def _ignore(dirpath, names):
    ignored = set()
    for name in names:
        if name in IGNORE_NAMES or name.startswith("processor") \
                or name.endswith((".out", ".err", ".part")):
            ignored.add(name)
    return ignored


def copy_case(dst):
    """Copy the pristine case into `dst` (excluding mesh / generated files)."""
    shutil.copytree(CASE_DIR, dst, ignore=_ignore,
                    symlinks=True, dirs_exist_ok=True)


def link_polymesh(run_dir, polymesh):
    """Symlink <run>/constant/polyMesh to the shared extracted polyMesh."""
    target = os.path.join(run_dir, "constant", "polyMesh")
    if os.path.islink(target) or os.path.exists(target):
        if os.path.islink(target):
            os.unlink(target)
        else:
            shutil.rmtree(target)
    os.symlink(os.path.abspath(polymesh), target)


def set_decomposition(run_dir, ncores, triplet):
    """Rewrite nCores and nHierarchical in the run's caseDefinition."""
    path = os.path.join(run_dir, CASE_DEFINITION)
    with open(path) as f:
        text = f.read()

    ntuple = "(%d %d %d)" % (triplet[0], triplet[1], triplet[2])
    text, n1 = re.subn(r"(?m)^(\s*nCores\s+)\S+;",
                       r"\g<1>%d;" % ncores, text)
    text, n2 = re.subn(r"(?m)^(\s*nHierarchical\s+)\([^)]*\);",
                       r"\g<1>%s;" % ntuple, text)
    if n1 != 1 or n2 != 1:
        raise RuntimeError("could not update caseDefinition in %s "
                           "(nCores=%d, nHierarchical=%d matches)"
                           % (path, n1, n2))

    with open(path, "w") as f:
        f.write(text)


def numa_placement(ranks_per_domain, cores_per_domain, bind_object="numa"):
    """OpenMPI flags pinning `ranks_per_domain` ranks into every domain.

    `bind_object` is any hwloc object PRRTE can map by.  'numa' spreads across
    memory domains; 'l3cache' is finer on chiplet CPUs, where several L3
    domains share one NUMA node and mapping by NUMA alone leaves whole L3
    slices idle.

    Beyond one rank per physical core the binding unit has to drop from core
    to hardware thread, otherwise there are not enough cores in the domain to
    bind to and mpirun refuses to launch.
    """
    unit = "core" if ranks_per_domain <= cores_per_domain else "hwthread"
    return ("--report-bindings --map-by ppr:%d:%s --bind-to %s"
            % (ranks_per_domain, bind_object, unit))


def set_end_time(run_dir, end_time):
    """Rewrite endTime in both controlDict variants of one run copy.

    Allrun copies whichever variant it is configured for over system/controlDict
    at launch, so both have to be patched for the setting to stick regardless
    of which one is selected.
    """
    patched = []
    for name in ("controlDict.noWrite", "controlDict.withWrite"):
        path = os.path.join(run_dir, "system", name)
        if not os.path.exists(path):
            continue
        with open(path) as f:
            text = f.read()
        text, count = re.subn(r"(?m)^(\s*endTime\s+)\d+;",
                              r"\g<1>%d;" % end_time, text)
        if count != 1:
            raise RuntimeError("could not set endTime in %s (%d matches)"
                               % (path, count))
        with open(path, "w") as f:
            f.write(text)
        patched.append(name)

    if not patched:
        raise RuntimeError("no controlDict variant found under %s" % run_dir)
    return patched


def render_slurm(template_text, run_dir, mesh, ncores, triplet, jobname, tpn,
                 ompiflags="", extra_sbatch="", label=None):
    """Render the SLURM script for one run and write it as run.slurm.

    `label` disambiguates the job name when nCores alone does not -- a
    fixed-total-ranks sweep runs the same core count on several node counts.
    """
    nodes = (ncores + tpn - 1) // tpn  # ceil division
    mapping = {
        "jobname": "%s_%s_%s" % (jobname, mesh, label if label else ncores),
        "ncores": ncores,
        "nx": triplet[0], "ny": triplet[1], "nz": triplet[2],
        "nhierarchical": "(%d %d %d)" % tuple(triplet),
        "mesh": mesh,
        "tpn": tpn,
        "nodes": nodes,
        "casedir": os.path.abspath(run_dir),
        "ompiflags": ompiflags,
        "extrasbatch": extra_sbatch,
    }
    text = string.Template(template_text).safe_substitute(mapping)
    path = os.path.join(run_dir, "run.slurm")
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)
    return path


# --------------------------------------------------------------------------- #
# Post-processing: logs -> YAML submission
# --------------------------------------------------------------------------- #

# The four pre-processing stages Allrun runs (and times) before the solver, in
# the order they appear both in logFiles/ and in the SLURM .err file.
PREP_STAGES = [
    ("decomposePar", "10_decomposePar.log"),
    ("renumberMesh", "20_renumberMesh.log"),
    ("potentialFoam", "30_potentialFoam.log"),
    ("applyBoundaryLayer", "40_applyBoundaryLayer.log"),
]

SOLVER_LOG_GLOB = "50_simpleFoam."

# OpenFOAM renumberMesh reports its internal method name; the challenge
# spreadsheet uses the common abbreviation.
RENUMBER_ALIASES = {"CuthillMcKee": "RCM", "reverseCuthillMcKee": "RCM"}

# Fields that cannot be derived from the logs and therefore have to come from
# the metadata file.  Values here are the placeholders written into a freshly
# generated template.
METADATA_TEMPLATE = [
    ("# OHC-1 submission metadata for benchmark.py --postProcess.", None),
    ("# Everything that cannot be read out of the OpenFOAM logs lives here;", None),
    ("# edit it once, then re-run --postProcess as often as you like.", None),
    ("", None),
    ("test_case", "Test case 2a"),
    ("track", "Hardware Track"),
    ("ohc_round", 2),
    ("participant_id", "FIXME"),
    ("lastname", "FIXME"),
    ("firstname", "FIXME"),
    ("affiliation", "FIXME"),
    ("email", "FIXME"),
    ("legend_suffix", "FIXME"),
    ("", None),
    ("software_optimization_category", "Other"),
    ("", None),
    ("# --- hardware description (per node) ------------------------------", None),
    ("# Name of the machine.  It becomes the folder this machine's results", None),
    ("# land in, so one submission can cover several of them.  'auto' takes", None),
    ("# the name from the scheduler (SLURM_CLUSTER_NAME, LMOD_SYSTEM_NAME) or", None),
    ("# the hostname, '-' means no machine folder at all, and --machine wins", None),
    ("# over whatever stands here.", None),
    ("machine", "auto"),
    ("server_type", "CPU"),
    ("cpu", "FIXME e.g. Intel Xeon Platinum 8368"),
    # The JSON submission (--format json) keeps the CPU description in the four
    # columns data/data.json groups by, which no log can spell out.
    ("cpu_family", "FIXME e.g. Intel"),
    ("cpu_model", "FIXME e.g. Xeon"),
    ("cpu_submodel", "FIXME e.g. 8368"),
    ("cpu_generation", "FIXME e.g. Ice Lake (3rd-gen)"),
    ("cpu_memory_bandwidth_gb_s", "-"),
    ("gpu", "-"),
    ("gpu_memory_bandwidth_gb_s", "-"),
    ("gpus_per_node", 0),
    ("memory_type", "FIXME e.g. DDR5-4800"),
    ("memory_capacity_per_node", "FIXME e.g. 256GB"),
    ("last_level_cache", "FIXME e.g. 105MB"),
    ("network_interconnect", "FIXME e.g. InfiniBand HDR200"),
    ("network_topology", "FIXME e.g. Fat-tree"),
    ("storage_device", "FIXME e.g. SSD"),
    ("storage_file_system", "FIXME e.g. Lustre"),
    ("", None),
    ("# Thermal design power of one *node* (all sockets + accelerators) in W.", None),
    ("# Used to derive the per-simulation TDP and the energy to solution.", None),
    ("tdp_per_node_w", 0),
]


def _read_text(path):
    """Return the contents of `path`, or None if it does not exist."""
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return None


def last_clock_time(path):
    """Return the last 'ClockTime = N s' value in an OpenFOAM log, else None."""
    text = _read_text(path)
    if text is None:
        return None
    matches = re.findall(r"ClockTime\s*=\s*([0-9.eE+-]+)\s*s", text)
    return float(matches[-1]) if matches else None


def parse_err_times(run_dir):
    """Return the `real` times (seconds) that bash's `time` wrote to the .err.

    Allrun times its five stages in a fixed order, so the n-th `real` block
    belongs to the n-th stage.  A run that was killed before the solver
    finished simply has fewer blocks, which the caller has to tolerate.
    """
    times = []
    for name in sorted(os.listdir(run_dir)):
        if not name.endswith(".err"):
            continue
        text = _read_text(os.path.join(run_dir, name)) or ""
        for m in re.finditer(r"^real\s+(\d+)m([0-9.]+)s", text, re.M):
            times.append(int(m.group(1)) * 60 + float(m.group(2)))
    return times


def find_solver_log(run_dir):
    """Return the path of the (most recent) simpleFoam log, or None."""
    logdir = os.path.join(run_dir, "logFiles")
    if not os.path.isdir(logdir):
        return None
    logs = sorted(n for n in os.listdir(logdir)
                  if n.startswith(SOLVER_LOG_GLOB) and n.endswith(".log"))
    return os.path.join(logdir, logs[-1]) if logs else None


def parse_solver_log(path):
    """Extract the banner, host layout and iteration progress of a solver log."""
    out = {}
    hosts = []
    in_hosts = False

    with open(path, errors="replace") as f:
        for line in f:
            if in_hosts:
                m = re.match(r"\s*\((\S+)\s+(\d+)\)", line)
                if m:
                    hosts.append((m.group(1), int(m.group(2))))
                    continue
                if line.startswith(")"):
                    in_hosts = False
                continue

            if line.startswith("ExecutionTime"):
                m = re.search(r"ExecutionTime\s*=\s*([0-9.eE+-]+)\s*s\s+"
                              r"ClockTime\s*=\s*([0-9.eE+-]+)\s*s", line)
                if m:
                    out["execution_time"] = float(m.group(1))
                    out["clock_time"] = float(m.group(2))
                continue
            if line.startswith("Time = "):
                out["last_iteration"] = int(float(line.split("=", 1)[1]))
                continue
            if "Calculations will run for" in line:
                m = re.search(r"run for (\d+) steps", line)
                if m:
                    out["requested_iterations"] = int(m.group(1))
                continue

            # Everything below is a banner field.  They are written once, at
            # the very top, as 'Name   : value' -- setdefault keeps that first
            # occurrence in case a function object echoes a similar line later.
            m = re.match(r"(Build|Exec|nProcs|Date|Time|Host)\s*:\s*(.*)$", line)
            if not m:
                if line.startswith("Hosts"):
                    in_hosts = True
                continue

            key, value = m.group(1), m.group(2).strip()
            if key == "Build":
                out.setdefault("build", value)
                v = re.search(r"version=(\S+)", value)
                if v:
                    out.setdefault("version", v.group(1).lstrip("v"))
            elif key == "Exec":
                out.setdefault("exec", value)
                if value.split():
                    out.setdefault("solver", value.split()[0])
            elif key == "nProcs":
                try:
                    out.setdefault("nprocs", int(value))
                except ValueError:
                    pass
            elif key == "Date":
                out.setdefault("date_raw", value)
            elif key == "Time":
                out.setdefault("time_raw", value)
            elif key == "Host":
                out.setdefault("first_host", value)

    out["hosts"] = hosts
    if hosts:
        out["nodes"] = len(hosts)
    return out


def read_dat(path):
    """Read an OpenFOAM function-object .dat file.

    Returns (columns, rows) where `columns` are the names taken from the last
    '#'-comment line and `rows` are lists of floats.  Non-numeric rows are
    skipped so a truncated final line (killed job) does not abort the parse.
    """
    text = _read_text(path)
    if text is None:
        return [], []

    columns, rows = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            fields = line.lstrip("#").split()
            # The header line is the last comment; single-token comments such
            # as '# magUInf : ...' are metadata and must not overwrite it.
            if len(fields) > 2 and fields[0] == "Time":
                columns = fields
            continue
        try:
            rows.append([float(v) for v in line.split()])
        except ValueError:
            continue
    return columns, rows


def column(columns, rows, name):
    """Return one named column of a read_dat() result as a list of floats."""
    if name not in columns:
        return []
    i = columns.index(name)
    return [r[i] for r in rows if len(r) > i]


def mean(values):
    return sum(values) / len(values) if values else None


def stddev(values):
    if len(values) < 2:
        return None
    m = sum(values) / len(values)
    return (sum((v - m) ** 2 for v in values) / (len(values) - 1)) ** 0.5


def force_statistics(run_dir, window):
    """Mean / spread of Cd, Cl, Cs over the last `window` iterations."""
    path = os.path.join(run_dir, "postProcessing", "forceCoeffsAll", "0",
                        "coefficient.dat")
    columns, rows = read_dat(path)
    if not rows:
        return None, {}

    history = {
        "iteration": column(columns, rows, "Time"),
        "cd": column(columns, rows, "Cd"),
        "cl": column(columns, rows, "Cl"),
        "cs": column(columns, rows, "Cs"),
    }

    stats = {"samples": len(rows), "averaging_window_iterations": window}
    for key in ("cd", "cl", "cs"):
        tail = history[key][-window:]
        m, s = mean(tail), stddev(tail)
        stats[key + "_mean"] = m
        stats[key + "_std_dev"] = s
        # Standard error of the mean; the spreadsheet reports 2*s(m) as the
        # 95% confidence interval.
        sem = s / len(tail) ** 0.5 if s is not None and tail else None
        stats[key + "_error_mean"] = sem
        stats[key + "_conf_int_95"] = 2 * sem if sem is not None else None
    return stats, history


def wall_clock_series(run_dir):
    """Per-iteration and accumulated wall-clock time of the solver run."""
    path = os.path.join(run_dir, "postProcessing", "wallClockTimeStatistics",
                        "0", "wallClockTime.dat")
    columns, rows = read_dat(path)
    return {
        "iteration": column(columns, rows, "Time"),
        "accumulated": column(columns, rows, "accumulatedWCT"),
        "per_step": column(columns, rows, "timeStepWCT"),
    }


def parse_case_definition(run_dir):
    """Read nCores / decompositionMethod / nHierarchical from the run's copy."""
    text = _read_text(os.path.join(run_dir, CASE_DEFINITION)) or ""
    out = {}
    m = re.search(r"(?m)^\s*nCores\s+(\d+);", text)
    if m:
        out["ncores"] = int(m.group(1))
    m = re.search(r"(?m)^\s*decompositionMethod\s+(\S+);", text)
    if m:
        out["decomposition_method"] = m.group(1)
    m = re.search(r"(?m)^\s*nHierarchical\s+\(([^)]*)\);", text)
    if m:
        out["n_hierarchical"] = "(%s)" % " ".join(m.group(1).split())
    return out


def parse_schemes(run_dir):
    """Derive the spreadsheet's scheme / accuracy fields from fvSchemes."""
    text = _read_text(os.path.join(run_dir, "system", "fvSchemes")) or ""
    out = {"velocity_convection_scheme": "N/A",
           "steady_state_transient": "N/A",
           "order_of_accuracy": "N/A"}

    m = re.search(r"(?m)^\s*div\(phi,U\)\s+(.+?);", text)
    if m:
        out["velocity_convection_scheme"] = " ".join(m.group(1).split())

    m = re.search(r"ddtSchemes\s*\{[^}]*?default\s+(\S+);", text, re.S)
    if m:
        ddt = m.group(1)
        steady = ddt == "steadyState"
        out["steady_state_transient"] = "Steady State" if steady else "Transient"
        # Space is 2nd order (linearUpwind); time is 1st order by definition
        # for a steady-state (pseudo-time) scheme.
        out["order_of_accuracy"] = "2nd/1st" if steady else "2nd/2nd"
    return out


def parse_renumber_method(run_dir):
    text = _read_text(os.path.join(run_dir, "logFiles",
                                   "20_renumberMesh.log")) or ""
    m = re.search(r"Using renumber-method:\s*(\S+)", text)
    if not m:
        return "N/A"
    return RENUMBER_ALIASES.get(m.group(1), m.group(1))


def collect_run(run_dir, mesh, window):
    """Harvest one <workspace>/runs/<mesh>/<ncores> directory.

    Returns None when the run has not produced a solver log yet.
    """
    solver_log = find_solver_log(run_dir)
    if solver_log is None:
        return None

    banner = parse_solver_log(solver_log)
    case = parse_case_definition(run_dir)
    wct = wall_clock_series(run_dir)
    stats, history = force_statistics(run_dir, window)

    ncores = banner.get("nprocs") or case.get("ncores")
    nodes = banner.get("nodes")
    if not nodes:
        # Fall back to the sbatch directive we rendered ourselves.
        slurm = _read_text(os.path.join(run_dir, "run.slurm")) or ""
        m = re.search(r"(?m)^#SBATCH\s+--nodes=(\d+)", slurm)
        nodes = int(m.group(1)) if m else None

    # Pre-processing: bash `time` in the .err covers I/O and process start-up,
    # so prefer it; decomposePar does not print a ClockTime at all, which makes
    # the log-based fallback an underestimate (flagged in the output).
    err_times = parse_err_times(run_dir)
    prep_source = "slurm-stderr"
    if len(err_times) >= len(PREP_STAGES):
        prep = dict(zip((s for s, _ in PREP_STAGES), err_times))
    else:
        prep_source = "log-clocktime (decomposePar missing)"
        prep = {}
        for stage, logname in PREP_STAGES:
            t = last_clock_time(os.path.join(run_dir, "logFiles", logname))
            if t is not None:
                prep[stage] = t

    per_step = wct["per_step"]
    completed = banner.get("last_iteration")
    requested = banner.get("requested_iterations")

    run = {
        "name": "%s_%s" % (mesh, ncores),
        "case_directory": os.path.abspath(run_dir),
        "mesh": mesh,
        "cells": int(MESH_CELLS[mesh]),

        "solver_type": "FVM",
        "compressibility": "Incompressible",
        "pressure_density_based": "Pressure",
        "segregated_coupled": "Segregate",
    }
    run.update(parse_schemes(run_dir))

    run.update({
        "time_for_pre_processing_s": round(sum(prep.values()), 3) if prep else None,
        "wall_clock_time_to_completion_s": banner.get("clock_time"),
        "wall_clock_time_per_iteration_s": mean(per_step),
        "decomposition_method": case.get("decomposition_method", "N/A"),
        "renumbering_method": parse_renumber_method(run_dir),

        "nodes": nodes,
        "cpu_cores": ncores,

        "pre_processing_breakdown_s": {k: round(v, 3) for k, v in prep.items()},
        "pre_processing_source": prep_source,
    })

    run["provenance"] = {
        "solver": banner.get("solver", "simpleFoam"),
        "openfoam_version": banner.get("version", "N/A"),
        "openfoam_build": banner.get("build", "N/A"),
        "solver_log": os.path.relpath(solver_log, run_dir),
        "run_date": banner.get("date_raw", "N/A"),
        "iterations_requested": requested,
        "iterations_completed": completed,
        "is_partial": bool(requested and completed and completed < requested),
        "hosts": [h for h, _ in banner.get("hosts", [])],
        "ranks_per_host": banner.get("hosts", [[None, None]])[0][1]
                          if banner.get("hosts") else None,
        "decomposition_coefficients": case.get("n_hierarchical", "N/A"),
        "wall_clock_per_iteration_median_s": (
            sorted(per_step)[len(per_step) // 2] if per_step else None),
        "wall_clock_samples": len(per_step),
    }

    if stats:
        run["forces"] = stats
    run["_history"] = history
    run["_wct_history"] = wct
    return run


def apply_metadata(run, meta):
    """Fill the hardware / energy fields that only the metadata file knows."""
    nodes = run.get("nodes")
    tdp_node = meta.get("tdp_per_node_w") or 0
    try:
        tdp_node = float(tdp_node)
    except (TypeError, ValueError):
        tdp_node = 0.0

    tdp = tdp_node * nodes if (tdp_node and nodes) else None
    run["tdp_of_system_w"] = tdp

    wct = run.get("wall_clock_time_to_completion_s")
    if tdp and wct:
        run["total_energy_to_completion_kwh"] = tdp * wct / 3.6e6
        run["is_energy_measured"] = False
    else:
        run["total_energy_to_completion_kwh"] = None
        run["is_energy_measured"] = False

    gpus_per_node = meta.get("gpus_per_node", 0) or 0
    try:
        gpus_per_node = int(gpus_per_node)
    except (TypeError, ValueError):
        gpus_per_node = 0

    run["hardware"] = {
        "server_type": meta.get("server_type", "CPU"),
        "cpu": meta.get("cpu", "N/A"),
        "gpu": meta.get("gpu", "-"),
        "gpus": gpus_per_node * nodes if nodes else 0,
        "memory_type": meta.get("memory_type", "N/A"),
        "memory_capacity_per_node": meta.get("memory_capacity_per_node", "N/A"),
        "last_level_cache": meta.get("last_level_cache", "N/A"),
        "network_interconnect": meta.get("network_interconnect", "N/A"),
        "network_topology": meta.get("network_topology", "N/A"),
        "storage_device": meta.get("storage_device", "N/A"),
        "storage_file_system": meta.get("storage_file_system", "N/A"),
    }
    return derive_metrics(run)


def derive_metrics(run):
    """Add the FVOPS throughput metrics to a harvested run.

    FVOPS -- Finite VOlume Operations Per Second -- is the number of cells
    advanced per second of wall-clock time, i.e. cells / average time per
    iteration.  Definition from Galeazzo et al., IOP Conf. Ser.: Mater. Sci.
    Eng. 1312, 012009 (2024), doi:10.1088/1757-899X/1312/1/012009.

    The per-device figure divides by the accelerators when the run used any,
    and by the compute nodes otherwise -- for a CPU-only run the node is the
    device.
    """
    cells = run.get("cells")
    dt = run.get("wall_clock_time_per_iteration_s")
    cores = run.get("cpu_cores")
    gpus = (run.get("hardware") or {}).get("gpus") or 0
    devices = gpus if gpus else run.get("nodes")

    fvops = cells / dt if (cells and dt) else None
    run["fvops"] = fvops
    run["fvops_per_core"] = fvops / cores if (fvops and cores) else None
    run["fvops_per_device"] = fvops / devices if (fvops and devices) else None
    run["fvops_device_kind"] = "GPU" if gpus else "node"
    return run


def add_parallel_efficiency(runs):
    """Annotate a mesh's sweep with its strong-scaling efficiency.

    The baseline is the smallest core count that produced a usable timing, so

        E = (FVOPS/core at N) / (FVOPS/core at N_base)

    which is the usual speedup / (N / N_base) rewritten in terms of the
    per-core throughput.  E > 1 is not a bug: superlinear speedup is routinely
    observed on these meshes once a subdomain starts fitting into cache.
    """
    base = next((r for r in runs if r.get("fvops_per_core")), None)

    # The keys are always written, null when there is nothing to compare, so
    # every simulation entry has the same shape.
    for run in runs:
        fpc = run.get("fvops_per_core")
        run["parallel_efficiency"] = (fpc / base["fvops_per_core"]
                                      if (base and fpc) else None)
        run["parallel_efficiency_baseline_cores"] = (base["cpu_cores"]
                                                     if base else None)
    return runs


def format_timestep(run):
    """Latest solver timestep as 'reached/requested', e.g. '1467/4000'.

    A trailing '*' marks a run that has not reached the requested end time --
    these numbers move while the queue is still working through the sweep.
    """
    prov = run.get("provenance", {})
    done = prov.get("iterations_completed")
    total = prov.get("iterations_requested")
    if done is None:
        return "n/a"
    if not total:
        return str(done)
    return "%d/%d%s" % (done, total, "*" if prov.get("is_partial") else "")


def format_fvops_table(mesh, runs):
    """Render the FVOPS / scaling summary of one mesh as aligned text lines."""
    cells = MESH_CELLS[mesh]

    # A run killed before the wall-clock function object wrote anything has no
    # throughput to report; list it separately rather than crashing the table.
    timed = [r for r in runs if r.get("parallel_efficiency")]
    untimed = [r for r in runs if not r.get("parallel_efficiency")]
    if not timed:
        return ["  no run produced wall-clock samples; FVOPS unavailable"]

    kind = timed[0]["fvops_device_kind"]
    base = timed[0]["parallel_efficiency_baseline_cores"]

    lines = ["  %-13s %6s %6s %11s %10s %12s %13s %12s %9s"
             % ("simulation", "nodes", "cores", "timestep", "dt [s]",
                "FVOPS [M/s]", "/core [k/s]", "/%s [M/s]" % kind, "eff [%]")]

    for run in timed:
        lines.append(
            "  %-13s %6s %6s %11s %10.4f %12.2f %13.2f %12.2f %8.1f%%" % (
                run["name"],
                run["nodes"], run["cpu_cores"],
                format_timestep(run),
                run["wall_clock_time_per_iteration_s"],
                run["fvops"] / 1e6,
                run["fvops_per_core"] / 1e3,
                run["fvops_per_device"] / 1e6,
                100.0 * run["parallel_efficiency"],
            ))

    incomplete = any(r["provenance"].get("is_partial") for r in timed)
    lines.append("  (%.0fM cells; efficiency relative to %d cores%s)"
                 % (cells / 1e6, base,
                    "; * = run still incomplete" if incomplete else ""))
    for run in untimed:
        lines.append("  %-13s no wall-clock samples, excluded from the table"
                     % run["name"])
    return lines


# --------------------------------------------------------------------------- #
# Minimal YAML I/O (stdlib only)
# --------------------------------------------------------------------------- #

# Characters that are safe unquoted in a YAML plain scalar.  The negative
# lookahead rejects a lone '-' (and '- x'), which YAML would read as the start
# of a block sequence rather than as text -- the 'gpu: -' case.
_YAML_PLAIN = re.compile(r"^(?!-(?:\s|$))[A-Za-z0-9_./+\-][A-Za-z0-9_ ./+()\-]*$")


def yaml_scalar(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # repr() round-trips exactly; strip a pointless trailing '.0'.
        return repr(value)
    text = str(value)
    # Quote anything a YAML reader would resolve to a non-string: participant
    # IDs such as "01" and versions such as "2606" have to survive as strings.
    looks_numeric = re.match(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$", text)
    if (text and _YAML_PLAIN.match(text) and not looks_numeric
            and text.lower() not in ("true", "false", "null", "yes", "no",
                                     "on", "off", "~")):
        return text
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')


def yaml_dump(obj, indent=0):
    """Serialise dicts / lists / scalars.  Insertion order is preserved."""
    pad = "  " * indent
    lines = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, dict) and value:
                lines.append("%s%s:" % (pad, key))
                lines.extend(yaml_dump(value, indent + 1))
            elif isinstance(value, list) and value:
                lines.append("%s%s:" % (pad, key))
                lines.extend(yaml_dump(value, indent + 1))
            elif isinstance(value, (dict, list)):
                lines.append("%s%s: %s" % (pad, key,
                                           "{}" if isinstance(value, dict) else "[]"))
            else:
                lines.append("%s%s: %s" % (pad, key, yaml_scalar(value)))
        return lines

    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict) and item:
                block = yaml_dump(item, indent + 1)
                lines.append("%s- %s" % (pad, block[0].lstrip()))
                lines.extend(block[1:])
            elif isinstance(item, list):
                # Force-flow style: force histories stay one row per line.
                lines.append("%s- [%s]"
                             % (pad, ", ".join(yaml_scalar(v) for v in item)))
            else:
                lines.append("%s- %s" % (pad, yaml_scalar(item)))
        return lines

    return ["%s%s" % (pad, yaml_scalar(obj))]


def yaml_load_flat(path):
    """Read a flat 'key: value' mapping, ignoring comments and blank lines.

    Deliberately tiny: the metadata file we generate is flat, and depending on
    PyYAML would break this script's standard-library-only contract.
    """
    meta = {}
    text = _read_text(path)
    if text is None:
        return meta
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            print("  ! %s:%d: ignoring unparsable line %r"
                  % (path, lineno, stripped))
            continue
        key, value = stripped.split(":", 1)
        value = value.strip()
        if value[:1] in ('"', "'") and value[-1:] == value[:1] and len(value) > 1:
            value = value[1:-1]
        meta[key.strip()] = value
    return meta


def display_path(path, base):
    """Shorten `path` against `base`, but never into a '../../..' chain."""
    rel = os.path.relpath(path, base)
    return path if rel.startswith(os.pardir) else rel


def sanitize(text):
    """Reduce `text` to something safe for a file or directory name."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)).strip("_")
    return cleaned or "run"


def write_metadata_template(path):
    lines = []
    for key, value in METADATA_TEMPLATE:
        if value is None:
            lines.append(key)
        else:
            lines.append("%s: %s" % (key, yaml_scalar(value)))
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# Post-processing driver
# --------------------------------------------------------------------------- #

# Environment variables that name the machine rather than the node: SLURM sets
# the first inside a job, the Lmod-based module systems of most HPC centres set
# the second on the login nodes too, and OHC_MACHINE is the manual override for
# a site that sets neither.
MACHINE_VARS = ("OHC_MACHINE", "SLURM_CLUSTER_NAME", "LMOD_SYSTEM_NAME",
                "CLUSTER_NAME")

# Hostnames that name a role rather than a machine.  --postProcess is run on a
# login node more often than anywhere else, and those are called exactly this;
# when one of them is the hostname, the second label of the fully qualified
# name usually is the machine (login01.hawk.hlrs.de -> hawk).
GENERIC_HOSTS = ("login", "node", "head", "batch", "compute", "front",
                 "frontend", "master", "gateway", "interactive")

# Spellings of "do not put the runs in a machine folder at all".
NO_MACHINE = ("", "-", "none", "no", "off")

# ... and of "work the name out yourself", which is what the metadata template
# ships with, so a contributor who never looks at the field still gets one
# folder per machine.
AUTO_MACHINE = ("auto", "detect", "default")


def detect_machine():
    """Name of the machine the harvested runs were made on.

    The submission is grouped per machine, so this has to name the system, not
    the node the harvest happens to run on: the scheduler's own cluster name
    when there is one, otherwise the short hostname with its node number
    stripped (``uc2n995`` -> ``uc2n``, ``hkn0801`` -> ``hkn``), which is as
    close as a hostname gets.  The number is only dropped when enough of a
    name is left without it -- ``k044`` is kept whole, since ``k`` would name
    nothing.  Override it with ``--machine`` whenever the guess is not the name
    the submission should carry.
    """
    for var in MACHINE_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            return sanitize(value).lower()
    labels = socket.gethostname().split(".")
    host = labels[0]
    stem = re.sub(r"[-_]?\d+$", "", host)
    if len(re.sub(r"[^A-Za-z]", "", stem)) >= 3:
        host = stem
    if host.lower() in GENERIC_HOSTS and len(labels) > 2:
        host = labels[1]
    return sanitize(host).lower()


def resolve_machine(requested, declared=None):
    """Turn --machine, the metadata's own `machine`, or neither into a folder.

    ``--machine`` wins over the metadata file, which wins over the guess from
    the scheduler; an empty result means the submission has no machine level --
    the files land directly in the dated folder, which is all a one-machine
    submission needs.
    """
    for value in (requested, declared):
        if value is None:
            continue
        name = str(value).strip()
        if name.lower() in AUTO_MACHINE:
            break
        if name.lower() in NO_MACHINE:
            return ""
        return sanitize(name).lower()
    return detect_machine()


def write_meta_yaml(path, source, updates):
    """Write the machine's meta.yaml, keeping the comments of `source`.

    The file next to the results is a copy of the metadata the harvest was
    driven with rather than a re-rendering of it, so whatever the contributor
    wrote in there -- comments, ordering, fields this script does not know --
    survives into the submission.  Only the keys in `updates` are rewritten,
    and any of them the source does not mention is appended.
    """
    text = _read_text(source) or ""
    pending = dict(updates)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and ":" in stripped:
            key = stripped.split(":", 1)[0].strip()
            if key in pending:
                lines.append("%s: %s" % (key, yaml_scalar(pending.pop(key))))
                continue
        lines.append(line)
    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        for key, value in sorted(pending.items()):
            lines.append("%s: %s" % (key, yaml_scalar(value)))
    with open(path, "w") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")
    return path


# The metadata file is looked for in the workspace under the name the
# submission itself uses, so the file a contributor fills in and the file that
# ends up next to the results are the same thing.  The old name is still read
# where a workspace already has one, so an existing filled-in file does not go
# unnoticed.
META_WORKSPACE_NAMES = ("meta.yaml", "meta.yml", "submissionMeta.yaml")


def find_workspace_meta(workspace):
    """The metadata file of `workspace`, or None when it has none yet."""
    for name in META_WORKSPACE_NAMES:
        candidate = os.path.join(workspace, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def template_todo():
    """The fields of the metadata template that only the contributor can fill.

    The FIXME strings, plus the node TDP: it ships as ``0`` because it is a
    number and cannot say FIXME, and left at zero it silently empties the
    energy columns of every run in the submission.
    """
    todo = [key for key, value in METADATA_TEMPLATE
            if value is not None and str(value).startswith("FIXME")]
    if "tdp_per_node_w" not in todo:
        todo.append("tdp_per_node_w")
    return todo


def repo_submissions_dir():
    """Return the OHC-DATA checkout's ``submissions/`` directory, or None.

    When this script is run from a clone of the repository -- the usual case,
    ``./scripts/benchmark.py`` -- the harvested submissions belong straight in
    that clone's ``submissions/`` folder, which is what the pull request is
    made of.  A copy of the script that lives outside such a checkout has no
    such folder, and post-processing falls back to the workspace.
    """
    candidate = os.path.join(os.path.dirname(CASE_DIR), "submissions")
    if os.path.isdir(candidate) and os.path.isfile(
            os.path.join(candidate, "README.md")):
        return candidate
    return None


def postprocess(args, workspace):
    runs_dir = os.path.join(workspace, "runs")
    if not os.path.isdir(runs_dir):
        sys.exit("error: no runs directory under %s -- nothing to post-process"
                 % workspace)

    # Nothing in the logs says who ran this, on what hardware, at what node
    # TDP, so without the metadata there is no submission to write -- only a
    # set of numbers nobody could attribute.  The first run therefore leaves a
    # commented template behind and stops, rather than writing results that
    # would have to be thrown away and harvested again.
    meta_path = args.metadata or find_workspace_meta(workspace)
    if meta_path is None or not os.path.exists(meta_path):
        target = meta_path or os.path.join(workspace, META_WORKSPACE_NAMES[0])
        write_metadata_template(target)
        todo = template_todo()
        print("wrote a metadata template: %s" % target)
        sys.exit(
            "error: this workspace has no metadata file, and the OpenFOAM "
            "logs cannot supply one.\n"
            "       Fill in the %d field(s) the template above leaves open "
            "-- %s -- and re-run the same command.\n"
            "       It is copied next to the results as that machine's "
            "meta.yaml, which is what describes the hardware behind the "
            "numbers in the submission." % (len(todo), ", ".join(todo)))

    meta = yaml_load_flat(meta_path)
    missing = sorted(k for k, v in meta.items() if str(v).startswith("FIXME"))
    if missing:
        print("  ! %s still has placeholder values: %s"
              % (meta_path, ", ".join(missing)))

    # Results are grouped per harvest and, inside it, per machine:
    #
    #     <parent>/<YYYYMMDD>_<handle>/<machine>/
    #
    # Re-running on another day therefore keeps the previous submission intact
    # instead of overwriting it, and harvesting a second machine into the same
    # dated folder adds a folder beside the first rather than mixing the two.
    suffix = sanitize(args.suffix or meta.get("legend_suffix") or "run")
    stamp = datetime.date.today().strftime("%Y%m%d")
    parent = (args.postprocess_out or repo_submissions_dir()
              or os.path.join(workspace, "submissions"))
    machine = resolve_machine(args.machine, meta.get("machine"))
    outdir = os.path.join(parent, "%s_%s" % (stamp, suffix))
    if machine:
        outdir = os.path.join(outdir, machine)

    # The suffix also drives the OHC plot legend, so keep the two in sync when
    # it was overridden on the command line.
    meta["legend_suffix"] = suffix

    written = []
    meta_yaml = None
    for mesh in args.mesh:
        mesh_dir = os.path.join(runs_dir, mesh)
        if not os.path.isdir(mesh_dir):
            continue

        # Numeric sort so the simulation list is a proper scaling sweep.
        ncores_dirs = sorted(
            (d for d in os.listdir(mesh_dir)
             if d.isdigit() and os.path.isdir(os.path.join(mesh_dir, d))),
            key=int)

        runs = []
        for entry in ncores_dirs:
            run_dir = os.path.join(mesh_dir, entry)
            run = collect_run(run_dir, mesh, args.avg_window)
            if run is None:
                print("  - %s/%s: no solver log, skipped" % (mesh, entry))
                continue
            runs.append(apply_metadata(run, meta))

        if not runs:
            print("[%s] no runs with solver output" % mesh)
            continue

        # Efficiency is a property of the sweep, so it needs every run of the
        # mesh and cannot be filled in by collect_run().
        add_parallel_efficiency(runs)

        partial = sum(1 for r in runs if r["provenance"]["is_partial"])
        print("[%s] %d simulation(s), %d partial"
              % (mesh, len(runs), partial))
        for line in format_fvops_table(mesh, runs):
            print(line)

        # write_submission consumes the history keys, so report first, and
        # let the JSON writer -- which needs none of them -- go first too.
        os.makedirs(outdir, exist_ok=True)
        if meta_yaml is None:
            # The hardware behind the numbers belongs next to them: one
            # meta.yaml per machine folder, whichever meshes end up in it.
            meta_yaml = write_meta_yaml(
                os.path.join(outdir, "meta.yaml"), meta_path,
                {"legend_suffix": suffix, "machine": machine or "-"})
            print("  -> %s" % display_path(meta_yaml, workspace))
        if args.submission_format in ("json", "both"):
            path = write_json_submission(outdir, mesh, meta, runs, machine)
            written.append(path)
            print("  -> %s" % display_path(path, workspace))
        if args.submission_format in ("yaml", "both"):
            path = write_submission(outdir, mesh, meta, runs, args, machine)
            written.append(path)
            print("  -> %s" % display_path(path, workspace))
        print("")

    if not written:
        print("no submissions written.")
        return

    print("\nwrote %d submission file(s) under %s" % (len(written), outdir))
    if machine:
        print("They describe machine '%s'.  Harvest another machine's "
              "workspace with the same --suffix to add it to this submission "
              "as a folder of its own." % machine)
    if parent == repo_submissions_dir():
        print("They are already inside this checkout's submissions/ folder, "
              "so review them and commit them for the pull request.  "
              "scripts/validate_submission.py checks a JSON submission before "
              "you open it.")
    else:
        print("Review them, then copy into the OHC-DATA repository's "
              "submissions/ folder for the pull request.  "
              "scripts/validate_submission.py there checks a JSON submission "
              "before you open it.")


def _as_number(value):
    """Return `value` as a float, or None for the '-'/'FIXME' placeholders."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value, default=None):
    """Return `value` as an int, or `default` when it is not a number.

    The metadata file is read as flat text, so every value out of it is a
    string -- and a column the dataset declares as a whole number has to reach
    the JSON as one, not as "2".
    """
    number = _as_number(value)
    return int(number) if number is not None else default


def submission_name(meta, mesh, machine, extension):
    """File name of one mesh's submission: machine and mesh.

    The machine is part of it because a submission may cover several, and
    `File Name` is the only provenance a row in data/data.json carries: two
    machines whose files were both called `OHC1_DrivAer_65M.json` would be
    indistinguishable there.  The participant is not: it is already in the
    record's own `Contributor ID`, and putting it in the name only meant that
    a metadata file which had not been filled in yet produced files called
    `FIXME_...`.  Nor are the date and the handle, which name the enclosing
    `<YYYYMMDD>_<handle>` directory.
    """
    _, label = MESHES[mesh]
    parts = ["OHC1", "DrivAer"]
    if machine:
        parts.append(machine)
    parts.append(label)
    return "_".join(parts) + extension


def write_json_submission(outdir, mesh, meta, runs, machine=""):
    """Emit one mesh as the flat JSON the OHC-DATA repository ingests.

    This is the same sweep as write_submission(), reduced to the columns of
    data/data.json: one object per simulation, with everything the runs share
    hoisted into `meta`.  The OHC-DATA workflow validates the file on the pull
    request and appends the records once it is merged; see that repository's
    submissions/README.md for the schema.
    """
    name = submission_name(meta, mesh, machine, ".json")

    hardware = runs[0].get("hardware", {})
    document = {
        "schema": "ohc-submission/v1",
        "generated_by": "benchmark.py --postProcess --format json",
        "generated_on": datetime.date.today().isoformat(),
        "meta": {
            "Contributor ID": str(meta.get("participant_id", "N/A")),
            "Contributor Affiliation": meta.get("affiliation", "N/A"),
            "File Name": name,
            "Track": meta.get("track", "Hardware Track"),
            "OHC": _as_int(meta.get("ohc_round"), 2),
            "Submission Date": datetime.date.today().isoformat(),
            "Software Optimization Category": meta.get(
                "software_optimization_category", "Other"),
            "Mesh": mesh,
            "Number of Cells": int(MESH_CELLS[mesh]),
            "OpenFOAM Flavor": "ESI (.com)",
            "CPU Family": meta.get("cpu_family", "N/A"),
            "CPU Model": meta.get("cpu_model", "N/A"),
            "CPU Submodel": meta.get("cpu_submodel", "N/A"),
            "CPU Generation": meta.get("cpu_generation", "N/A"),
            "CPU Memory Bandwidth [GB/s]": _as_number(
                meta.get("cpu_memory_bandwidth_gb_s")),
            "GPU Model": hardware.get("gpu", "N/A"),
            "GPU Memory Bandwidth [GB/s]": _as_number(
                meta.get("gpu_memory_bandwidth_gb_s")),
            "Last-Level Cache": hardware.get("last_level_cache", "N/A"),
            "Network Interconnect": hardware.get("network_interconnect", "N/A"),
            "Storage File-System": hardware.get("storage_file_system", "N/A"),
        },
        "simulations": [],
    }

    for run in runs:
        provenance = run.get("provenance", {})
        document["simulations"].append({
            "Number of Nodes": run.get("nodes"),
            "Number of CPU Cores": run.get("cpu_cores"),
            "Number of GPU Devices": (run.get("hardware") or {}).get("gpus", 0),
            "Decomposition Method": run.get("decomposition_method", "N/A"),
            "Renumbering Method": run.get("renumbering_method", "N/A"),
            "Run Wall-Clock Time [s]": run.get(
                "wall_clock_time_to_completion_s"),
            "Time per Iteration [s]": run.get(
                "wall_clock_time_per_iteration_s"),
            "Pre-Processing Wall-Clock Time [s]": run.get(
                "time_for_pre_processing_s"),
            "System TDP [W]": run.get("tdp_of_system_w"),
            # Only a metered figure belongs here.  The harvester's own energy
            # is modelled from the node TDP, and the dataset derives its energy
            # columns from the TDP anyway, so writing it out would just claim a
            # measurement that was never taken.  A site that did meter the run
            # sets the flag to 1 and fills in what it measured.
            "Run Consumed Energy [kWh]": (
                run.get("total_energy_to_completion_kwh")
                if run.get("is_energy_measured") else None),
            "Is Energy Reported": 1 if run.get("is_energy_measured") else 0,
            "Is Partial": 1 if provenance.get("is_partial") else 0,
        })

    path = os.path.join(outdir, name)
    with open(path, "w") as f:
        json.dump(document, f, indent=2)
        f.write("\n")
    return path


def write_submission(outdir, mesh, meta, runs, args, machine=""):
    """Emit one YAML submission file covering every run of a single mesh."""
    _, label = MESHES[mesh]

    document = {
        "schema": "ohc1-submission/v1",
        "generated_by": "benchmark.py --postProcess",
        "generated_on": datetime.date.today().isoformat(),
        "meta": {
            "test_case": meta.get("test_case", "Test case 2a"),
            "track": meta.get("track", "Hardware Track"),
            "participant_id": meta.get("participant_id", "N/A"),
            "lastname": meta.get("lastname", "N/A"),
            "firstname": meta.get("firstname", "N/A"),
            "affiliation": meta.get("affiliation", "N/A"),
            "email": meta.get("email", "N/A"),
            "legend_suffix": meta.get("legend_suffix", "N/A"),
            "cfd_software": "OpenFOAM",
            "flavor": "ESI (.com)",
            "cfd_software_version": runs[0]["provenance"]["openfoam_version"],
            "mesh": mesh.capitalize(),
            "mesh_cells": label,
        },
        "simulations": [],
    }

    for run in runs:
        history = run.pop("_history")
        wct_history = run.pop("_wct_history")
        entry = dict(run)

        if not args.no_forces and history.get("iteration"):
            entry["forces"] = dict(entry.get("forces") or {})
            entry["forces"]["history_columns"] = [
                "iteration", "wall_clock_time_s", "cd", "cl", "cs"]
            entry["forces"]["history"] = force_history_rows(
                history, wct_history, args.forces_stride)

        document["simulations"].append(entry)

    name = submission_name(meta, mesh, machine, ".yaml")
    path = os.path.join(outdir, name)
    with open(path, "w") as f:
        f.write("\n".join(yaml_dump(document)) + "\n")
    return path


def force_history_rows(history, wct_history, stride):
    """Zip the force coefficients with the accumulated wall-clock time.

    The two function objects start at different iterations (forceCoeffsAll
    writes iteration 1, wallClockTimeStatistics only from 2 on), so the times
    are looked up by iteration rather than by row index.
    """
    wct_by_iter = dict(zip(wct_history["iteration"], wct_history["accumulated"]))

    rows = []
    iterations = history["iteration"]
    for i in range(0, len(iterations), max(1, stride)):
        it = iterations[i]
        rows.append([
            int(it),
            wct_by_iter.get(it, 0.0),
            history["cd"][i] if i < len(history["cd"]) else None,
            history["cl"][i] if i < len(history["cl"]) else None,
            history["cs"][i] if i < len(history["cs"]) else None,
        ])
    return rows


# --------------------------------------------------------------------------- #
# CLI
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


EXAMPLES = """\
examples:
  # Prepare all meshes at 128 and 256 cores (does not submit)
  ./benchmark.py --ncores 128,256

  # Only the coarse mesh, a scaling sweep, submitted to the queue
  ./benchmark.py --mesh coarse --ncores 32,64,128,256,512 \\
                 --slurm-template slurm.template --submit

  # Specify cores by node count: 1,2,4 nodes x 128 cores = 128,256,512 cores
  ./benchmark.py --nnodes 1,2,4 --ncores-per-node 128

  # Reproduce the case's stock 512 -> (16 8 4) decomposition
  ./benchmark.py --ncores 512 --start 2,1,1

  # Custom workspace location
  ./benchmark.py --ncores 512 --workspace /scratch/$USER/occDrivAer

  # Harvest the finished runs of that workspace into YAML submissions
  ./benchmark.py --postProcess --workspace /scratch/$USER/occDrivAer

  # Only the fine mesh, subsampling the force history every 10th iteration,
  # into submissions/<YYYYMMDD>_tuned/<machine>/
  ./benchmark.py --postProcess --mesh fine --forces-stride 10 --suffix tuned \\
                 --workspace /scratch/$USER/occDrivAer

  # A second machine into the same submission: same handle, own folder
  ./benchmark.py --postProcess --suffix tuned --machine horeka \\
                 --workspace /scratch/$USER/occDrivAer-horeka
"""


def build_parser():
    p = argparse.ArgumentParser(
        description="Set up strong-scaling benchmark runs for occDrivAer.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mesh", type=parse_meshes, default=list(MESHES),
                   metavar="LIST",
                   help="mesh resolution(s): any of fine,medium,coarse or "
                        "'all' (default: all)")
    p.add_argument("--ncores", type=parse_posint_list, metavar="LIST",
                   help="core count(s), single value or comma list, "
                        "e.g. 128 or 32,64,128,256. Mutually exclusive with "
                        "--nnodes/--ncores-per-node")
    p.add_argument("--nnodes", type=parse_posint_list, metavar="LIST",
                   help="node count(s), single value or comma list; requires "
                        "--ncores-per-node. nCores is computed as "
                        "nnodes*ncores-per-node")
    p.add_argument("--ncores-per-node", type=int, dest="ncores_per_node",
                   metavar="N",
                   help="cores per node; combined with --nnodes to derive "
                        "nCores (and used as tasks per node)")
    p.add_argument("--workspace", default=os.path.join(os.getcwd(), "workspace"),
                   metavar="PATH",
                   help="workspace directory for meshes and run copies "
                        "(default: ./workspace)")
    p.add_argument("--slurm-template", metavar="FILE",
                   help="SLURM template file providing sbatch directives; "
                        "$ncores $nhierarchical $mesh $jobname $tpn $nodes "
                        "$casedir are substituted (default: built-in template)")
    decomp_choices = ["doubling", "optimal"]
    if decompositionPlugin:
        decomp_choices.append(decompositionPlugin.NAME)
    p.add_argument("--decomposition", choices=tuple(decomp_choices),
                   default="doubling", dest="decomposition",
                   help="how to pick nHierarchical: 'doubling' follows the "
                        "--start triplet cyclically (the case's own "
                        "convention); 'optimal' minimises the interface area "
                        "against the blockMeshDict bounding box, which matters "
                        "on this elongated domain%s (default: doubling)"
                        % ("; '%s' delegates to decompositionPlugin.py"
                           % decompositionPlugin.NAME if decompositionPlugin
                           else ""))
    p.add_argument("--domain-aspect", type=parse_start, dest="domain_aspect",
                   metavar="X,Y,Z",
                   help="override the domain side lengths used by "
                        "--decomposition optimal (default: read from "
                        "system/blockMeshDict)")
    p.add_argument("--start", type=parse_start, default=[1, 1, 1],
                   metavar="X,Y,Z",
                   help="start triplet for the decompositionIter doubling "
                        "(default: 1,1,1)")
    p.add_argument("--jobname", default="DrivAer_RANS",
                   help="base SLURM job name (default: DrivAer_RANS)")
    p.add_argument("--ntasks-per-node", type=int, default=128, dest="tpn",
                   metavar="N",
                   help="tasks per node used to derive --nodes (default: 128)")
    p.add_argument("--end-time", type=int, dest="end_time", metavar="N",
                   help="override endTime (iteration count) in the run copies. "
                        "Useful for short placement/tuning runs; the challenge "
                        "result itself requires the stock 4000")
    p.add_argument("--submit", action="store_true",
                   help="submit each generated script with sbatch "
                        "(default: only prepare files)")

    g = p.add_argument_group("rank placement sweep")
    g.add_argument("--ranks-per-numa", "--ranks-per-domain",
                   type=parse_posint_list,
                   dest="ranks_per_numa", metavar="LIST",
                   help="sweep the number of MPI ranks pinned to each domain "
                        "of --bind-object, e.g. 8,12,16,20,24. One run per "
                        "value; nCores is ranks-per-domain * "
                        "--domains-per-node * --nnodes. Mutually exclusive "
                        "with --ncores")
    g.add_argument("--bind-object", choices=tuple(BIND_OBJECTS),
                   default="numa", dest="bind_object",
                   help="hwloc object the ranks are spread over (default: "
                        "numa). 'l3cache' is finer on chiplet CPUs, where "
                        "several L3 slices share a NUMA domain and mapping by "
                        "NUMA leaves whole slices idle. Sets the topology "
                        "defaults below unless they are given explicitly")
    g.add_argument("--total-ranks", type=int, dest="total_ranks", metavar="N",
                   help="hold the total rank count fixed and let the node "
                        "count follow from --ranks-per-numa. This is the "
                        "multi-node form of the sweep: decomposition and "
                        "cells/rank stay identical, only the spread over "
                        "nodes changes. Must divide evenly for every value")
    g.add_argument("--min-nodes", type=int, default=1, dest="min_nodes",
                   metavar="N", help="reject a --total-ranks sweep point that "
                                     "needs fewer nodes than this (default: 1)")
    g.add_argument("--max-nodes", type=int, dest="max_nodes", metavar="N",
                   help="reject a --total-ranks sweep point that needs more "
                        "nodes than this")
    g.add_argument("--numa-per-node", "--domains-per-node", type=int,
                   default=None, dest="domains_per_node", metavar="N",
                   help="domains of --bind-object per node (default: 8 for "
                        "numa, 24 for l3cache, 2 for package)")
    g.add_argument("--cores-per-numa", "--cores-per-domain", type=int,
                   default=None, dest="cores_per_domain", metavar="N",
                   help="physical cores per domain (default: 24 for numa, 8 "
                        "for l3cache, 96 for package). Above this the sweep "
                        "binds to hardware threads instead of cores and asks "
                        "SLURM for two tasks per core")

    if decompositionPlugin:
        decompositionPlugin.add_arguments(
            p.add_argument_group("decomposition plugin"))

    g = p.add_argument_group("post-processing")
    g.add_argument("--postProcess", action="store_true", dest="postprocess",
                   help="do not prepare anything; instead read the logs of the "
                        "runs already present in --workspace and write one "
                        "submission per mesh (see --format)")
    g.add_argument("--metadata", metavar="FILE",
                   help="metadata file with the fields that cannot be derived "
                        "from the logs (participant, hardware, TDP). Without "
                        "it, a commented template is written and the harvest "
                        "stops so it can be filled in "
                        "(default: <workspace>/meta.yaml, or the legacy "
                        "submissionMeta.yaml where one exists)")
    g.add_argument("--postprocess-out", metavar="DIR", dest="postprocess_out",
                   help="parent directory for the generated submissions "
                        "(default: the submissions/ folder of this OHC-DATA "
                        "checkout, or <workspace>/submissions when the script "
                        "runs outside one). The files themselves land in a "
                        "<YYYYMMDD>_<suffix> subdirectory of it")
    # --sufix is accepted as an alias so the spelling used on the command line
    # does not matter.
    g.add_argument("--suffix", "--sufix", dest="suffix", metavar="NAME",
                   help="your handle: it names the dated submission directory "
                        "(<YYYYMMDD>_<NAME>) and the plot legend "
                        "(default: legend_suffix from the metadata file)")
    g.add_argument("--machine", metavar="NAME",
                   help="machine folder written inside the dated directory, "
                        "so several machines can share one submission. Each "
                        "gets its own meta.yaml. Default: 'machine' from the "
                        "metadata file, or -- where that says 'auto' -- the "
                        "scheduler's cluster name (SLURM_CLUSTER_NAME, "
                        "LMOD_SYSTEM_NAME, OHC_MACHINE) falling back to the "
                        "short hostname. Pass '-' to put the files straight "
                        "in the dated directory")
    g.add_argument("--avg-window", type=int, default=200, dest="avg_window",
                   metavar="N",
                   help="number of trailing iterations averaged for the "
                        "reported force coefficients (default: 200)")
    g.add_argument("--forces-stride", type=int, default=1,
                   dest="forces_stride", metavar="N",
                   help="write only every N-th sample of the force history "
                        "(default: 1, i.e. all samples)")
    g.add_argument("--no-forces", action="store_true", dest="no_forces",
                   help="omit the force coefficient history from the YAML "
                        "(the averaged values are still written)")
    g.add_argument("--format", choices=("yaml", "json", "both"), default="yaml",
                   dest="submission_format",
                   help="submission flavour to write: the full YAML document "
                        "(default), the flat JSON the OHC-DATA repository "
                        "appends to data/data.json, or both")
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    # --postProcess works purely on what is already in the workspace, so none
    # of the run-preparation arguments apply.
    if args.postprocess:
        if args.ncores or args.nnodes or args.submit:
            parser.error("--postProcess cannot be combined with "
                         "--ncores/--nnodes/--submit")
        if args.avg_window < 1:
            parser.error("--avg-window must be >= 1")
        if args.forces_stride < 1:
            parser.error("--forces-stride must be >= 1")
        workspace = os.path.abspath(args.workspace)
        print("workspace: %s" % workspace)
        print("meshes:    %s\n" % ", ".join(args.mesh))
        return postprocess(args, workspace)

    # Resolve the runs: --ncores directly, the product of --nnodes and
    # --ncores-per-node, or a NUMA placement sweep.  The paths are exclusive.
    # Each entry is a dict so a run carries its own node count, directory name
    # and placement -- a fixed-total-ranks sweep has identical nCores
    # throughout and could not be keyed on that.
    tpn = args.tpn
    runs = []

    if args.ranks_per_numa is not None:
        if args.ncores is not None or args.ncores_per_node is not None:
            parser.error("--ranks-per-domain cannot be combined with "
                         "--ncores/--ncores-per-node")
        # Topology follows the chosen object unless overridden explicitly.
        topo = BIND_OBJECTS[args.bind_object]
        if args.domains_per_node is None:
            args.domains_per_node = topo["domains_per_node"]
        if args.cores_per_domain is None:
            args.cores_per_domain = topo["cores_per_domain"]
        if args.domains_per_node < 1:
            parser.error("--domains-per-node must be >= 1")
        if args.cores_per_domain < 1:
            parser.error("--cores-per-domain must be >= 1")
        if args.total_ranks is not None and args.nnodes is not None:
            parser.error("--total-ranks derives the node count; drop --nnodes")

        # Without --total-ranks the sweep is the cross product of the two
        # lists, so a single density can be swept over many node counts (a
        # strong-scaling run at fixed placement) as readily as the reverse.
        combos = []
        for rpn in args.ranks_per_numa:
            per_node = rpn * args.domains_per_node
            if args.total_ranks is not None:
                if args.total_ranks % per_node:
                    parser.error("--total-ranks %d is not divisible by %d "
                                 "ranks/node (%d ranks/domain x %d domains)"
                                 % (args.total_ranks, per_node, rpn,
                                    args.domains_per_node))
                nodes = args.total_ranks // per_node
                if nodes < args.min_nodes:
                    parser.error("%d ranks/domain needs only %d node(s), below "
                                 "--min-nodes %d" % (rpn, nodes,
                                                     args.min_nodes))
                if args.max_nodes and nodes > args.max_nodes:
                    parser.error("%d ranks/domain needs %d nodes, above "
                                 "--max-nodes %d" % (rpn, nodes,
                                                     args.max_nodes))
                # nCores is identical across such a sweep, so the node count
                # is what names the run directory.
                combos.append((rpn, per_node, nodes, args.total_ranks,
                               str(nodes)))
            else:
                for nodes in (args.nnodes or [1]):
                    if nodes < args.min_nodes:
                        parser.error("--nnodes %d is below --min-nodes %d"
                                     % (nodes, args.min_nodes))
                    if args.max_nodes and nodes > args.max_nodes:
                        parser.error("--nnodes %d is above --max-nodes %d"
                                     % (nodes, args.max_nodes))
                    combos.append((rpn, per_node, nodes, per_node * nodes,
                                   str(per_node * nodes)))

        for rpn, per_node, nodes, ncores, dirname in combos:
            if any(r["dirname"] == dirname for r in runs):
                parser.error("--ranks-per-domain values collide on run "
                             "directory %r" % dirname)

            # --exclusive is mandatory, not a nicety: without it SLURM hands
            # out a cpuset covering only the requested cores, packed onto the
            # lowest NUMA domains, and 'ppr:N:numa' then sees fewer domains
            # than the node has and refuses to map.  It also keeps another job
            # off the node, which a timing measurement needs anyway.
            directives = ["#SBATCH --exclusive"]
            # SLURM hands out a whole core per task by default, so one rank per
            # physical core already consumes the node.  Placing more than that
            # needs --ntasks-per-core, which permits two tasks to share a core;
            # --threads-per-core does NOT do this and the allocation is refused.
            if rpn > args.cores_per_domain:
                directives.append("#SBATCH --ntasks-per-core=2")

            runs.append({
                "ncores": ncores,
                "nodes": nodes,
                "tpn": per_node,
                "dirname": dirname,
                "label": "%dn_%drpn" % (nodes, rpn),
                "ompiflags": numa_placement(rpn, args.cores_per_domain,
                                            args.bind_object),
                "extra_sbatch": "\n".join(directives),
            })

        ncores_list = [r["ncores"] for r in runs]

    elif args.nnodes is not None or args.ncores_per_node is not None:
        if args.ncores is not None:
            parser.error("--ncores cannot be combined with "
                         "--nnodes/--ncores-per-node")
        if args.nnodes is None or args.ncores_per_node is None:
            parser.error("--nnodes requires --ncores-per-node (and vice versa)")
        if args.ncores_per_node < 1:
            parser.error("--ncores-per-node must be >= 1")
        ncores_list = [n * args.ncores_per_node for n in args.nnodes]
        tpn = args.ncores_per_node  # so derived $nodes matches --nnodes exactly
    elif args.ncores is not None:
        ncores_list = args.ncores
    else:
        parser.error("one of --ncores or --nnodes/--ncores-per-node is required")

    if not runs:
        # The plain paths: one run per core count, no explicit placement.
        runs = [{"ncores": n, "nodes": (n + tpn - 1) // tpn, "tpn": tpn,
                 "dirname": str(n), "label": None,
                 "ompiflags": "", "extra_sbatch": ""}
                for n in ncores_list]

    if args.submit and not shutil.which("sbatch"):
        sys.exit("error: --submit given but 'sbatch' not found in PATH")

    if args.slurm_template:
        with open(args.slurm_template) as f:
            template_text = f.read()
    else:
        template_text = DEFAULT_SLURM_TEMPLATE

    workspace = os.path.abspath(args.workspace)
    meshes_dir = os.path.join(workspace, "meshes")
    runs_dir = os.path.join(workspace, "runs")
    os.makedirs(meshes_dir, exist_ok=True)
    os.makedirs(runs_dir, exist_ok=True)

    print("workspace: %s" % workspace)
    print("meshes:    %s" % ", ".join(args.mesh))
    print("nCores:    %s" % ", ".join(str(n) for n in ncores_list))
    if args.end_time:
        print("endTime:   %d iterations (stock is 4000)" % args.end_time)
    if args.ranks_per_numa:
        print("sweep:     %s ranks per %s over %d domain(s)/node"
              % (",".join(str(r) for r in args.ranks_per_numa),
                 args.bind_object, args.domains_per_node))
        print("nodes:     %s" % ", ".join(str(r["nodes"]) for r in runs))
    print()

    use_plugin = (decompositionPlugin
                  and args.decomposition == decompositionPlugin.NAME)
    if use_plugin:
        ok, msg = decompositionPlugin.check_method_available(
            args.multilevel_method)
        print("decomp:    %s" % msg)
        if ok is False:
            sys.exit("error: %s" % msg)
        print()

    extents = None
    if args.decomposition == "optimal":
        extents = args.domain_aspect or domain_extents()
        if extents is None:
            sys.exit("error: --decomposition optimal needs the domain size; "
                     "could not read system/blockMeshDict -- pass "
                     "--domain-aspect X,Y,Z")
        print("domain:    %g x %g x %g (interface-minimising decomposition)"
              % tuple(extents))
        print()

    prepared = []
    for mesh in args.mesh:
        polymesh = prepare_mesh(mesh, meshes_dir)

        for spec in runs:
            ncores = spec["ncores"]
            plugin_spec = None
            if use_plugin:
                plugin_spec = decompositionPlugin.spec_from_args(
                    args, ncores, spec["nodes"])
                # multiLevel replaces nHierarchical entirely; the triplet is
                # still written so the dictionary stays valid if the method is
                # switched back by hand.
                triplet = hierarchical_for(ncores, args.start)
            else:
                triplet = (optimal_triplet(ncores, extents) if extents
                           else hierarchical_for(ncores, args.start))
            run_dir = os.path.join(runs_dir, mesh, spec["dirname"])
            print("[run]  %s / %d cores on %d node(s) -> hierarchical %s"
                  % (mesh, ncores, spec["nodes"], tuple(triplet)))
            if plugin_spec:
                print("       %s" % decompositionPlugin.describe(plugin_spec))
            if spec["ompiflags"]:
                print("       placement %s" % spec["ompiflags"])

            copy_case(run_dir)
            link_polymesh(run_dir, polymesh)
            set_decomposition(run_dir, ncores, triplet)
            if plugin_spec:
                decompositionPlugin.apply(run_dir, plugin_spec)
            if args.end_time:
                set_end_time(run_dir, args.end_time)
            script = render_slurm(template_text, run_dir, mesh, ncores,
                                   triplet, args.jobname, spec["tpn"],
                                   spec["ompiflags"], spec["extra_sbatch"],
                                   label=spec["label"])
            print("       prepared %s" % os.path.relpath(script, workspace))
            prepared.append((run_dir, script))
        print()

    print("prepared %d run(s)." % len(prepared))

    if args.submit:
        print("\nsubmitting:")
        for run_dir, script in prepared:
            print("  sbatch %s" % script)
            subprocess.run(["sbatch", os.path.basename(script)],
                           cwd=run_dir, check=True)
    else:
        print("re-run with --submit to queue them, or submit manually:")
        for _, script in prepared:
            print("  (cd %s && sbatch run.slurm)" % os.path.dirname(script))


if __name__ == "__main__":
    main()
