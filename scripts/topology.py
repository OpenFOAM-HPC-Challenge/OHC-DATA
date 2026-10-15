#!/usr/bin/env python3
"""Hardware topology of the compute node the benchmark runs go to.

benchmark.py derives its per-node defaults from one description of that node
-- tasks per node, the multiLevel topology and the domains of a placement
sweep -- kept as ``<workspace>/node-topology.yaml``:

    detected_on: nid001234
    sockets: 2
    numa_per_node: 8
    l3_per_node: 16
    cores_per_node: 128
    threads_per_core: 2

The counts are per node, and ``cores_per_node`` counts physical cores.  The
file is detected the first time and edited by hand afterwards;
``detected_on`` names the machine it was detected on, and is optional.

Detection is Linux only and reads sysfs (``/sys/devices/system/{cpu,node}``),
counting every CPU the node has online -- not just those this process may run
on, which inside a SLURM job step is the step's share of the node.  Anywhere
else, or where sysfs is incomplete -- some VMs, containers, ARM kernels -- a
count falls back to a safe value and is marked in the file as not detected,
to be filled in.

Below the counts, the file keeps a few hardware facts read on the same node
-- CPU name, memory, last-level cache size, interconnect -- from which
postprocess.py fills the hardware part of its metadata template.

Standard library only, like the rest of these scripts.
"""

import os
import re
import socket
import sys

NODE_FILE = "node-topology.yaml"
SYSFS = "/sys/devices/system"

# In file order; every one is a positive integer.
KEYS = ("sockets", "numa_per_node", "l3_per_node", "cores_per_node",
        "threads_per_core")

UNDETECTED = "# not detected -- fill in"

# Optional, and not a count: the machine the counts were detected on.
HOST_KEY = "detected_on"

# Optional hardware facts, kept as read: the CPU's model name, MemTotal and the
# size of one last-level cache in kB, and the interconnect.
INFO_KEYS = ("cpu_name", "memory_kb", "llc_kb", "interconnect")


def _read(path):
    with open(path) as f:
        return f.read().strip()


def parse_cpulist(text):
    """'0-3,8,10-11' -> {0, 1, 2, 3, 8, 10, 11}."""
    cpus = set()
    for part in text.strip().split(","):
        if not part:
            continue
        if "-" in part:
            first, last = part.split("-", 1)
            cpus.update(range(int(first), int(last) + 1))
        else:
            cpus.add(int(part))
    return cpus


def _online_cpus(sysfs):
    """The node's online CPUs, else those this process may use, else None."""
    try:
        return parse_cpulist(_read(os.path.join(sysfs, "cpu", "online")))
    except (OSError, ValueError):
        pass
    try:
        return set(os.sched_getaffinity(0))
    except AttributeError:          # not Linux
        return None


def _cores(cpu_dir, cpus):
    """{(package, core): {cpus}} from cpuN/topology, or None if unreadable.

    A core is keyed by the CPUs sharing it (thread_siblings_list) where the
    kernel provides that: core_id only counts within a package, so cores of
    different packages collide when the package is unknown (-1).
    """
    cores = {}
    try:
        for cpu in cpus:
            topo = os.path.join(cpu_dir, "cpu%d" % cpu, "topology")
            package = int(_read(os.path.join(topo, "physical_package_id")))
            siblings = os.path.join(topo, "thread_siblings_list")
            if os.path.exists(siblings):
                core = frozenset(parse_cpulist(_read(siblings)))
            elif package < 0:
                return None                 # core_id alone is ambiguous
            else:
                core = int(_read(os.path.join(topo, "core_id")))
            cores.setdefault((package, core), set()).add(cpu)
    except (OSError, ValueError):
        return None
    return cores


def _numa_domains(node_dir, cpus):
    """Number of NUMA nodes holding any of `cpus`, or None if unknown."""
    if not os.path.isdir(node_dir):
        return None
    count = 0
    for name in os.listdir(node_dir):
        if not re.match(r"node\d+$", name):
            continue
        try:
            if parse_cpulist(_read(os.path.join(node_dir, name, "cpulist"))) \
                    & cpus:
                count += 1
        except (OSError, ValueError):
            return None
    return count or None


