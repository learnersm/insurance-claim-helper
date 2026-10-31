# AGENTS.md — for the opencode instance doing the extraction

You are the extraction worker for **insurance-claim-helper**. The script
`process.py` will call you (or you may be invoked manually) once per document.
Your job: turn one document into **one complete JSON capture**, nothing else.

## Invocation (what the script does)

```bash
opencode run -f <path/to/file.jpg> \
  "Read the file \`<abs path>\` and extract all fields as one JSON object. <INSTRUCTION_NOTE>"
```

The instruction note says: if the file contains several bills/claims (multiple
pages, or several documents photographed together), extract EACH bill as its
own JSON object and reply `{"claims": [bill1, bill2, ...]}`; if it holds
exactly one bill, reply with the bill object directly. Include every field of
each bill using snake_case keys — patient/claimant, insurance/company/policy
ids and names, hospital/clinic details, doctor, treatments, line items, dates,
amounts, currency, totals, reference numbers, notes. Reply with ONLY the JSON
— no prose, no markdown fences.

If you are an opencode instance working manually (the script isn't calling you),
do exactly this for a given file:

1. Open/read the file at its absolute path (use your file/vision tools).
2. Produce one JSON object containing EVERY field found in the document
   (see the field guidance above). Use sensible snake_case keys; keep numbers
   as numbers; keep dates exactly as printed, adding a `*_iso` sibling if the
   ISO format is obvious.
3. If parts are unreadable, give the best reading and add a top-level
   `"_confidence": "low" | "medium" | "high"`.
4. Reply with the JSON object only.
5. Save nothing yourself: reply is enough — the script writes
   `data/raw/<name>.json`, then builds `data/summary/summary.csv` from it.
   (If asked to, you may run `python3 process.py --budget 1` for the caller.)

## Rules

- NEVER invent fields that are not in the document. Missing fields are simply
  absent from the JSON.
- Never modify files under `inbox/`.
- One file → one session → one JSON object. Keep sessions small.

## What happens downstream (summary fields)

`process.py` maps your full capture into these summary columns — include them
under these exact key names when the document contains them so no information
is lost: `date`, `bill_reference_number`, `organisation_name`, `insurance_id`,
`expense_id`, `hospital_id`, `hospital_name`, `expense_description`,
`expense_date`, `amount`, `reimbursed`, `doctor` (`sl_no` is assigned
automatically, no need to extract it).
Synonyms like `invoice_number`, `bill_number`, `reference_no`, `total`,
`physician` are recognized, but the canonical keys are preferred.
