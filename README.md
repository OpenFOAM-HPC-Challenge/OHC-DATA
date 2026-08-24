# OHC-DATA

Results of the [OpenFOAM HPC Challenge](https://exafoam.eu/hpc-challenge/),
collected as one dataset.

## Contents

| Path | What it is |
| --- | --- |
| `data/data.json` | The global dataset: both rounds, 385 records, plus every merged submission. |
| `data/data_OHC1.json`, `data/data_OHC2.json` | The two challenge rounds as published, the sources `data.json` is built from. |
| `data/submissions.lock.json` | Which submission files have been appended, and what they contributed. |
| `submissions/` | Contributed result files, at any depth (`submissions/example/` is a sample and is never ingested). |
| `scripts/benchmark.py` | Sets up and harvests the occDrivAer benchmark runs. |
| `scripts/validate_submission.py` | Checks a submission against the schema. |
| `scripts/append_submissions.py` | Appends merged submissions to `data/data.json`. |
| `scripts/rebuild_dataset.py` | Rebuilds `data/data.json` from the round files. |
| `scripts/plot_submission.py` | Draws the preview plot a pull request shows. |
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
record's problems on the pull request, previews the rows that would be added,
and draws a plot of where the new runs land among the published ones. Merging it runs `append-submissions`, which appends those rows to
`data/data.json` and commits the result -- so `data/data.json` is never edited
by hand. The append is keyed by the SHA-256 of each submission file in
`data/submissions.lock.json`, so it never runs twice for the same file.

A comment can only show an image it can fetch from a URL, so a pull request
from this repository publishes the plot to an orphan `previews` branch --
nothing but PNGs, rewritten to a single commit, never merged -- and links it as
`github.com/<repo>/raw/previews/pr-<n>.png`. While the repository is private
that URL resolves only for readers who already have access, because GitHub
serves private content to authenticated sessions and its image proxy is not
one; if the image does not appear inline, the plot is always attached to the
run as an artifact and the comment links to it.

A pull request from a fork cannot push here, so its plot goes to catbox.moe --
but only while the repository is public. Uploading a private repository's data
to a public image host would publish it, so that step is switched off until
the repository is, and fork pull requests fall back to the artifact.

## The dataset

`data/data.json` holds one record per simulation: the 43 columns the round
files publish, plus `OHC` (which round, 1 or 2) and `Submission Date`. Neither
round recorded a submission date, so every record of a round carries the date
of the workshop its results were presented at -- 2025-06-30 for OHC1,
2026-06-29 for OHC2. `scripts/rebuild_dataset.py` regenerates the file from
the round files; it refuses to run once submissions have been appended, since
those records exist nowhere else.
