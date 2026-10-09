# OHC-DATA

Results of the [OpenFOAM HPC Challenge](https://exafoam.eu/hpc-challenge/),
collected as one dataset.

- [The dataset](#the-dataset)
- [Generating the submission data](#generating-the-submission-data)
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

### 1. Set the runs up

The `benchmark.py` script automates the preprocessing procedure of downloading
the case and the meshes, and decomposing according to the provided number of
ranks. Nothing has to be in place first:

```sh
./benchmark.py --mesh coarse --ncores 128,256,512 \
               --workspace /scratch/$USER/occDrivAer
```

That fetches the
[occDrivAerStaticMesh case](https://develop.openfoam.com/committees/hpc/-/tree/develop/incompressible/simpleFoam/occDrivAerStaticMesh)
from the OpenFOAM HPC committee repository into `<workspace>/case`, downloads
the requested polyMesh resolutions from Zenodo (fine, medium and coarse -- all
three unless `--mesh` says otherwise), builds one case copy per mesh and core
count with the mesh symlinked to the shared copy, writes the hierarchical
decomposition into each copy's `system/include/caseDefinition`, and renders a
SLURM script beside it. Add `--submit` to queue them straight away, or submit
the generated `run.slurm` files yourself.

The sbatch directives come from `<workspace>/slurm.template`. The first
invocation writes it from the built-in default and does not submit even with
`--submit`, because that default is a starting point, not a portable one: set
the account, partition, time limit and environment your site needs, then run
the same command again to re-render every `run.slurm` from it.

Both downloads happen once and are reused, so a second invocation against the
same workspace touches the network not at all. If you drop the scripts into a
case checkout of your own, that case is used instead of the upstream one --
so local modifications are never silently replaced.

The options worth knowing about:

| Option | What it does | Default |
| --- | --- | --- |
| `--mesh coarse,medium` | Restricts the sweep to some of the resolutions. `all` names them all. | all three -- `coarse,medium,fine` |
| `--workspace PATH` | Where the case, the meshes and the run copies live. | `workspace/` under the current directory |
| `--case DIR` | Copies the runs from a case of your own instead of the upstream one. | the script's own directory when that is a case, otherwise the upstream case |
| `--download-case` | Forces the upstream case even when the script sits in a case checkout. | off |
| `--slurm-template FILE` | Supplies your site's sbatch directives from a file of your own. | `<workspace>/slurm.template`, written from the built-in default on first use |
| `--nnodes 1,2,4 --ncores-per-node 128` | Specifies the sweep by node count instead of by core count. | unset -- the sweep comes from `--ncores` |
| `--decomposition optimal` | Picks the triplet minimising interface area against the `blockMeshDict` bounding box, instead of the case's cyclic doubling. | `doubling`, from `--start 1,1,1` |
| `--ranks-per-domain 8,12,16 --bind-object l3cache` | Sweeps MPI rank placement inside a node. | no placement sweep; `--bind-object numa` |
| `--end-time N` | Shortens the runs for a placement or tuning sweep. A challenge result needs the stock 4000 iterations. | the case's own `endTime`, 4000 upstream |

`./benchmark.py --help` lists the rest.

### 2. Harvest the finished runs

Once the queue is through, `postprocess.py` reads the `logFiles/` and
`postProcessing/` output back out of the same workspace:

```sh
./postprocess.py --workspace /scratch/$USER/occDrivAer --format json
```

The first invocation writes `<workspace>/submissionMeta.yaml` and stops. That
file holds everything no OpenFOAM log can know: your participant ID,
affiliation and e-mail, the CPU and memory description, the interconnect, and
the per-node TDP the energy figures are derived from. Fill in its `FIXME`
fields, then run the same command again.

It then prints a scaling table per mesh and writes one submission per mesh to
`<workspace>/submissions/<YYYYMMDD>_<suffix>/`, named after your participant
ID and the cell count -- `07_OHC1_DrivAer_65M.json`. Runs that never reached
the requested iteration count are harvested as well and marked partial, so a
sweep can be inspected while the queue is still working through it.

`--format yaml` (the default) writes the fuller document, including the force
coefficient history, and `--format both` writes the two. The JSON is the one
this repository ingests; copy it into `submissions/` and open the pull
request.

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
