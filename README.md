# OHC-DATA

Results of the [OpenFOAM HPC Challenge](https://exafoam.eu/hpc-challenge/),
collected as one dataset.

## Contents

| Path | What it is |
| --- | --- |
| `data/data.json` | The global dataset: both rounds, 385 records, plus every merged submission. |
| `data/data_OHC1.json`, `data/data_OHC2.json` | The two challenge rounds as published, the sources `data.json` is built from. |
| `data/submissions.lock.json` | Which submission files have been appended, and what they contributed. |
| `submissions/` | Contributed result files, one per pull request (`submissions/example/` holds a sample). |
| `scripts/benchmark.py` | Sets up and harvests the occDrivAer benchmark runs. |
| `scripts/validate_submission.py` | Checks a submission against the schema. |
| `scripts/append_submissions.py` | Appends merged submissions to `data/data.json`. |
| `scripts/rebuild_dataset.py` | Rebuilds `data/data.json` from the round files. |
| `scripts/ohc_schema.py` | The schema itself: fields, ranges, derived quantities. |
| `Overview.ipynb` | Plots over `data/data.json`. |

## Contributing results

1. Produce a JSON submission -- by hand, or with
   `scripts/benchmark.py --postProcess --format json`.
2. Check it locally: `./scripts/validate_submission.py submissions/<your file>.json`
   (standard library only, nothing to install).
3. Open a pull request that adds it under `submissions/`.

`submissions/README.md` documents the file format and every field.

Opening the pull request runs `validate-submission`, which reports each
record's problems on the pull request and previews the rows that would be
added. Merging it runs `append-submissions`, which appends those rows to
`data/data.json` and commits the result -- so `data/data.json` is never edited
by hand. The append is keyed by the SHA-256 of each submission file in
`data/submissions.lock.json`, so it never runs twice for the same file.

## The dataset

`data/data.json` holds one record per simulation: the 43 columns the round
files publish, plus `OHC` (which round, 1 or 2) and `Submission Date`. Neither
round recorded a submission date, so every record of a round carries the date
of the workshop its results were presented at -- 2025-06-30 for OHC1,
2026-06-29 for OHC2. `scripts/rebuild_dataset.py` regenerates the file from
the round files; it refuses to run once submissions have been appended, since
those records exist nowhere else.
