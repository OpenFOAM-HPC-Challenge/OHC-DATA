#!/usr/bin/env python3
"""Optional decomposition strategies for benchmark.py.

benchmark.py ships two geometric strategies of its own -- ``doubling`` (the
case's cyclic ``--start`` convention) and ``optimal`` (interface-minimising
hierarchical).  Anything that needs a *different decomposition method* rather
than different coefficients lives here, so the driver stays free of
method-specific dictionary writing.

Currently provided:

``multilevel``
    OpenFOAM's ``multiLevel`` method, one level per hardware tier, each level
    decomposed with ``scotch`` (or whatever ``--multilevel-method`` says).  The
    intent is that every cut in the decomposition coincides with a real
    boundary in the machine: the first split separates nodes, the next
    separates sockets, the next L3 slices, and the innermost splits the ranks
    that share one L3.  Communication then gets cheaper the deeper it stays.

benchmark.py discovers this module with a plain ``import`` and degrades
gracefully if it is absent, so the file can be deleted without breaking the
driver.  It is also usable on its own::

    python3 decompositionPlugin.py --ncores 720 --nodes 10 --show

Requires a real ``libscotchDecomp.so`` in ``$FOAM_LIBBIN`` -- the ``dummy``
stub links but aborts at run time.  ``check_method_available()`` reports this.

Standard library only, like the rest of this case.
"""

import argparse
import os
import re
import sys

NAME = "multilevel"

# How many of each tier a single node contains, for the 2x AMD EPYC 9655 nodes
# this case is tuned on.  Counts are PER NODE, not per parent tier, so any
# ordering of tiers works out: the subdomain count for a level is simply how
# many of that tier fit inside one of the tier above it.  Overridable with
# --multilevel-topology.
DEFAULT_TOPOLOGY = {"socket": 2, "numa": 8, "cache": 24}

# Tiers in containment order, outermost first.  'node' is the sweep's node
# count and 'core' absorbs the remainder, so neither appears in the topology.
LEVEL_TIERS = ["node", "socket", "numa", "cache", "core"]


# --------------------------------------------------------------------------- #
# CLI surface, called by benchmark.py when this module is importable
# --------------------------------------------------------------------------- #

def add_arguments(group):
    """Register this plugin's options on an argparse group."""
    group.add_argument("--multilevel-method", default="scotch",
                       dest="multilevel_method", metavar="M",
                       help="decomposition method used within every level "
                            "(default: scotch)")
    group.add_argument("--multilevel-levels", default="node,socket,cache,core",
                       dest="multilevel_levels", metavar="LIST",
                       help="hardware tiers to split on, outermost first, from "
                            "%s (default: node,socket,cache,core). Must start "
                            "with 'node' and end with 'core'; the tiers between "
                            "may be any subset in containment order, e.g. "
                            "node,numa,cache,core"
                            % ",".join(LEVEL_TIERS))
    group.add_argument("--multilevel-topology", dest="multilevel_topology",
                       metavar="LIST", default=None,
                       help="per-node counts of each tier as name=N pairs "
                            "(default: %s)"
                            % ",".join("%s=%d" % kv
                                       for kv in sorted(DEFAULT_TOPOLOGY.items())))


def parse_topology(text):
    """Parse 'socket=2,numa=8,cache=24' into a per-node count mapping."""
    topo = dict(DEFAULT_TOPOLOGY)
    for item in (text or "").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError("topology entry %r is not name=N" % item)
        name, value = item.split("=", 1)
        topo[name.strip()] = int(value)
    return topo


def spec_from_args(args, ncores, nodes):
    """Build a decomposition spec from parsed benchmark.py arguments."""
    return multilevel_spec(
        ncores, nodes,
        tiers=[t.strip() for t in args.multilevel_levels.split(",") if t.strip()],
        topology=parse_topology(getattr(args, "multilevel_topology", None)),
        method=args.multilevel_method)


# --------------------------------------------------------------------------- #
# The strategy itself
# --------------------------------------------------------------------------- #