def _last_level_caches(cpu_dir, cpus):
    """Number of distinct last-level caches over `cpus`, or None if unknown.

    That is the L3 on x86; on CPUs without one (A64FX, some ARM) the highest
    data cache stands in for it.  A machine whose CPUs disagree on the level
    is reported as unknown rather than guessed.
    """
    groups = set()
    for cpu in cpus:
        cache_dir = os.path.join(cpu_dir, "cpu%d" % cpu, "cache")
        best = None
        try:
            for name in os.listdir(cache_dir):
                if not re.match(r"index\d+$", name):
                    continue
                index = os.path.join(cache_dir, name)
                if _read(os.path.join(index, "type")) == "Instruction":
                    continue
                level = int(_read(os.path.join(index, "level")))
                shared = parse_cpulist(
                    _read(os.path.join(index, "shared_cpu_list")))
                if best is None or level > best[0]:
                    best = (level, frozenset(shared & cpus))
        except (OSError, ValueError):
            return None
        if best is None:
            return None
        groups.add(best)
    if len({level for level, _ in groups}) != 1:
        return None
    return len(groups)


def _interconnect(classes):
    """The high-speed network of this node, from /sys/class, or None."""
    def entries(name):
        try:
            return sorted(os.listdir(os.path.join(classes, name)))
        except OSError:
            return []
    if entries("cxi"):
        return "HPE Slingshot 11"
    best = None
    for dev in entries("infiniband"):
        ports = os.path.join(classes, "infiniband", dev, "ports")
        try:
            port_names = os.listdir(ports)
        except OSError:
            continue
        for port in port_names:
            try:
                rate = _read(os.path.join(ports, port, "rate"))
                layer = _read(os.path.join(ports, port, "link_layer"))
            except OSError:
                continue
            m = re.match(r"(\d+) Gb/sec(?: \(\w+ (\w+)\))?", rate)
            if not m or layer != "InfiniBand":
                continue        # RoCE ports carry Ethernet, not InfiniBand
            gbit = int(m.group(1))
            name = ("Omni-Path %d Gb/s" % gbit if dev.startswith("hfi")
                    else "InfiniBand %s%d Gb/s"
                    % (m.group(2) + " " if m.group(2) else "", gbit))
            if best is None or gbit > best[0]:
                best = (gbit, name)
    return best[1] if best else None


def hardware(sysfs=SYSFS, proc="/proc", classes="/sys/class"):
    """The INFO_KEYS facts this node can tell about itself, as far as read."""
    info = {}
    try:
        for line in _read(os.path.join(proc, "cpuinfo")).splitlines():
            if line.startswith("model name"):
                info["cpu_name"] = " ".join(line.split(":", 1)[1].split())
                break
    except OSError:
        pass
    try:
        m = re.search(r"(?m)^MemTotal:\s+(\d+) kB",
                      _read(os.path.join(proc, "meminfo")))
        if m:
            info["memory_kb"] = int(m.group(1))
    except OSError:
        pass
    # The size of the cache _last_level_caches() counts, read off CPU 0.
    cache_dir = os.path.join(sysfs, "cpu", "cpu0", "cache")
    try:
        best = None
        for name in os.listdir(cache_dir):
            index = os.path.join(cache_dir, name)
            if not re.match(r"index\d+$", name) \
                    or _read(os.path.join(index, "type")) == "Instruction":
                continue
            level = int(_read(os.path.join(index, "level")))
            m = re.match(r"(\d+)K$", _read(os.path.join(index, "size")))
            if m and (best is None or level > best[0]):
                best = (level, int(m.group(1)))
        if best:
            info["llc_kb"] = best[1]
    except (OSError, ValueError):
        pass
    interconnect = _interconnect(classes)
    if interconnect:
        info["interconnect"] = interconnect
    return info


