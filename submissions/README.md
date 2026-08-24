# Submitting results

Open a pull request that adds **one JSON file** to this directory.  The
`validate-submission` workflow checks it against the schema and reports every
problem it finds on the pull request; once the pull request is merged, the
`append-submissions` workflow appends the records to `data/data.json` and
commits the result.  You never edit `data/data.json` yourself.

The file name is yours to choose -- anything descriptive that no other
submission already uses, e.g. `drivaer_coarse_scaling.json`.  Nothing is read
out of it: your participant number comes from `Contributor ID` inside the
file.  Put it directly in this directory, though; only files here are
appended.  `example/99_OHC_DrivAer_coarse.json` is a complete, valid
submission to copy, and lives one level down precisely so that it is
validated but never appended.

## Producing the file

`scripts/benchmark.py --postProcess --format json` harvests a finished
benchmark workspace straight into this format:

```sh
./scripts/benchmark.py --postProcess --workspace /scratch/$USER/occDrivAer \
                       --format json
```

It writes `<workspace>/submissions/<date>_<suffix>/<id>_OHC1_DrivAer_<mesh>.json`;
copy that file here.  Fields the OpenFOAM logs cannot know (your affiliation,
the CPU model, the node TDP) come from `submissionMeta.yaml`, which the same
command writes as a template on its first run.

## Checking it yourself

```sh
./scripts/validate_submission.py submissions/42_OHC_DrivAer_coarse.json
```

Standard library only -- no `pip install` needed.  Errors block the merge;
warnings do not, but they are usually worth a second look: a hardware string
nobody has used before is often a typo, and a derived value that disagrees
with its formula is often a unit slip.

## File format

Either a bare list of records:

```json
[
  { "Contributor ID": "42", "Mesh": "coarse", "...": "..." }
]
```

or -- easier for a scaling sweep, since every run repeats the same hardware
description -- an envelope whose `meta` block supplies the fields shared by
all of them.  A record may override anything `meta` sets:

```json
{
  "schema": "ohc-submission/v1",
  "meta": {
    "Contributor ID": "42",
    "Contributor Affiliation": "Example Institute",
    "File Name": "42_OHC_DrivAer_coarse.json",
    "Track": "Hardware Track",
    "Software Optimization Category": "Other",
    "Mesh": "coarse",
    "Number of Cells": 65000000,
    "OpenFOAM Flavor": "ESI (.com)",
    "CPU Family": "AMD",
    "CPU Model": "EPYC",
    "CPU Generation": "Genoa (4th-gen)"
  },
  "simulations": [
    {
      "Number of Nodes": 1,
      "Number of CPU Cores": 96,
      "Run Wall-Clock Time [s]": 71600.0,
      "Time per Iteration [s]": 17.9,
      "System TDP [W]": 700.0,
      "Run Consumed Energy [kWh]": 13.922222,
      "Is Energy Reported": 0,
      "Is Partial": 0
    }
  ]
}
```

One record is one simulation: one mesh, one node/core count, one run.  A
strong-scaling sweep is several records in one file.

## What is checked

Beyond the per-field types and ranges in the table below:

- `Number of Cells` has to match the mesh (65M coarse, 110M medium,
  236M fine).
- Cores are counted across the whole allocation, so they cannot be fewer than
  the nodes, and should divide evenly over them.
- `Run Wall-Clock Time [s]` divided by `Time per Iteration [s]` should come out
  near the 4000 iterations the case runs -- unless `Is Partial` is 1.
- A run with GPUs has to name the `GPU Model`.
- The energy has to sit inside the envelope its TDP and runtime allow, and has
  to equal TDP x wall-clock exactly when `Is Energy Reported` is 0.
- The record must not already exist in `data/data.json`, or twice in the same
  pull request.  Identity is round + contributor + track + optimization
  category + mesh + nodes + cores + GPU count + file name + the two timings.
  Repeating a configuration is fine -- a repeat run measures something
  different -- but submitting the very same measurement twice is not.
- Free-form hardware strings are compared against the values the dataset
  already uses, and a new one is reported as a warning.

## Field reference

Regenerate the table below in place with
`./scripts/validate_submission.py --schema-doc submissions/README.md`.

<!-- fields:start -->

### Required (21)

Every record must carry these.