def multilevel_spec(ncores, nodes, tiers=None, topology=None,
                    method="scotch"):
    """Return a spec describing a multiLevel decomposition of `ncores`.

    Each level's subdomain count is how many of that tier fit inside one of
    the tier above it -- derived from the per-node counts in `topology`, so
    'cache' is 12 below a socket but 3 below a NUMA domain without the caller
    having to know that.  The innermost tier absorbs the remainder, and the
    product is checked against `ncores`: a silently wrong subdomain count
    would decompose fine and then mismatch the rank count at launch.
    """
    tiers = tiers or ["node", "socket", "cache", "core"]
    topology = dict(DEFAULT_TOPOLOGY if topology is None else topology)

    unknown = [t for t in tiers if t not in LEVEL_TIERS]
    if unknown:
        raise ValueError("unknown level tier(s): %s (choose from %s)"
                         % (", ".join(unknown), ", ".join(LEVEL_TIERS)))
    if tiers[0] != "node":
        raise ValueError("the outermost tier must be 'node'")
    if tiers[-1] != "core":
        raise ValueError("the innermost tier must be 'core'; it absorbs the "
                         "remaining subdomains")
    order = [LEVEL_TIERS.index(t) for t in tiers]
    if order != sorted(order):
        raise ValueError("tiers must be given outermost first, in containment "
                         "order: %s" % ", ".join(LEVEL_TIERS))

    if ncores % nodes:
        raise ValueError("%d ranks does not divide evenly over %d nodes"
                         % (ncores, nodes))
    per_node = ncores // nodes

    levels = [("node", nodes)]
    parent = 1                       # count-per-node of the tier above
    for tier in tiers[1:-1]:
        count = topology[tier]
        if count % parent:
            raise ValueError("%d %ss per node is not a multiple of the %d "
                             "above it" % (count, tier, parent))
        levels.append((tier, count // parent))
        parent = count

    if per_node % parent:
        raise ValueError("%d ranks/node is not divisible by the %d inner "
                         "domains per node" % (per_node, parent))
    levels.append(("core", per_node // parent))

    product = 1
    for _, n in levels:
        product *= n
    if product != ncores:
        raise ValueError("level product %d != nCores %d" % (product, ncores))

    return {"method": "multiLevel",
            "levels": [(t, n, method) for t, n in levels],
            "ncores": ncores}


def describe(spec):
    """One-line human summary of a spec, for benchmark.py's run log."""
    return "multiLevel %s: %s = %d" % (
        spec["levels"][0][2],
        " x ".join("%s %d" % (t, n) for t, n, _ in spec["levels"]),
        spec["ncores"])


# --------------------------------------------------------------------------- #
# Writing it into a case
# --------------------------------------------------------------------------- #

MULTILEVEL_BLOCK = """
// written by decompositionPlugin.py -- one level per hardware tier
multiLevelCoeffs
{
%s}
"""


def apply(run_dir, spec):
    """Write `spec` into one prepared run directory.

    Sets the method in system/include/caseDefinition (which decomposeParDict
    substitutes) and injects multiLevelCoeffs into the run's decomposeParDict.
    """
    case_def = os.path.join(run_dir, "system", "include", "caseDefinition")
    with open(case_def) as f:
        text = f.read()
    text, n = re.subn(r"(?m)^(\s*decompositionMethod\s+)\S+;",
                      r"\g<1>%s;" % spec["method"], text)
    if n != 1:
        raise RuntimeError("could not set decompositionMethod in %s (%d matches)"
                           % (case_def, n))
    with open(case_def, "w") as f:
        f.write(text)

    body = "".join(
        "    %-8s { numberOfSubdomains %-5s method %s; }\n"
        % (tier, "%d;" % n, meth)
        for tier, n, meth in spec["levels"])
    block = MULTILEVEL_BLOCK % body

    dpd = os.path.join(run_dir, "system", "decomposeParDict")
    with open(dpd) as f:
        text = f.read()
    # Replace an existing block so re-preparing a run stays idempotent.
    text, n = re.subn(r"\n// written by decompositionPlugin\.py.*?\n"
                      r"multiLevelCoeffs\s*\{.*?\n\}\n", "", text, flags=re.S)
    marker = "// ****"
    if marker in text:
        text = text.replace(marker, block.lstrip("\n") + "\n" + marker, 1)
    else:
        text += block
    with open(dpd, "w") as f:
        f.write(text)
    return spec


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


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Compute or apply a multiLevel decomposition.")
    p.add_argument("--ncores", type=int, required=True)
    p.add_argument("--nodes", type=int, required=True)
    p.add_argument("--run-dir", metavar="PATH",
                   help="write the decomposition into this prepared run")
    p.add_argument("--show", action="store_true",
                   help="print the spec instead of writing it")
    add_arguments(p.add_argument_group("multiLevel"))
    args = p.parse_args(argv)

    spec = spec_from_args(args, args.ncores, args.nodes)
    print(describe(spec))
    ok, msg = check_method_available(args.multilevel_method)
    print("  %s" % msg)
    if args.run_dir and not args.show:
        apply(args.run_dir, spec)
        print("  written to %s" % args.run_dir)
    return 0 if ok is not False else 1


if __name__ == "__main__":
    sys.exit(main())
