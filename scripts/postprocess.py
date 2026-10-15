#!/usr/bin/env python3
"""Harvest finished occDrivAer benchmark runs into a submission file.

The counterpart of ``benchmark.py``: where that script builds and submits the
runs, this one reads them back out of the workspace again.  It parses the
``logFiles/`` written by ``Allrun`` together with the ``postProcessing/``
function-object output and emits one submission file per mesh -- by default
the full YAML document, following the data model of the OpenFOAM HPC Challenge
result spreadsheets (``META Data`` / ``Simulations`` / ``Aero Forces``), and
with ``--format json`` the flat JSON this repository's ``submissions/``
directory takes (see ``submissions/README.md``).

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

``--run NAME`` adds one more level below the machine, for when the same
machine is harvested more than once -- a different rank placement, a different
decomposition, a re-run after a fix::

    submissions/<YYYYMMDD>_<handle>/<machine>/<run>/
        meta.yaml
        OHC1_DrivAer_<machine>_<mesh>.json

Each run folder carries its own ``meta.yaml``, so a configuration that changed
the hardware description (cores used per node, threads per rank) is recorded
where the numbers it produced are, instead of one file having to describe
several runs.  Without ``--run`` the layout is unchanged.

Everything the OpenFOAM logs cannot know -- participant, affiliation, the CPU
model, the node TDP -- comes from that metadata file, written as a commented
template on the first run and edited once.

Standard library only, no external modules.

Examples
--------
    # Harvest the finished runs of a workspace into YAML submissions
    ./postprocess.py --workspace /scratch/$USER/occDrivAer

    # ... or into the JSON a submissions/ pull request is made of
    ./postprocess.py --workspace /scratch/$USER/occDrivAer --format json

    # A second placement of the same machine, beside the first
    ./postprocess.py --workspace /scratch/$USER/occDrivAer --run l3-packed

    # Only the fine mesh, subsampling the force history every 10th iteration
    ./postprocess.py --mesh fine --forces-stride 10 --suffix tuned \
                     --workspace /scratch/$USER/occDrivAer
"""

import argparse
import datetime
import json
import os
import re
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (CASE_DEFINITION, DECOMPOSITIONS, DEFAULT_DECOMPOSITION,
                    MESH_CELLS, MESHES, add_shared_arguments, display_path,
                    parse_run_dirname, read_text, sanitize)
import topology

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
    ("# OHC-1 submission metadata for postprocess.py.", None),
    ("# Everything that cannot be read out of the OpenFOAM logs lives here;", None),
    ("# edit it once, then re-run postprocess.py as often as you like.", None),
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


def read_text(path):
    """Return the contents of `path`, or None if it does not exist."""
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return None


