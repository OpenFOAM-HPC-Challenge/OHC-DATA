#!/usr/bin/env python3
"""Benchmark set-up for the occDrivAer static-mesh case.

This is a self-contained driver (Python standard library only, no external
modules) for setting up strong-scaling benchmark runs of the OpenFOAM HPC
Challenge occDrivAer case.  It

  0. obtains the case itself -- the directory this script sits in when that
     is already a case, otherwise the upstream one, downloaded from the
     OpenFOAM HPC committee repository into ``<workspace>/case/``,
  1. downloads and extracts the requested polyMesh resolution(s)
     (fine / medium / coarse) from Zenodo -- by default all three -- or
     copies in the existing mesh ``--polymesh`` names instead,
  2. builds one self-contained case copy per (mesh, nCores) combination under
     ``<workspace>/runs/<mesh>/<nCores>/``, with the polyMesh symlinked to a
     shared, downloaded-once copy and an ``Allrun`` whose MPI launcher and
     flags ``run.slurm`` can set (``MPI_LAUNCHER``, ``OMPI_FLAGS``),
  3. writes the decomposition for each nCores into that copy's
     ``system/include/caseDefinition`` -- by default ``hierarchical`` with
     the ``decompositionIter`` doubling strategy (``--decomposition``),
  4. derives tasks per node and the topology defaults of the multilevel
     decompositions and the placement sweep from
     ``<workspace>/node-topology.yaml``, a description of the compute node
     detected on first use (``--probe-node`` rewrites it on a compute node),
  5. renders a SLURM batch script from ``--slurm-template``, or else from
     ``<workspace>/slurm.template`` -- written from a built-in default on
     first use, for you to adapt -- that supplies the sbatch directives and
     calls ``Allrun``,
  6. optionally submits every generated script with ``sbatch`` (``--submit``).

Harvesting the finished runs back into a submission file is the job of the
sibling script ``postprocess.py``; what the two share lives in ``common.py``.

Examples
--------
    # Prepare all meshes at 128 and 256 cores (does not submit).  Run from
    # outside a case, this downloads the case as well as the meshes.
    ./benchmark.py --ncores 128,256

    # Only the coarse mesh, a scaling sweep, submitted to the queue
    ./benchmark.py --mesh coarse --ncores 32,64,128,256,512 \
                   --slurm-template slurm.template --submit

    # Custom workspace location
    ./benchmark.py --ncores 512 --workspace /scratch/$USER/occDrivAer

    # ... and afterwards, to harvest what the queue produced:
    ./postprocess.py --workspace /scratch/$USER/occDrivAer
"""

import argparse
import gzip
import itertools
import os
import re
import shlex
import shutil
import string
import subprocess
import sys
import tarfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (CASE_ARCHIVE_URL, CASE_DEFINITION, CASE_DIR,
                    CASE_WEB_URL, DECOMPOSITIONS, DEFAULT_DECOMPOSITION,
                    MESH_CELLS, MESHES, ZENODO_URL, add_shared_arguments,
                    check_method_available, run_dirname,
                    display_path, is_case, parse_posint_list, parse_start)
import topology

# Optional: decomposition methods that need more than a coefficient triplet
# (currently multiLevel) live in a sibling module.  Absent is fine -- the two
# geometric strategies below are self-contained.
try:
    import decompositionPlugin
except ImportError:
    decompositionPlugin = None


# --------------------------------------------------------------------------- #
# Static configuration
# --------------------------------------------------------------------------- #

# The hwloc objects a placement sweep can map by, and the node description
# (node-topology.yaml) count each one comes from.  On chiplet CPUs several L3
# slices share a NUMA domain, and mapping by NUMA alone packs ranks into the
# low slices and leaves the rest idle, so the finer object is often the one
# worth sweeping.  Override per run with --domains-per-node /
# --cores-per-domain.
BIND_OBJECTS = {"numa": "numa_per_node", "l3cache": "l3_per_node",
                "package": "sockets"}


