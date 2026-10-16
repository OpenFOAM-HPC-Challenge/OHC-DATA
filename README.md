# OHC-DATA

Results of the [OpenFOAM HPC Challenge](https://exafoam.eu/hpc-challenge/),
collected as one dataset.

- [The dataset](#the-dataset)
- [Generating the submission data](#generating-the-submission-data)
  - [Requirements](#requirements)
  - [Quick start](#quick-start)
  - [1. Set the runs up](#1-set-the-runs-up)
  - [2. Harvest the finished runs](#2-harvest-the-finished-runs)
- [Contributing results](#contributing-results)

## The dataset

`data/data.json` is the single source of truth: one record per simulation, the
43 measured columns plus `OHC` (which round, 1 or 2) and `Submission Date`.
Every result -- whole rounds and individual submissions alike -- is appended
here, and nothing reads from a second dataset. `Overview.ipynb` plots over it.

![Time-to-solution against core count, Hardware Track](assets/submissions_overview.svg)

![Contributed runs per CPU family, stacked by generation](assets/submissions_overview_cpu.svg)

Both are the Hardware Track overview plots of the challenge notebooks, drawn
straight from `data/data.json` by `scripts/plot_overview.py` -- standard
library only, like the rest of `scripts/`. The `overview-figures` workflow
redraws and commits them whenever the dataset changes, so they never fall
behind the data; `./scripts/plot_overview.py` redraws them locally.

Neither round recorded a submission date, so every record of a round carries
the date of the workshop its results were presented at -- 2025-06-30 for OHC1,
2026-06-29 for OHC2.

## Generating the submission data

`scripts/` holds a pair of drivers for the occDrivAer static-mesh case:
`benchmark.py` sets the runs up, `postprocess.py` reads them back out again.

### Requirements

- **Python 3.6 or newer.** The scripts use the standard library only, so
  there is nothing to install; downloading and extracting are done by Python
  itself.
- **An ESI OpenFOAM build** the jobs can load -- the environment part of the
  SLURM template sets it up -- with the tools `Allrun` runs: `decomposePar`,
  `renumberMesh`, `potentialFoam`, `applyBoundaryLayer` and `simpleFoam`. The
  `scotch` and `multilevel-*` decompositions need OpenFOAM built with that
  library; `benchmark.py` checks for it when the OpenFOAM environment is
  loaded and stops if only the dummy stub is there.
- **SLURM** on a cluster: `sbatch` submits the runs and the probe job. On a
  workstation, run each copy's `Allrun` directly instead.
- **Linux** to detect the compute node; anywhere else, fill in
  `node-topology.yaml` by hand.
- **Internet access** where `benchmark.py` runs: the case comes from
  develop.openfoam.com, the meshes from Zenodo. `--polymesh` takes a mesh you
  already have instead.
- **Disk space** in the workspace: the mesh downloads are 3.2 GB (coarse),
  5.9 GB (medium) and 11.5 GB (fine). The coarse mesh takes about 7 GB
  extracted, and each coarse run about 11 GB once decomposed; medium and fine
  scale roughly with their cell count.

### Quick start

From benchmark runs on a SLURM cluster to a submission:

```sh
git clone https://github.com/OpenFOAM-HPC-Challenge/OHC-DATA.git
cd OHC-DATA
W=/scratch/$USER/occDrivAer

# 1. Prepare the runs: downloads the case and the mesh, writes the SLURM
#    template and a description of this machine.  Nothing is submitted yet.
./scripts/benchmark.py --mesh coarse --ncores 128,256,512 --workspace $W

# 2. Adapt $W/slurm.template to your site: account, partition, time limit,
#    and the environment that loads OpenFOAM.

# 3. Describe a compute node rather than the login node (skip this on a
#    workstation): writes $W/probe.slurm, a one-node job that does it.
./scripts/benchmark.py --probe-node --workspace $W
(cd $W && sbatch probe.slurm)

# 4. Once the probe job has finished, render the runs again and submit them.
./scripts/benchmark.py --mesh coarse --ncores 128,256,512 --workspace $W --submit

# 5. Once the runs have finished, harvest them.  The first call writes
#    $W/meta.yaml and stops: check the values it filled in, fill in the
#    FIXME fields, then call it again.
./scripts/postprocess.py --workspace $W --format json
./scripts/postprocess.py --workspace $W --format json

# 6. Validate the submission and open a pull request with it.
./scripts/validate_submission.py submissions/<YYYYMMDD>_<suffix>
```

Sections 1 and 2 below explain these commands and their options.

### 1. Set the runs up

The `benchmark.py` script automates the preprocessing procedure of downloading
the case and the meshes, and decomposing according to the provided number of
ranks. Nothing has to be in place first:

```sh
./scripts/benchmark.py --mesh coarse --ncores 128,256,512 \
                       --workspace /scratch/$USER/occDrivAer
```

That fetches the
[occDrivAerStaticMesh case](https://develop.openfoam.com/committees/hpc/-/tree/84c262431117f5c921db8335e368a12d0e9fa3f0/incompressible/simpleFoam/occDrivAerStaticMesh)
from the OpenFOAM HPC committee repository into `<workspace>/case`, at the
commit `CASE_REF` in `scripts/common.py` pins, downloads the requested polyMesh
resolutions from Zenodo (fine, medium and coarse -- all three unless `--mesh`
says otherwise), builds one case copy per mesh and core count with the mesh
symlinked to the shared copy, writes the hierarchical decomposition into each
copy's `system/include/caseDefinition`, and renders a SLURM script beside it.
Add `--submit` to queue them straight away, or submit the generated
`run.slurm` files yourself.

The sbatch directives come from `<workspace>/slurm.template`. The first
invocation writes it from the built-in default and does not submit even with
`--submit`, because that default is a starting point, not a portable one: set
the account, partition, time limit (24 hours by default) and environment your
site needs, then run the same command again to re-render every `run.slurm`
from it. The runs launch their parallel steps with `srun -n`, which the
template exports as `MPI_LAUNCHER`; the `--ranks-per-domain` runs, whose
placement flags are Open MPI options, keep `mpirun -np`.

What a compute node looks like comes from `<workspace>/node-topology.yaml`:
sockets, NUMA domains, L3 caches, physical cores and hardware threads per
node, the machine they were detected on, which every run shows, and the CPU,
memory and interconnect `postprocess.py` describes the hardware with. Tasks
per node, the `multilevel-*` decompositions and the `--ranks-per-domain` sweep
take their defaults from it. The first invocation detects the machine it runs
on (Linux only; counts it cannot read are marked in the file) and, like a
fresh template, does not submit. On a workstation that is the right machine;
on a cluster the login node may not be. `--probe-node` rewrites the file and
writes `<workspace>/probe.slurm` from the template, a one-node job that probes
a compute node; submit it, and once it has finished, run `benchmark.py` again:

```sh
./scripts/benchmark.py --probe-node --workspace /scratch/$USER/occDrivAer
(cd /scratch/$USER/occDrivAer && sbatch probe.slurm)
```

Both downloads happen once and are reused, so a second invocation against the
same workspace touches the network not at all. A mesh you already have can be
copied into the workspace instead: `--polymesh DIR` names its `polyMesh`
directory, and `--mesh` the one resolution it is. If you drop the scripts into a
case checkout of your own, that case is used instead of the upstream one --
so local modifications are never silently replaced.

The options worth knowing about:

| Option | What it does | Default |
| --- | --- | --- |
| `--mesh coarse,medium` | Restricts the sweep to some of the resolutions. `all` names them all. | all three -- `coarse,medium,fine` |
| `--workspace PATH` | Where the case, the meshes and the run copies live. | `workspace/` under the current directory |
| `--case DIR` | Copies the runs from a case of your own instead of the upstream one. | the script's own directory when that is a case, otherwise the upstream case |
| `--polymesh DIR` | Copies an existing `polyMesh` directory into the workspace instead of downloading the mesh. Needs `--mesh` to name its single resolution, which the cell count in its `owner` header is checked against. | the Zenodo mesh |
| `--download-case` | Forces the upstream case even when the script sits in a case checkout. | off |
| `--slurm-template FILE` | Supplies your site's sbatch directives from a file of your own. | `<workspace>/slurm.template`, written from the built-in default on first use |
| `--probe-node` | Only detects the topology of the machine it runs on, writes it to `<workspace>/node-topology.yaml`, replacing what is there, and writes `<workspace>/probe.slurm`, a job that does the same on a compute node. | off -- `node-topology.yaml` is detected once, on first use |
| `--ntasks-per-node N` | Sets the ranks per node the node count is derived from. | `cores_per_node` from `node-topology.yaml` |
| `--nnodes 1,2,4 --ncores-per-node 128` | Specifies the sweep by node count instead of by core count. | unset -- the sweep comes from `--ncores` |
| `--decomposition NAME` | Picks the decomposition: `hierarchical-doubling` (the case's cyclic doubling), `hierarchical-optimal` (the triplet minimising interface area against the `blockMeshDict` bounding box), `scotch`, or `multilevel-scotch`, `multilevel-metis`, `multilevel-kahip` (one level per hardware tier from `node-topology.yaml`, that method within each). All but the default get their own run directories, e.g. `runs/coarse/512-scotch`. Only the two hierarchical ones count as Hardware Track. | `hierarchical-doubling`, from `--start 1,1,1` |
| `--ranks-per-domain 8,12,16 --bind-object l3cache` | Sweeps MPI rank placement inside a node, over the domains `node-topology.yaml` describes. | no placement sweep; `--bind-object numa` |
| `--end-time N` | Shortens the runs for a placement or tuning sweep. A challenge result needs the stock 4000 iterations. | the case's own `endTime`, 4000 upstream |

`./scripts/benchmark.py --help` lists the rest.

### 2. Harvest the finished runs

Once the queue is through, `postprocess.py` reads the `logFiles/` and
`postProcessing/` output back out of the same workspace:

```sh
./scripts/postprocess.py --workspace /scratch/$USER/occDrivAer --format json
```

The first invocation writes `<workspace>/meta.yaml` and stops. That file
holds everything no OpenFOAM log can know: your participant ID, affiliation
and e-mail, the CPU and memory description, the interconnect, and the
per-node TDP the energy figures are derived from. The hardware part is filled
in as far as it can be -- from the compute node `node-topology.yaml`
describes (so on a cluster, probe one first), from `data/data.json` for CPUs
it already knows, and from the workspace's file system -- with a note above
each value saying where it came from. Check those, fill in the remaining
`FIXME` fields, then run the same command again.

It then prints a scaling table per mesh and writes one submission per mesh,
together with a copy of `meta.yaml`, to
`submissions/<YYYYMMDD>_<suffix>/<machine>/` of this checkout (or of the
workspace when the script runs outside one): `<suffix>` is your handle,
`legend_suffix` in `meta.yaml`, and the file is named after the machine and
the cell count -- `OHC1_DrivAer_hawk_65M.json`. Runs that never reached the
requested iteration count are harvested as well and marked partial, so a sweep
can be inspected while the queue is still working through it.

One sweep is one decomposition. The default harvests the
`hierarchical-doubling` runs; `--decomposition scotch` (or any other name
`benchmark.py` took) harvests that decomposition's run directories instead and
writes them to a folder of its own, so the submissions do not overwrite each
other.

The track follows from what was run. A Hardware Track result uses the stock
case with a hierarchical decomposition, so every run copy is compared with
`data/case-manifest.json`, which records the case at the pinned commit; a
harvest whose decomposition is not hierarchical, or whose case differs, is
written as Software Track, and the differences are printed file by file.
Comments, layout and what `benchmark.py` itself sets in a run copy do not
count. `./scripts/case_manifest.py` writes the manifest when `CASE_REF` moves.

`--format yaml` (the default) writes the fuller document, including the force
coefficient history, and `--format both` writes the two. The JSON is the one
this repository ingests; check it with `./scripts/validate_submission.py` and
open the pull request.

## Contributing results

1. Fork this repository
2. Produce a JSON submission -- as described above, or manually.
3. Open a pull request that adds it under `submissions/`.

`submissions/README.md` documents the file format and every field;
`scripts/ohc_schema.py` is the schema itself -- fields, ranges and derived
quantities -- and is what the validator checks against.

Opening the pull request runs `validate-submission`, which reports each
record's problems on the pull request, previews the rows that would be added,
and draws a plot of where the new runs land among the published ones. Merging it runs `append-submissions`, which appends those rows to
`data/data.json` and commits the result -- so `data/data.json` is never edited
by hand. The append is keyed by the SHA-256 of each submission file in
`data/submissions.lock.json`, so it never runs twice for the same file.

A pull request is public and carries your name in the repository's history.
Additionally, results can be submitted anonymously instead: send either the JSON files described here or the
traditional Excel workbooks to Mark Wasserman (mawasserman@nvidia.com),
Sergey Lesnik (sergey.lesnik@wikki-gmbh.de), or Gregor Olenik
(<gregor.olenik@tum.de>), and one of them will add them to the dataset on your
behalf.
