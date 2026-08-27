# Submitting results

Open a pull request that adds **one JSON file** to this directory.  The
`validate-submission` workflow checks it against the schema and reports every
problem it finds on the pull request; once the pull request is merged, the
`append-submissions` workflow appends the records to `data/data.json` and
commits the result.  You never edit `data/data.json` yourself.

The file name is yours to choose -- anything descriptive that no other
submission already uses, e.g. `drivaer_coarse_scaling.json`.  Nothing is read
out of it: your participant number comes from `Contributor ID` inside the
file.  Put it anywhere under this directory: a folder of your own, like
`OHC-Comitee/OHC1.json`, is picked up just as well as a file sitting here.

The one exception is `example/`, which holds a complete, valid submission to
copy.  Everything in there is treated as a sample rather than a submission --
never validated as one, never plotted, never appended -- so that no example
data can reach the dataset.

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

## The preview plot

The pull request also gets a scatter of time-to-solution against
energy-to-solution -- the `TTS_ETS` view of the challenge notebooks -- with
everything already published in grey and your runs in red, restricted to the
meshes you submitted.  Lower left is better; the dashed line is the Pareto
front of the published field, so you can see whether your machine extends it.

Draw it yourself with:

```sh
./scripts/plot_submission.py submissions/<your file>.json --output preview.svg --png
```

The SVG needs nothing installed; the PNG needs `rsvg-convert` (`librsvg2-bin`).

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
- `Run Wall-Clock Time [s]` divided by `Time per Iteration [s]` has to land
  somewhere believable (100 to 100,000 iterations).  It does *not* have to come
  out at 4000: runs stop at different iteration counts, which is exactly why
  the scored columns are normalised.  This only catches the two timings being
  in different units.
- A run with GPUs has to name the `GPU Model`.
- `Run Wall-Clock Time [s]` may be left out if `Time-To-Solution [h]` is there:
  it is read back as `Time-To-Solution x 3600` and a warning says so.  That is
  the length of a full 4000-iteration run at your reported pace, so for a
  partial run it is longer than the run actually lasted, and `Total Core Time`
  and `Total Node Time` then describe the standard case rather than what you
  really spent.
- `Run Consumed Energy [kWh]` is required when `Is Energy Reported` is 1 and
  should be left out otherwise, and a metered value has to sit within a wide
  band around TDP x wall-clock (the published ones span 0.6x to 2.4x).
- The record must not already exist in `data/data.json`, or twice in the same
  pull request.  Identity is round + contributor + track + optimization
  category + mesh + nodes + cores + GPU count + file name + the two timings.
  Repeating a configuration is fine -- a repeat run measures something
  different -- but submitting the very same measurement twice is not.
- Free-form hardware strings are compared against the values the dataset
  already uses, and a new one is reported as a warning.

## How the derived columns work

Two different things live in the derived columns, and mixing them up is the
easiest way to submit a wrong number:

- `Total Core Time [s]` and `Total Node Time [s]` are what the run **cost** --
  the measured wall clock times the hardware it occupied.
- The `*-To-Solution` and energy columns are what the machine **would need for
  the standard case**: 4000 iterations at your measured time per iteration,
  drawing the system TDP throughout.  They ignore the wall clock, because runs
  stop at different iteration counts, and they ignore `Run Consumed Energy`,
  because not every site meters its runs.  That is what makes two entries
  comparable.

You can leave all of them out and let the append compute them.  Every formula
reproduces all 385 published records exactly.

## Field reference

Regenerate the table below in place with
`./scripts/validate_submission.py --schema-doc submissions/README.md`.

<!-- fields:start -->

### Required (18)

Every record must carry these.

| Field | Type | Accepted values / notes |
| --- | --- | --- |
| `Contributor ID` | str | Participant number assigned by the challenge organisers. |
| `Contributor Affiliation` | str | Institution or company the submission is credited to. |
| `File Name` | str | Name of the source result file this record was harvested from. |
| `Track` | str | `Hardware Track`, `Software Track` |
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
| `Is Energy Reported` | flag | 1 when the energy was measured, 0 when derived from the TDP. |
| `Is Partial` | flag | 1 when the run stopped before the requested end time. |

### Optional (12)

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
| `Run Consumed Energy [kWh]` | float | >= 0 What the run actually drew, when it was metered. Required if Is Energy Reported is 1; leave it out otherwise. It is reported for the record and does not feed the energy columns below, which are normalised from the TDP so that entries stay comparable. stored as `1e-32` when absent |

### Supplied on merge (2)

May be omitted from a submission: the merge fills `OHC` from the current iteration and `Submission Date` from the merge date. A submitted value is validated and kept as submitted.

| Field | Type | Accepted values / notes |
| --- | --- | --- |
| `OHC` | int | >= 1 Round of the challenge this result was submitted to. May be omitted: the merge stamps the current iteration on a submission that does not carry it. |
| `Submission Date` | date | ISO date, YYYY-MM-DD. May be omitted: the merge stamps the date the pull request was merged. For the published rounds it is the workshop the results were presented at, since the spreadsheets did not record a date. |

### Derived (13)

Computed on append when absent. A submitted value is kept as submitted and only cross-checked against the formula.

| Field | Type | Accepted values / notes |
| --- | --- | --- |
| `Total Core Time [s]` | float | What the run cost: wall-clock x cores. |
| `Total Node Time [s]` | float | What the run cost: wall-clock x nodes. |
| `Time-To-Solution [h]` | float | 4000 x time per iteration / 3600, i.e. the standard case at this machine's pace -- not the measured wall clock. |
| `Core-Time-To-Solution [h]` | float | Time-To-Solution x cores |
| `Node-Time-To-Solution [h]` | float | Time-To-Solution x nodes |
| `Energy-To-Solution [kWh]` | float | TDP x Time-To-Solution / 1000. Modelled from the TDP, not taken from Run Consumed Energy, so that metered and unmetered entries stay comparable. |
| `Energy per Iteration [J]` | float | TDP x time per iteration |
| `Energy per Iteration [kJ]` | float | TDP x time per iteration / 1000 |
| `FVOPS` | float | Finite VOlume Operations Per Second: cells / time per iteration. |
| `FVOPS per Node` | float | FVOPS / nodes |
| `FVOPS per Energy` | float | FVOPS / (TDP x time per iteration) |
| `FVOPS per Energy per Iteration` | float | Same quantity as FVOPS per Energy; both column names exist in the published data, so both are written. |
| `FVOPS per kW` | float | FVOPS / (TDP / 1000) |

<!-- fields:end -->