def bind_object_topology(node, bind_object):
    """Domains per node and physical cores per domain of `bind_object`."""
    domains = node[BIND_OBJECTS[bind_object]]
    return {"domains_per_node": domains,
            "cores_per_domain": node["cores_per_node"] // domains}


def node_tiers(node):
    """The node description as the multilevel plugin's per-node tier counts."""
    return {"socket": node["sockets"], "numa": node["numa_per_node"],
            "cache": node["l3_per_node"]}

# Directory that contains this script is treated as the pristine source case.
CASE_DIR = os.path.dirname(os.path.abspath(__file__))

CASE_DEFINITION = os.path.join("system", "include", "caseDefinition")

# Names never copied into a run directory (large, generated, or irrelevant).
IGNORE_NAMES = {
    ".git", "figures", "workspace", "logFiles", "postProcessing",
    "polyMesh", "meshes", "runs", "__pycache__",
    "benchmark.py", "slurm.template",
}

# Built-in SLURM template, written to <workspace>/slurm.template on first use
# when --slurm-template is not given.  Placeholders use string.Template syntax
# ($name); bash ${VAR} / $VAR expansions are left untouched because their names
# are not in the substitution mapping.
SLURM_TEMPLATE_NAME = "slurm.template"
PROBE_SCRIPT_NAME = "probe.slurm"
DEFAULT_SLURM_TEMPLATE = """\
#!/bin/bash -l
#SBATCH --job-name=$jobname
#SBATCH --ntasks=$ncores
#SBATCH --ntasks-per-node=$tpn
#SBATCH --nodes=$nodes
#SBATCH --partition=arm
#SBATCH --time=24:00:00
#SBATCH --output=%x-%j.out
#SBATCH --error=%x-%j.err
$extrasbatch

export OMPI_FLAGS="$ompiflags"
export MPI_LAUNCHER="$launcher"

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


def hierarchical_for(ncores, start, quiet=False):
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
    if not quiet:
        print("  ! %d not reachable by doubling from %s; using balanced %s"
              % (ncores, start, tuple(triplet)))
    return triplet


# --------------------------------------------------------------------------- #
# Case / mesh download / extraction
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


def has_points(path):
    """True when `path` holds a mesh's points, as a polyMesh directory does."""
    return any(os.path.isfile(os.path.join(path, name))
               for name in ("points", "points.gz"))


def mesh_cells(polymesh):
    """The cell count in the header note of the mesh's owner file, or None.

    OpenFOAM writes ``note "nPoints:... nCells:... ..."`` into that header,
    which stays plain text even in a binary or compressed file.
    """
    for name, opener in (("owner", open), ("owner.gz", gzip.open)):
        path = os.path.join(polymesh, name)
        if os.path.isfile(path):
            with opener(path, "rb") as f:
                head = f.read(4096).decode("latin-1")
            m = re.search(r"nCells:\s*(\d+)", head)
            return int(m.group(1)) if m else None
    return None


def find_polymesh(root):
    """Locate a polyMesh directory (one containing 'points') under `root`."""
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.basename(dirpath) == "polyMesh" and has_points(dirpath):
            return dirpath
    return None


def find_case(root):
    """Locate an occDrivAer case directory under `root`.

    The GitLab subdirectory archive keeps the whole path inside the tarball
    (``hpc-develop-.../incompressible/simpleFoam/occDrivAerStaticMesh``), so
    the case is found by looking for its caseDefinition rather than by
    assuming a fixed nesting depth.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        if is_case(dirpath):
            return dirpath
    return None


def extract(tarball, destdir, locate=find_polymesh, what="polyMesh"):
    """Extract `tarball` into `destdir`, returning the path `locate` finds."""
    existing = locate(destdir)
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

    found = locate(destdir)
    if not found:
        raise RuntimeError("no %s found after extracting %s" % (what, tarball))
    return found


def mesh_files(polymesh):
    """{file name: size} of a polyMesh directory, to tell two meshes apart."""
    return {name: os.path.getsize(os.path.join(polymesh, name))
            for name in os.listdir(polymesh)
            if os.path.isfile(os.path.join(polymesh, name))}


def copy_polymesh(src, dst):
    """Copy the polyMesh `src` to `dst`. Atomic via .part, like download()."""
    part = dst + ".part"
    if os.path.exists(part):
        shutil.rmtree(part)
    size = sum(mesh_files(src).values())
    print("  > copying %s (%.1f GB)" % (src, size / 1e9))
    shutil.copytree(src, part)
    os.rename(part, dst)
    return dst


def prepare_mesh(mesh, meshes_dir, polymesh=None):
    """Provide one mesh in the workspace, returning the shared polyMesh path.

    A mesh already there is reused, whether or not its tarball is still
    around.  Otherwise the `polymesh` given by the user is copied in, or the
    mesh is downloaded and extracted.
    """
    tarname, label = MESHES[mesh]
    print("[mesh] %s (%s cells)" % (mesh, label))
    tarball = os.path.join(meshes_dir, tarname)
    extract_dir = os.path.join(meshes_dir, mesh)
    os.makedirs(extract_dir, exist_ok=True)

    existing = find_polymesh(extract_dir)
    if existing and polymesh and mesh_files(existing) != mesh_files(polymesh):
        sys.exit("error: the workspace already holds a different %s mesh, "
                 "%s; remove it to copy --polymesh %s in instead"
                 % (mesh, existing, polymesh))
    if existing:
        print("  = already in the workspace: %s" % existing)
        return existing
    if polymesh:
        return copy_polymesh(polymesh, os.path.join(extract_dir, "polyMesh"))
    download(ZENODO_URL.format(tarname), tarball)
    return extract(tarball, extract_dir)


def prepare_case(workspace):
    """Download the upstream case into `<workspace>/case`, returning its path.

    Downloaded once and reused: a second invocation finds the extracted case
    and touches the network not at all.
    """
    case_root = os.path.join(workspace, "case")
    os.makedirs(case_root, exist_ok=True)

    existing = find_case(case_root)
    if existing:
        print("[case] = already downloaded: %s"
              % display_path(existing, workspace))
        return existing

    print("[case] %s" % CASE_WEB_URL)
    tarball = os.path.join(case_root, "occDrivAerStaticMesh.tar.gz")
    download(CASE_ARCHIVE_URL, tarball)
    return extract(tarball, case_root, locate=find_case, what="case")


def resolve_case(args, workspace):
    """Decide which directory is the pristine case the runs are copied from.

    An explicit --case wins; otherwise the script's own directory is used when
    the scripts were dropped into a case checkout (the original convention,
    and the one that respects local modifications), and the upstream case is
    downloaded when they were not.
    """
    if args.case:
        case_dir = os.path.abspath(args.case)
        if not is_case(case_dir):
            sys.exit("error: --case %s is not an occDrivAer case (no %s)"
                     % (args.case, CASE_DEFINITION))
        return case_dir

    if not args.download_case and is_case(CASE_DIR):
        return CASE_DIR

    return prepare_case(workspace)


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


def allrun_with_launcher(text):
    """`text` of an Allrun whose MPI launcher and flags the environment sets.

    The stock Allrun hard-codes ``mpirun -np`` and resets OMPI_FLAGS, so
    neither a site without mpirun nor the placement flags run.slurm exports
    get through.  Both become overridable, MPI_LAUNCHER and OMPI_FLAGS, with
    the stock values as defaults.  Returns `text` itself when it is patched
    already, and None when it lacks the stock lines.
    """
    if "MPI_LAUNCHER" in text:
        return text
    text, n1 = re.subn(r'(?m)^OMPI_FLAGS=""', 'OMPI_FLAGS="${OMPI_FLAGS:-}"',
                       text)
    text, n2 = re.subn(
        r'(?m)^parEx="mpirun -np \$\{nProcs\} \$\{OMPI_FLAGS\}"$',
        'MPI_LAUNCHER="${MPI_LAUNCHER:-mpirun -np}"  # run.slurm may set it\n'
        'parEx="${MPI_LAUNCHER} ${nProcs} ${OMPI_FLAGS}"', text)
    return text if n1 == 1 and n2 == 1 else None


def copy_case(src, dst):
    """Copy the pristine case into `dst` (excluding mesh / generated files)."""
    for dirpath, dirnames, filenames in os.walk(src):
        ignored = _ignore(dirpath, dirnames + filenames)
        target = os.path.join(dst, os.path.relpath(dirpath, src))
        os.makedirs(target, exist_ok=True)
        subdirs = []
        for name in dirnames + filenames:
            if name in ignored:
                continue
            s, d = os.path.join(dirpath, name), os.path.join(target, name)
            if os.path.islink(s):
                if os.path.islink(d) or os.path.isfile(d):
                    os.unlink(d)
                os.symlink(os.readlink(s), d)
            elif name in dirnames:
                subdirs.append(name)
            else:
                shutil.copy2(s, d)
        dirnames[:] = subdirs


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


def set_method(run_dir, method):
    """Rewrite decompositionMethod in the run's caseDefinition."""
    path = os.path.join(run_dir, CASE_DEFINITION)
    with open(path) as f:
        text = f.read()
    text, n = re.subn(r"(?m)^(\s*decompositionMethod\s+)\S+;",
                      r"\g<1>%s;" % method, text)
    if n != 1:
        raise RuntimeError("could not set decompositionMethod in %s "
                           "(%d matches)" % (path, n))
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


def workspace_template(workspace):
    """The workspace's SLURM template as (path, fresh).

    Written from the built-in default the first time (fresh is then True) and
    reused afterwards, so editing it and re-running re-renders every run.slurm.
    """
    path = os.path.join(workspace, SLURM_TEMPLATE_NAME)
    if os.path.exists(path):
        return path, False
    with open(path, "w") as f:
        f.write(DEFAULT_SLURM_TEMPLATE)
    return path, True


def write_probe_script(template_text, workspace):
    """Render the SLURM template into <workspace>/probe.slurm, a job that runs
    --probe-node on a compute node in place of Allrun.

    It keeps the site settings of the template -- account, partition,
    environment -- and appends the probe's own directives after its last
    #SBATCH line, where they override the template's.  Returns the path, or
    None when the template has no ./Allrun line to replace.
    """
    text = string.Template(template_text).safe_substitute({
        "jobname": "probe-node", "ncores": 1, "tpn": 1, "nodes": 1,
        "casedir": workspace, "ompiflags": "", "launcher": "srun -n",
        "extrasbatch": "", "mesh": "", "nhierarchical": "",
    })
    probe = " ".join(shlex.quote(arg) for arg in (
        sys.executable, os.path.abspath(__file__), "--probe-node",
        "--workspace", workspace))
    text, n = re.subn(r"(?m)^(\s*)\./Allrun\b.*$",
                      lambda m: m.group(1) + probe, text)
    if n == 0:
        return None
    lines = text.split("\n")
    last = max([i for i, line in enumerate(lines)
                if line.startswith("#SBATCH")] or [0])
    lines[last + 1:last + 1] = ["#SBATCH --nodes=1", "#SBATCH --ntasks=1",
                                "#SBATCH --exclusive",
                                "#SBATCH --time=00:10:00"]
    path = os.path.join(workspace, PROBE_SCRIPT_NAME)
    with open(path, "w") as f:
        f.write("\n".join(lines))
    os.chmod(path, 0o755)
    return path


def render_slurm(template_text, run_dir, mesh, ncores, triplet, jobname, tpn,
                 ompiflags="", extra_sbatch="", label=None):
    """Render the SLURM script for one run and write it as run.slurm.

    `label` disambiguates the job name when nCores alone does not -- a
    fixed-total-ranks sweep runs the same core count on several node counts.
    The launcher is srun, except for runs with placement flags: those are
    Open MPI options, which only mpirun accepts.
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
        "launcher": "mpirun -np" if ompiflags else "srun -n",
        "extrasbatch": extra_sbatch,
    }
    text = string.Template(template_text).safe_substitute(mapping)
    path = os.path.join(run_dir, "run.slurm")
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)
    return path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

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

  # Copy the runs from a case of your own rather than the upstream one
  ./benchmark.py --ncores 512 --case /path/to/my/occDrivAerStaticMesh

  # Use a coarse mesh you already have instead of downloading it
  ./benchmark.py --mesh coarse --ncores 512 --polymesh /path/to/polyMesh

  # Once the queue is done, harvest that workspace with the sibling script
  ./postprocess.py --workspace /scratch/$USER/occDrivAer
"""



def build_parser():
    p = argparse.ArgumentParser(
        description="Set up strong-scaling benchmark runs for occDrivAer.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_shared_arguments(p)
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
    p.add_argument("--case", metavar="DIR",
                   help="pristine case the runs are copied from (default: "
                        "the directory this script sits in when that is a "
                        "case, otherwise the upstream case, downloaded into "
                        "<workspace>/case)")
    p.add_argument("--polymesh", metavar="DIR",
                   help="existing polyMesh directory to copy into "
                        "<workspace>/meshes instead of downloading the mesh; "
                        "--mesh must name the one resolution it is "
                        "(default: the Zenodo mesh)")
    p.add_argument("--download-case", action="store_true", dest="download_case",
                   help="always fetch the upstream case from the OpenFOAM HPC "
                        "committee repository, even when this script already "
                        "sits in a case")
    p.add_argument("--slurm-template", metavar="FILE",
                   help="SLURM template file providing sbatch directives; "
                        "$ncores $nhierarchical $mesh $jobname $tpn $nodes "
                        "$casedir are substituted (default: "
                        "<workspace>/slurm.template, written from the "
                        "built-in template on first use)")
    p.add_argument("--decomposition", dest="decomposition",
                   choices=tuple(d for d in DECOMPOSITIONS
                                 if decompositionPlugin
                                 or not d.startswith("multilevel")),
                   default=DEFAULT_DECOMPOSITION,
                   help="OpenFOAM method and how it is set up: "
                        "'hierarchical-doubling' follows the --start triplet "
                        "cyclically (the case's own convention); "
                        "'hierarchical-optimal' minimises the interface area "
                        "against the blockMeshDict bounding box, which "
                        "matters on this elongated domain; 'scotch' needs no "
                        "set-up%s. All but the default get their own run "
                        "directories, e.g. runs/<mesh>/512-scotch "
                        "(default: %s)"
                        % ("; 'multilevel-scotch', 'multilevel-metis' and "
                           "'multilevel-kahip' split by hardware tier with "
                           "that method in every level, see "
                           "decompositionPlugin.py"
                           if decompositionPlugin else "",
                           DEFAULT_DECOMPOSITION))
    p.add_argument("--domain-aspect", type=parse_start, dest="domain_aspect",
                   metavar="X,Y,Z",
                   help="override the domain side lengths used by "
                        "--decomposition hierarchical-optimal (default: "
                        "read from system/blockMeshDict)")
    p.add_argument("--start", type=parse_start, default=[1, 1, 1],
                   metavar="X,Y,Z",
                   help="start triplet for the decompositionIter doubling "
                        "(default: 1,1,1)")
    p.add_argument("--jobname", default="DrivAer_RANS",
                   help="base SLURM job name (default: DrivAer_RANS)")
    p.add_argument("--ntasks-per-node", type=int, default=None, dest="tpn",
                   metavar="N",
                   help="tasks per node used to derive --nodes (default: "
                        "cores_per_node of the node description)")
    p.add_argument("--probe-node", action="store_true", dest="probe_node",
                   help="only detect this machine's topology and write it to "
                        "<workspace>/%s, replacing what is there, and write "
                        "<workspace>/%s, a job that does the same on a "
                        "compute node" % (topology.NODE_FILE,
                                          PROBE_SCRIPT_NAME))
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
                   help="domains of --bind-object per node (default: from "
                        "the node description)")
    g.add_argument("--cores-per-numa", "--cores-per-domain", type=int,
                   default=None, dest="cores_per_domain", metavar="N",
                   help="physical cores per domain (default: from the node "
                        "description). Above this the sweep binds to "
                        "hardware threads instead of cores and asks SLURM for "
                        "two tasks per core")

    if decompositionPlugin:
        decompositionPlugin.add_arguments(
            p.add_argument_group("decomposition plugin"))

    # Harvesting used to live here as --postProcess.  It is its own script
    # now; catch the old flag rather than letting argparse call it unknown.
    p.add_argument("--postProcess", action="store_true", dest="postprocess",
                   help=argparse.SUPPRESS)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.case and args.download_case:
        parser.error("--case names a local case; drop --download-case")

    if args.polymesh:
        if len(args.mesh) != 1:
            parser.error("--polymesh is the mesh of one resolution; name it "
                         "with --mesh fine, medium or coarse")
        if not has_points(args.polymesh):
            parser.error("--polymesh %s is not a polyMesh directory (no "
                         "points or points.gz in it)" % args.polymesh)
        args.polymesh = os.path.abspath(args.polymesh)
        # The resolution names the results, so a mesh given under the wrong
        # one would be harvested under the wrong cell count.
        mesh, cells = args.mesh[0], mesh_cells(args.polymesh)
        if cells is None:
            print("warning: cannot read the cell count from the owner file "
                  "of --polymesh %s; not confirmed to be the %s mesh"
                  % (args.polymesh, mesh), file=sys.stderr)
        elif abs(cells / MESH_CELLS[mesh] - 1) > 0.1:
            closest = min(MESH_CELLS,
                          key=lambda m: abs(cells / MESH_CELLS[m] - 1))
            hint = ("it looks like --mesh %s" % closest
                    if abs(cells / MESH_CELLS[closest] - 1) <= 0.1
                    else "it matches none of the resolutions")
            parser.error("--polymesh %s has %d cells, not the %s mesh "
                         "(%dM); %s" % (args.polymesh, cells, mesh,
                                        MESH_CELLS[mesh] / 1e6, hint))

    if args.postprocess:
        parser.error("--postProcess has moved into its own script; run "
                     "'postprocess.py --workspace %s' instead" % args.workspace)

    # The node description supplies every per-node default below: the
    # workspace's node-topology.yaml, or -- the first time -- this machine,
    # detected.
    workspace = os.path.abspath(args.workspace)
    node_path = os.path.join(workspace, topology.NODE_FILE)
    if args.probe_node:
        node, undetected = topology.detect()
        os.makedirs(workspace, exist_ok=True)
        topology.write(node_path, node, undetected)
        print("wrote %s: %s, detected on %s"
              % (node_path, topology.describe(node), node[topology.HOST_KEY]))
        if undetected:
            print("not detected, fill in: %s" % ", ".join(undetected))
        if os.environ.get("SLURM_JOB_ID"):
            return          # already on a compute node
        if args.slurm_template:
            template_path, fresh_template = args.slurm_template, False
        else:
            template_path, fresh_template = workspace_template(workspace)
        with open(template_path) as f:
            script = write_probe_script(f.read(), workspace)
        if script is None:
            print("\nthat is %s, the machine this ran on. To describe the "
                  "compute nodes instead, run --probe-node on one, e.g. in an "
                  "srun: %s has no ./Allrun line to turn into a probe job."
                  % (node[topology.HOST_KEY], template_path))
            return
        print("\nthat is %s, the machine this ran on. To describe the "
              "compute nodes instead, submit the probe job written from %s%s:"
              % (node[topology.HOST_KEY], template_path,
                 " -- just written from the built-in default, so adapt it to "
                 "your site and re-run --probe-node first"
                 if fresh_template else ""))
        print("  (cd %s && sbatch %s)" % (workspace, PROBE_SCRIPT_NAME))
        return
    fresh_node = not os.path.exists(node_path)
    if fresh_node:
        node, undetected = topology.detect()
    else:
        try:
            node = topology.load(node_path)
        except ValueError as exc:
            sys.exit("error: %s" % exc)
        undetected = []

    # Resolve the runs: --ncores directly, the product of --nnodes and
    # --ncores-per-node, or a NUMA placement sweep.  The paths are exclusive.
    # Each entry is a dict so a run carries its own node count, directory name
    # and placement -- a fixed-total-ranks sweep has identical nCores
    # throughout and could not be keyed on that.
    tpn = args.tpn or node["cores_per_node"]
    runs = []

    if args.ranks_per_numa is not None:
        if args.ncores is not None or args.ncores_per_node is not None:
            parser.error("--ranks-per-domain cannot be combined with "
                         "--ncores/--ncores-per-node")
        # Topology follows the chosen object unless overridden explicitly.
        topo = bind_object_topology(node, args.bind_object)
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

    # Every decomposition but the default gets its own run directories and
    # job names, so several can be prepared side by side in one workspace.
    for spec in runs:
        spec["dirname"] = run_dirname(spec["dirname"], args.decomposition)
        if args.decomposition != DEFAULT_DECOMPOSITION:
            spec["label"] = "%s_%s" % (spec["label"] or spec["ncores"],
                                       args.decomposition)

    if args.submit and not shutil.which("sbatch"):
        sys.exit("error: --submit given but 'sbatch' not found in PATH")

    meshes_dir = os.path.join(workspace, "meshes")
    runs_dir = os.path.join(workspace, "runs")
    os.makedirs(meshes_dir, exist_ok=True)
    os.makedirs(runs_dir, exist_ok=True)
    if fresh_node:
        topology.write(node_path, node, undetected)

    if args.slurm_template:
        template_path, fresh_template = args.slurm_template, False
    else:
        template_path, fresh_template = workspace_template(workspace)
    with open(template_path) as f:
        template_text = f.read()

    print("workspace: %s" % workspace)
    print("template:  %s%s" % (display_path(template_path, workspace),
                               " (written from the built-in default)"
                               if fresh_template else ""))
    print("node:      %s (%s%s)" % (topology.describe(node),
                                    display_path(node_path, workspace),
                                    ", %sdetected on %s"
                                    % ("just " if fresh_node else "",
                                       node[topology.HOST_KEY])
                                    if node.get(topology.HOST_KEY) else ""))
    if undetected:
        print("           not detected, fallbacks used: %s"
              % ", ".join(undetected))
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

    case_dir = resolve_case(args, workspace)
    print("case:      %s" % display_path(case_dir, workspace))
    allrun = None
    if os.path.isfile(os.path.join(case_dir, "Allrun")):
        with open(os.path.join(case_dir, "Allrun")) as f:
            allrun = allrun_with_launcher(f.read())
    if allrun is None:
        print("warning: the case's Allrun has no stock mpirun/OMPI_FLAGS "
              "lines; MPI_LAUNCHER and OMPI_FLAGS from run.slurm will not "
              "reach it", file=sys.stderr)
    print()

    use_plugin = (decompositionPlugin
                  and args.decomposition in decompositionPlugin.NAMES)
    hierarchical = args.decomposition.startswith("hierarchical")
    if not hierarchical:
        ok, msg = check_method_available(
            decompositionPlugin.METHODS[args.decomposition] if use_plugin
            else args.decomposition)
        print("decomp:    %s" % msg)
        if ok is False:
            sys.exit("error: %s" % msg)
        print()

    extents = None
    if args.decomposition == "hierarchical-optimal":
        extents = args.domain_aspect or domain_extents(case_dir)
        if extents is None:
            sys.exit("error: --decomposition hierarchical-optimal needs the "
                     "domain size; could not read system/blockMeshDict -- "
                     "pass --domain-aspect X,Y,Z")
        print("domain:    %g x %g x %g (interface-minimising decomposition)"
              % tuple(extents))
        print()

    prepared = []
    for mesh in args.mesh:
        polymesh = prepare_mesh(mesh, meshes_dir, args.polymesh)

        for spec in runs:
            ncores = spec["ncores"]
            plugin_spec = None
            if use_plugin:
                plugin_spec = decompositionPlugin.spec_from_args(
                    args, ncores, spec["nodes"], node_tiers(node))
            # scotch and multiLevel do not use nHierarchical; the triplet is
            # still written so the dictionary stays valid if the method is
            # switched back by hand.
            triplet = (optimal_triplet(ncores, extents) if extents
                       else hierarchical_for(ncores, args.start,
                                             quiet=not hierarchical))
            run_dir = os.path.join(runs_dir, mesh, spec["dirname"])
            print("[run]  %s / %d cores on %d node(s) -> %s%s"
                  % (mesh, ncores, spec["nodes"], args.decomposition,
                     " %s" % (tuple(triplet),) if hierarchical else ""))
            if plugin_spec:
                print("       %s" % decompositionPlugin.describe(plugin_spec))
            if spec["ompiflags"]:
                print("       placement %s" % spec["ompiflags"])

            copy_case(case_dir, run_dir)
            if allrun is not None:
                with open(os.path.join(run_dir, "Allrun"), "w") as f:
                    f.write(allrun)
            link_polymesh(run_dir, polymesh)
            set_decomposition(run_dir, ncores, triplet)
            # Set the method in either case: a --case may hold another one.
            if hierarchical:
                set_method(run_dir, "hierarchical")
            elif args.decomposition == "scotch":
                set_method(run_dir, "scotch")
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

    if fresh_template:
        print("\nthe sbatch directives come from %s, just written from the "
              "built-in default -- adapt them to your site and re-run to "
              "re-render run.slurm." % template_path)
    if fresh_node:
        print("\nthe node description %s was just detected on %s -- check "
              "that it describes the compute nodes (--probe-node writes a job "
              "that rewrites it on one) and re-run."
              % (node_path, node[topology.HOST_KEY]))
    unreviewed = [name for name, fresh in (("the template", fresh_template),
                                           ("the node description",
                                            fresh_node)) if fresh]
    if args.submit and not unreviewed:
        print("\nsubmitting:")
        for run_dir, script in prepared:
            print("  sbatch %s" % script)
            subprocess.run(["sbatch", os.path.basename(script)],
                           cwd=run_dir, check=True)
    else:
        if args.submit:
            print("not submitting: review %s first."
                  % " and ".join(unreviewed))
        print("re-run with --submit to queue them, or submit manually:")
        for _, script in prepared:
            print("  (cd %s && sbatch run.slurm)" % os.path.dirname(script))




if __name__ == "__main__":
    main()