def detect(sysfs=SYSFS, cpus=None, platform=None):
    """This machine's node description as (node, undetected).

    `node` maps every key in KEYS to a count; `undetected` lists the keys that
    hold a fallback instead of a detected value.  `sysfs`, `cpus` and
    `platform` exist for testing.
    """
    platform = platform or sys.platform
    if cpus is None and platform.startswith("linux"):
        cpus = _online_cpus(sysfs)
    logical = len(cpus) if cpus else (os.cpu_count() or 1)

    found = {}
    cpu_dir = os.path.join(sysfs, "cpu")
    if platform.startswith("linux") and cpus and os.path.isdir(cpu_dir):
        cores = _cores(cpu_dir, cpus)
        if cores:
            found["cores_per_node"] = len(cores)
            found["threads_per_core"] = max(len(c) for c in cores.values())
            packages = {package for package, _ in cores}
            if min(packages) >= 0:              # -1: package unknown
                found["sockets"] = len(packages)
        numa = _numa_domains(os.path.join(sysfs, "node"), cpus)
        if numa:
            found["numa_per_node"] = numa
        caches = _last_level_caches(cpu_dir, cpus)
        if caches:
            found["l3_per_node"] = caches

    # A count that does not split the cores evenly is not believed.
    for key in ("sockets", "numa_per_node", "l3_per_node"):
        if key in found and found.get("cores_per_node", 0) % found[key]:
            del found[key]

    # Fallbacks nest -- a NUMA domain per socket, an L3 per NUMA domain -- so
    # the multiLevel tiers stay in containment order even when guessed.
    node = dict(found)
    node.setdefault("cores_per_node", logical)
    node.setdefault("threads_per_core", 1)
    node.setdefault("sockets", 1)
    node.setdefault("numa_per_node", node["sockets"])
    node.setdefault("l3_per_node", node["numa_per_node"])
    node[HOST_KEY] = socket.gethostname()
    if platform.startswith("linux"):
        node.update(hardware(sysfs))
    return node, [key for key in KEYS if key not in found]


def write(path, node, undetected):
    """Write `node` to `path`, marking the `undetected` counts."""
    lines = [
        "# The compute node the runs go to.  benchmark.py derives tasks per",
        "# node, the multilevel topology and the --ranks-per-domain domains",
        "# from it; counts are per node, cores are physical cores.",
        "#",
        "# %s is the machine the counts were detected on.  On a cluster,"
        % HOST_KEY,
        "# make sure they describe a compute node, not the login node --",
        "# e.g. rewrite the file on one with",
        "#   srun -N1 --exclusive ... benchmark.py --probe-node "
        "--workspace <this workspace>",
    ]
    if node.get(HOST_KEY):
        lines.append("%s: %s" % (HOST_KEY, node[HOST_KEY]))
    for key in KEYS:
        line = "%s: %d" % (key, node[key])
        if key in undetected:
            line = "%-24s %s" % (line, UNDETECTED)
        lines.append(line)
    info = [key for key in INFO_KEYS if node.get(key) not in (None, "")]
    if info:
        lines.append("")
        lines.append("# Read on the same node, for postprocess.py's metadata "
                     "template.")
        lines.extend("%s: %s" % (key, node[key]) for key in info)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def load(path):
    """Read a node description, raising ValueError naming what is wrong."""
    node, seen, problems = {}, set(), []
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if ":" not in line:
                problems.append("unreadable line %r" % line)
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            if key == HOST_KEY or key in INFO_KEYS:
                node[key] = value
                continue
            if key not in KEYS:
                problems.append("unknown key %r" % key)
                continue
            seen.add(key)
            try:
                node[key] = int(value)
            except ValueError:
                problems.append("%s is %r, not a whole number" % (key, value))
                continue
            if node[key] < 1:
                problems.append("%s must be at least 1" % key)
    problems.extend("%s is missing" % key for key in KEYS if key not in seen)
    if problems:
        raise ValueError("%s: %s" % (path, "; ".join(problems)))
    return node


def describe(node):
    """One line for the run log."""
    return ("%d socket(s), %d NUMA, %d L3, %d cores x %d thread(s)"
            % (node["sockets"], node["numa_per_node"], node["l3_per_node"],
               node["cores_per_node"], node["threads_per_core"]))


if __name__ == "__main__":
    node, undetected = detect()
    print(describe(node))
    if undetected:
        print("not detected: %s" % ", ".join(undetected))