| Field | Type | Accepted values / notes |
| --- | --- | --- |
| `Contributor ID` | str | Participant number assigned by the challenge organisers. |
| `Contributor Affiliation` | str | Institution or company the submission is credited to. |
| `File Name` | str | Name of the source result file this record was harvested from. |
| `Track` | str | `Hardware Track`, `Software Track` |
| `OHC` | int | >= 1 Round of the challenge this result was submitted to. |
| `Submission Date` | date | ISO date, YYYY-MM-DD. For the published rounds this is the workshop the results were presented at, since the spreadsheets did not record a date. |
| `Software Optimization Category` | str | 'Other' for a plain Hardware Track run. |
| `Mesh` | str | `coarse`, `medium`, `fine` |
| `Number of Cells` | number | > 0 Must match the nominal cell count of the mesh. |
| `OpenFOAM Flavor` | str | `ESI (.com)`, `Foundation (.org)`, `Other` |
| `Number of Nodes` | int | > 0 |
| `Number of CPU Cores` | int | > 0 Total across all nodes, not per node. |
| `CPU Family` | str | -- |
| `CPU Model` | str | -- |
| `CPU Generation` | str | -- |
| `System TDP [W]` | float | > 0 Thermal design power of the whole allocation, all nodes. |
| `Run Wall-Clock Time [s]` | float | > 0 Solver only, excluding pre-processing. |
| `Time per Iteration [s]` | float | > 0 |
| `Run Consumed Energy [kWh]` | float | >= 0 Measured when Is Energy Reported is 1, else TDP x wall-clock. |
| `Is Energy Reported` | flag | 1 when the energy was measured, 0 when derived from the TDP. |
| `Is Partial` | flag | 1 when the run stopped before the requested end time. |

### Optional (11)

May be omitted, `null`, `"N/A"`, `"-"` or `"/"`; the placeholder in the last column is stored instead.

| Field | Type | Accepted values / notes |
| --- | --- | --- |
| `Decomposition Method` | str | stored as `"N/A"` when absent |
| `Renumbering Method` | str | stored as `"N/A"` when absent |
| `Number of GPU Devices` | int | >= 0 Total across all nodes; omit it for a CPU-only run. stored as `0` when absent |
| `CPU Submodel` | str | stored as `"N/A"` when absent |
| `CPU Memory Bandwidth [GB/s]` | float | > 0 Theoretical peak per node. stored as `null` when absent |
| `GPU Model` | str | Required once Number of GPU Devices is non-zero. stored as `"N/A"` when absent |
| `GPU Memory Bandwidth [GB/s]` | float | > 0 Theoretical peak per device. stored as `null` when absent |
| `Last-Level Cache` | cache | Per node, e.g. '256MB' or 'on-package memory'. stored as `"N/A"` when absent |
| `Network Interconnect` | str | stored as `"N/A"` when absent |
| `Storage File-System` | str | stored as `"N/A"` when absent |
| `Pre-Processing Wall-Clock Time [s]` | number | >= 0 decomposePar + renumberMesh + potentialFoam + applyBoundaryLayer. stored as `null` when absent |

### Derived (13)

Computed on append when absent. A submitted value is kept as submitted and only cross-checked against the formula.

| Field | Type | Accepted values / notes |
| --- | --- | --- |
| `Total Core Time [s]` | float | wall-clock x cores |
| `Total Node Time [s]` | float | wall-clock x nodes |
| `Time-To-Solution [h]` | float | wall-clock / 3600 |
| `Core-Time-To-Solution [h]` | float | wall-clock x cores / 3600 |
| `Node-Time-To-Solution [h]` | float | wall-clock x nodes / 3600 |
| `Energy-To-Solution [kWh]` | float | Run Consumed Energy, under the column name the notebook uses. |
| `Energy per Iteration [J]` | float | energy x 3.6e6 / iterations, iterations = wall-clock / time per iteration. |
| `Energy per Iteration [kJ]` | float | Energy per Iteration [J] / 1000 |
| `FVOPS` | float | Finite VOlume Operations Per Second: cells / time per iteration. |
| `FVOPS per Node` | float | FVOPS / nodes |
| `FVOPS per Energy` | float | FVOPS / Energy per Iteration [J] |
| `FVOPS per Energy per Iteration` | float | Same quantity as FVOPS per Energy; both column names exist in the published data, so both are written. |
| `FVOPS per kW` | float | FVOPS / (TDP / 1000) |

<!-- fields:end -->