def last_clock_time(path):
    """Return the last 'ClockTime = N s' value in an OpenFOAM log, else None."""
    text = read_text(path)
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
        text = read_text(os.path.join(run_dir, name)) or ""
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
    text = read_text(path)
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
    text = read_text(os.path.join(run_dir, CASE_DEFINITION)) or ""
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
    text = read_text(os.path.join(run_dir, "system", "fvSchemes")) or ""
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
    text = read_text(os.path.join(run_dir, "logFiles",
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
        slurm = read_text(os.path.join(run_dir, "run.slurm")) or ""
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
    text = read_text(path)
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

# --------------------------------------------------------------------------- #
# Hardware metadata the machine can tell
# --------------------------------------------------------------------------- #

def clean_cpu_name(name):
    """'Intel(R) Xeon(R) Platinum 8368 CPU @ 2.40GHz' -> 'Intel Xeon Platinum
    8368', 'AMD EPYC 7763 64-Core Processor' -> 'AMD EPYC 7763 64-Core'."""
    name = re.sub(r"\((?:R|TM)\)", "", name, flags=re.I)
    name = re.sub(r"\s+(?:CPU\s+)?@\s*[\d.]+\s*GHz\s*$", "", name)
    name = re.sub(r"\s+(?:Processor|CPU)\s*$", "", name)
    return " ".join(name.split())


def split_cpu(name):
    """(family, model, submodel) of a cleaned CPU name, or None."""
    m = re.match(r"AMD EPYC (\w{4})", name)
    if m:
        return "AMD", "EPYC", m.group(1)
    m = re.match(r"Intel Xeon (?:(?:Platinum|Gold|Silver|Bronze|Max) )?"
                 r"[A-Za-z-]*?(\d{4})", name)
    if m:
        return "Intel", "Xeon", m.group(1)
    return None


def dataset_records():
    """data/data.json of this checkout, or [] outside one."""
    try:
        with open(os.path.join(REPO, "data", "data.json")) as f:
            records = json.load(f)
    except (OSError, ValueError):
        return []
    return records if isinstance(records, list) else records.get("records", [])


def most_common(values):
    values = [v for v in values if v not in (None, "", "N/A", "-", "Unknown")]
    return max(set(values), key=values.count) if values else None


def same_cpu(record, family, submodel):
    return (record.get("CPU Family") == family
            and str(record.get("CPU Submodel")).lower() == submodel.lower())


def generation_digit(family, submodel):
    """The digit of the model number that tells the generation: the last for
    AMD EPYC, the second for Intel Xeon Scalable; None for anything else."""
    if family == "AMD":
        return submodel[-1:]
    if family == "Intel" and len(submodel) == 4:
        return submodel[1:2]
    return None


def cpu_generation(family, model, submodel, records):
    """The generation of a CPU as data/data.json has it, and a note on where
    it was found, or (None, None).

    A CPU the dataset has no record of gets the generation of the CPUs that
    share its generation digit.
    """
    known = most_common(r.get("CPU Generation") for r in records
                        if same_cpu(r, family, submodel))
    if known:
        return known, "as data/data.json has it for this CPU"
    digit = generation_digit(family, submodel)
    if digit is None:
        return None, None
    alike = [r for r in records
             if (r.get("CPU Family"), r.get("CPU Model")) == (family, model)
             and generation_digit(family, str(r.get("CPU Submodel")).lower())
             == digit]
    gen = most_common(r.get("CPU Generation") for r in alike)
    if gen is None:
        return None, None
    names = sorted({str(r.get("CPU Submodel")) for r in alike
                    if r.get("CPU Generation") == gen})
    return gen, ("as data/data.json has it for %s %s, the same generation "
                 "digit" % (model, ", ".join(names)))


def memory_size(gib):
    """The installed memory in GB that a MemTotal of `gib` GiB stands for.

    MemTotal sits a few per cent below the installed size, so it is rounded up
    to the next 2^k or 1.5 * 2^k: the sizes a power-of-two number of DIMMs
    per channel gives on 8-, 6-, 12- and 24-channel boards alike.
    """
    size = 1
    while size < gib:
        size *= 2
    if size >= 4 and size * 3 // 4 >= gib:
        size = size * 3 // 4
    return size


def file_system(path):
    """The file system `path` lives on, from /proc/mounts, or None."""
    path = os.path.realpath(path)
    best = None
    try:
        with open("/proc/mounts") as f:
            for line in f:
                fields = line.split()
                if len(fields) < 3:
                    continue
                mount = fields[1].replace("\\040", " ")
                if (path == mount or path.startswith(mount.rstrip("/") + "/")) \
                        and (best is None or len(mount) > len(best[0])):
                    best = (mount, fields[2])
    except OSError:
        return None
    return best[1] if best else None


def hardware_guesses(workspace):
    """What the hardware part of the metadata template can be filled with.

    Returns (guesses, source, hint): {key: (value, note)} for the template, the
    node the hardware facts were read on, and a hint when they are missing.
    The facts come from the workspace's node-topology.yaml, read on a compute
    node, and only without one from the machine this runs on -- the login
    node, typically, which is not what ran the job.
    """
    node_path = os.path.join(workspace, topology.NODE_FILE)
    hint = None
    if os.path.isfile(node_path):
        try:
            node = topology.load(node_path)
        except ValueError as exc:
            return {}, None, str(exc)
        source = "%s, as %s describes it" % (node.get(topology.HOST_KEY, "?"),
                                             topology.NODE_FILE)
        if not any(node.get(k) for k in topology.INFO_KEYS):
            hint = ("%s has no hardware facts yet; re-run benchmark.py "
                    "--probe-node on a compute node to record them"
                    % topology.NODE_FILE)
    else:
        node, _ = topology.detect()
        source = "%s, the machine this runs on" % node.get(topology.HOST_KEY)

    records = dataset_records()
    guesses = {}

    def guess(key, value, note):
        if value not in (None, ""):
            guesses[key] = (value, note)

    cpu = node.get("cpu_name") and clean_cpu_name(node["cpu_name"])
    guess("cpu", cpu, "the CPU's model name")
    split = cpu and split_cpu(cpu)
    if split:
        family, model, submodel = split
        guess("cpu_family", family, "from the CPU's model name")
        guess("cpu_model", model, "from the CPU's model name")
        guess("cpu_submodel", submodel, "from the CPU's model name")
        gen, how = cpu_generation(family, model, submodel, records)
        guess("cpu_generation", gen, how)
        same = [r for r in records if same_cpu(r, family, submodel)]
        bandwidth = most_common(r.get("CPU Memory Bandwidth [GB/s]")
                                for r in same)
        guess("cpu_memory_bandwidth_gb_s",
              bandwidth and "%s GB/s" % bandwidth,
              "per socket, as data/data.json has it for this CPU")
        tdps = []
        for r in same:
            try:
                nodes = float(r["Number of Nodes"])
                if float(r["Number of CPU Cores"]) / nodes \
                        == node["cores_per_node"]:
                    tdps.append(int(round(float(r["System TDP [W]"]) / nodes)))
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                pass
        tdp = most_common(tdps)
        guess("tdp_per_node_w", tdp,
              "from %d dataset run(s) with this CPU and %d cores per node"
              % (len(tdps), node["cores_per_node"]))
    if node.get("memory_kb"):
        gib = int(node["memory_kb"]) / 2.0 ** 20
        guess("memory_capacity_per_node", "%dGB" % memory_size(gib),
              "MemTotal %.1f GiB, rounded up to the next 2^k or 1.5 x 2^k GB"
              % gib)
    if node.get("llc_kb"):
        per_socket = (int(node["llc_kb"]) * node["l3_per_node"]
                      // node["sockets"] // 1024)
        guess("last_level_cache", "%dMB" % per_socket,
              "per socket, like data/data.json")
    guess("network_interconnect", node.get("interconnect"), "from /sys/class")
    # Unlike the rest, read here: the workspace is the same file system for
    # the login node as for the compute nodes.
    guess("storage_file_system", file_system(workspace),
          "the file system of the workspace, read here")
    return guesses, source, hint


def write_metadata_template(path, guesses=None, source=None):
    """Write the template, filling in `guesses` ({key: (value, note)}).

    Each filled-in value gets its note on the line above -- not after it, where
    the flat reader would take it for part of the value -- and the hardware
    section says once which node they were read on.
    """
    guesses = guesses or {}
    lines = []
    for key, value in METADATA_TEMPLATE:
        if value is None:
            lines.append(key)
            if key.startswith("# --- hardware") and guesses and source:
                lines.append("# Values with a '-- check' note were filled in "
                             "from %s." % source)
        elif key in guesses:
            value, note = guesses[key]
            lines.append("# %s -- check" % note)
            lines.append("%s: %s" % (key, yaml_scalar(value)))
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

# Hostnames that name a role rather than a machine.  A harvest is run on a
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
    text = read_text(source) or ""
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


def template_todo(guesses=()):
    """The fields of the metadata template that only the contributor can fill.

    These are the fields still holding a FIXME placeholder once `guesses` are
    filled in, plus the node TDP unless a guess was found for it.  The TDP is
    a number, so the template writes it as ``0`` rather than FIXME; left at
    zero, it empties the energy columns of every run in the submission.
    """
    todo = [key for key, value in METADATA_TEMPLATE
            if value is not None and str(value).startswith("FIXME")
            and key not in guesses]
    if "tdp_per_node_w" not in todo and "tdp_per_node_w" not in guesses:
        todo.append("tdp_per_node_w")
    return todo


def repo_submissions_dir():
    """Return the OHC-DATA checkout's ``submissions/`` directory, or None.

    When this script is run from a clone of the repository -- the usual case,
    ``./scripts/postprocess.py`` -- the harvested submissions belong straight in
    that clone's ``submissions/`` folder, which is what the pull request is
    made of.  A copy of the script that lives outside such a checkout has no
    such folder, and post-processing falls back to the workspace.
    """
    candidate = os.path.join(REPO, "submissions")
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
        guesses, source, hint = hardware_guesses(workspace)
        write_metadata_template(target, guesses, source)
        todo = template_todo(guesses)
        print("wrote a metadata template: %s" % target)
        if guesses:
            print("filled in %d hardware field(s), each marked in the file "
                  "-- check them: %s" % (len(guesses),
                                         ", ".join(sorted(guesses))))
        if hint:
            print("  ! %s" % hint)
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

    # One machine may be harvested several times -- a different placement, a
    # different decomposition, a re-run after a fix -- and those are separate
    # results, not a second draft of the same one.  --run gives each its own
    # folder below the machine, with its own meta.yaml, so nothing overwrites
    # anything and each configuration is described where its numbers are.
    # A decomposition other than the default is such a configuration too, so
    # it gets a run folder of its own unless --run names one.
    run_name = sanitize(args.run) if args.run else (
        "" if args.decomposition == DEFAULT_DECOMPOSITION
        else args.decomposition)
    if run_name:
        outdir = os.path.join(outdir, run_name)

    # The suffix also drives the OHC plot legend, so keep the two in sync when
    # it was overridden on the command line.
    meta["legend_suffix"] = suffix

    written = []
    meta_yaml = None
    for mesh in args.mesh:
        mesh_dir = os.path.join(runs_dir, mesh)
        if not os.path.isdir(mesh_dir):
            continue

        # Only the --decomposition's run directories, sorted by number so the
        # simulation list is a proper scaling sweep.
        ncores_dirs = sorted(
            (d for d in os.listdir(mesh_dir)
             if parse_run_dirname(d, args.decomposition) is not None
             and os.path.isdir(os.path.join(mesh_dir, d))),
            key=lambda d: parse_run_dirname(d, args.decomposition))

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
            # meta.yaml per folder, whichever meshes end up in it -- per
            # machine, or per run when --run split the machine further.
            updates = {"legend_suffix": suffix, "machine": machine or "-"}
            if run_name:
                updates["run"] = run_name
            meta_yaml = write_meta_yaml(
                os.path.join(outdir, "meta.yaml"), meta_path, updates)
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
    if machine and run_name:
        print("They describe run '%s' on machine '%s'.  Harvest another "
              "configuration with the same --suffix and --machine but a "
              "different --run to add it beside this one."
              % (run_name, machine))
    elif machine:
        print("They describe machine '%s'.  Harvest another machine's "
              "workspace with the same --suffix to add it to this submission "
              "as a folder of its own.  Use --run to keep several harvests of "
              "this machine apart." % machine)
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
        "generated_by": "postprocess.py --format json",
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
        "generated_by": "postprocess.py",
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

EXAMPLES = """\
examples:
  # Harvest every mesh of a workspace into YAML submissions
  ./postprocess.py --workspace /scratch/$USER/occDrivAer

  # ... or into the flat JSON the OHC-DATA repository ingests
  ./postprocess.py --workspace /scratch/$USER/occDrivAer --format json

  # Two machines under one submission: same --suffix, different --machine
  ./postprocess.py --workspace /scratch/$USER/hawk --suffix tuned
  ./postprocess.py --workspace /scratch/$USER/hkn  --suffix tuned

  # The same machine harvested twice, one folder per configuration
  ./postprocess.py --workspace /scratch/$USER/hkn --run l3-packed

  # Only the fine mesh, subsampling the force history every 10th iteration,
  # into submissions/<YYYYMMDD>_tuned/
  ./postprocess.py --mesh fine --forces-stride 10 --suffix tuned \\
                   --workspace /scratch/$USER/occDrivAer
"""


def build_parser():
    p = argparse.ArgumentParser(
        description="Harvest finished occDrivAer benchmark runs into a "
                    "submission file.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_shared_arguments(p)
    p.add_argument("--metadata", metavar="FILE",
                   help="metadata file with the fields that cannot be derived "
                        "from the logs (participant, hardware, TDP). Without "
                        "it, a commented template is written and the harvest "
                        "stops so it can be filled in "
                        "(default: <workspace>/meta.yaml, or the legacy "
                        "submissionMeta.yaml where one exists)")
    p.add_argument("--out", "--postprocess-out", metavar="DIR",
                   dest="postprocess_out",
                   help="parent directory for the generated submissions "
                        "(default: the submissions/ folder of this OHC-DATA "
                        "checkout, or <workspace>/submissions when the script "
                        "runs outside one). The files themselves land in a "
                        "<YYYYMMDD>_<suffix>/<machine>[/<run>] subdirectory "
                        "of it")
    # --sufix is accepted as an alias so the spelling used on the command line
    # does not matter.
    p.add_argument("--suffix", "--sufix", dest="suffix", metavar="NAME",
                   help="your handle: it names the dated submission directory "
                        "(<YYYYMMDD>_<NAME>) and the plot legend "
                        "(default: legend_suffix from the metadata file)")
    p.add_argument("--machine", metavar="NAME",
                   help="machine folder written inside the dated directory, "
                        "so several machines can share one submission. Each "
                        "gets its own meta.yaml. Default: 'machine' from the "
                        "metadata file, or -- where that says 'auto' -- the "
                        "scheduler's cluster name (SLURM_CLUSTER_NAME, "
                        "LMOD_SYSTEM_NAME, OHC_MACHINE) falling back to the "
                        "short hostname. Pass '-' to put the files straight "
                        "in the dated directory")
    p.add_argument("--decomposition", choices=DECOMPOSITIONS,
                   default=DEFAULT_DECOMPOSITION,
                   help="which of benchmark.py's decompositions to harvest: "
                        "its run directories (512, or 512-scotch for any but "
                        "the default) make one sweep. A non-default one is "
                        "written to a run folder of its name unless --run "
                        "gives another (default: %s)" % DEFAULT_DECOMPOSITION)
    p.add_argument("--run", metavar="NAME",
                   help="run folder written inside the machine directory, for "
                        "harvesting the same machine more than once -- a "
                        "different placement or decomposition, or a re-run "
                        "after a fix. Each run folder gets its own meta.yaml, "
                        "so a configuration that changed the hardware "
                        "description is recorded with the numbers it "
                        "produced. Default: none, and the layout is unchanged")
    p.add_argument("--avg-window", type=int, default=200, dest="avg_window",
                   metavar="N",
                   help="number of trailing iterations averaged for the "
                        "reported force coefficients (default: 200)")
    p.add_argument("--forces-stride", type=int, default=1,
                   dest="forces_stride", metavar="N",
                   help="write only every N-th sample of the force history "
                        "(default: 1, i.e. all samples)")
    p.add_argument("--no-forces", action="store_true", dest="no_forces",
                   help="omit the force coefficient history from the YAML "
                        "(the averaged values are still written)")
    p.add_argument("--format", choices=("yaml", "json", "both"), default="yaml",
                   dest="submission_format",
                   help="submission flavour to write: the full YAML document "
                        "(default), the flat JSON the OHC-DATA repository "
                        "appends to data/data.json, or both")
    # Harvesting used to be 'benchmark.py --postProcess'; accept the flag as a
    # no-op so an existing command line keeps working after the split.
    p.add_argument("--postProcess", action="store_true", dest="postprocess",
                   help=argparse.SUPPRESS)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.avg_window < 1:
        parser.error("--avg-window must be >= 1")
    if args.forces_stride < 1:
        parser.error("--forces-stride must be >= 1")

    workspace = os.path.abspath(args.workspace)
    print("workspace: %s" % workspace)
    print("decomp:    %s" % args.decomposition)
    print("meshes:    %s\n" % ", ".join(args.mesh))
    return postprocess(args, workspace)


if __name__ == "__main__":
    main()
