# insurance-claim-helper

Continuously extracts every field from insurance paper photos/PDFs and keeps a
running summary of all claims.

```
inbox/                <- drop .jpg / .jpeg / .pdf here (or sync a Google
                         Photos album here with rclone — see below)
data/raw/             <- ONE full JSON capture per input file
                         inbox/policy.jpg  ->  data/raw/policy.json
data/summary/         <- summary.csv  (the useful file: all claims tracked)
data/state.json       <- what was processed last, hashes, failures
```

## Why two outputs?

- `data/raw/` is the **detailed capture**: every field of every document as
  JSON. Cheaper to query later than the image itself; if the tracked field
  list changes, re-derive from raw instead of re-reading photos.
- `data/summary/summary.csv` is the **claim tracker**: one row per document
  with the currently tracked fields —

  | column | meaning |
  |---|---|
  | `insurance_id` | insurance/policy/claim id |
  | `expense_id` | medical expense / invoice / bill number |
  | `hospital_id` | hospital identifier |
  | `hospital_name` | hospital name |
  | `expense_description` | what the expense was about |
  | `expense_date` | date of the expense |
  | `amount` | amount of the expense |
  | `reimbursed` | `pending` until you mark it (reimbursed / not reimbursed) |
  | `doctor` | treating doctor |

  The list can change in the future — that's what `data/raw/` is for. Edit
  `SYNONYMS` in `process.py` to add fields.

## Run it

```bash
python3 process.py                # one run: process new files until budget, stop
python3 process.py --mock         # offline fake extractor, for testing
python3 process.py --budget 1     # process at most 1 file this run
python3 process.py -v             # verbose
```

Every run:
1. scans `inbox/` (oldest-first);
2. skips any file whose corresponding `data/raw/<name>.json` already matches
   the file's hash — **a file is processed iff it has no corresponding JSON**;
3. processes files until `max_files_per_run` is hit (this is the token/credit
   budget — each file costs roughly constant tokens);
4. stores state safely after **every single file**, so a killed run never
   loses work;
5. cron runs it again next day; it resumes exactly where it left off.

## Extraction: delegated to opencode (no API integration in this repo)

The repo ships **no LLM provider integration**. `process.py` delegates
extraction to whatever opencode instance is available (any model it has):

```bash
opencode run -f inbox/bill.jpg "Read the file ... extract all fields ..."
```

Config in `config.toml` (`[opencode]`): command, optional `-m provider/model`,
image attach on/off. Requirements for the full path:

- an opencode install (`npm i -g opencode-ai`) with at least one model
  authenticated (`opencode auth login` or `models.dev` provider keys);
- `python3.11+` (stdlib only — no pip packages needed).

The exact worker instructions live in `AGENTS.md` — read them if chaining
another opencode instance manually.

### Testing without a model

`--mock` fabricates deterministic JSON from the file hash, so the whole
pipeline (state, resumption, summary) is testable with zero credits:

```bash
python3 fixtures/make_fixtures.py              # needs pillow + reportlab
python3 process.py --mock --budget 2           # run 1: 2 files
python3 process.py --mock                      # run 2: remaining files
python3 tests/test_pipeline.py                 # full integration test
```

## Google Photos → inbox (documented, optional)

Simplest supported path: keep `inbox/` as the single source folder and sync
your Google Photos album into it with rclone:

```bash
rclone config        # create remote "gphotos" (Google Photos backend)
rclone lsf "gphotos:album/My Insurance Album"
rclone sync "gphotos:album/My Insurance Album" inbox/ \
  --include "*.{jpg,jpeg,pdf}"
```

Run that in the same cron before `process.py`; new album items become new
inbox files and get processed on the next run. (Google Photos API direct use
needs an OAuth client from Google Cloud Console; rclone handles that flow for
you and keeps this repo integration-free.)

## Scheduling (cron)

```cron
0 4 * * *  cd /path/to/insurance-claim-helper && python3 process.py >> data/cron.log 2>&1
```

(see `cron.sample`)

## Reimbursement tracking

`reimbursed` starts as `pending` in summary.csv. Because summary.csv is rebuilt
from `data/raw/` on every run, mark a claim paid by setting
`"reimbursed": "yes"` in that file's `data/raw/<name>.json` and rerunning
`python3 process.py` (or edit `reimbursed` mapping off if you track it
elsewhere).
