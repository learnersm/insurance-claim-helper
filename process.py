#!/usr/bin/env python3
"""
insurance-claim-helper — continuous extractor for insurance photos/PDFs.

How it works (read this before reading the code):

  inbox/          you (or a Google Photos sync) drop insurance papers here:
                  .jpg / .jpeg / .pdf
  data/raw/       processor writes ONE full JSON capture per input file
                  (inbox/policy.jpg  ->  data/raw/policy.json)
  data/summary/   summary.csv — one row per claim with the tracked fields:
                  insurance id, expense id, hospital id, hospital name,
                  what the expense was about, date, amount, reimbursed, doctor
  data/state.json what was processed last, file hashes and failures.

Every run:
  1. scan inbox (sorted by modification time, oldest first)
  2. skip files that already have a corresponding raw JSON and a matching
     hash in state (re-process a file only if it changed)
  3. process files until the run budget (max_files_per_run ~ token budget)
     is consumed; finished work is stored safely after every single file
  4. next cron run picks up exactly where this one stopped.

Extraction itself is delegated to an opencode instance (see AGENTS.md);
with `--mock` a tiny built-in fake extractor lets you test the pipeline
offline.
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------ config ---

def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["root"] = os.path.dirname(os.path.abspath(path))
    inbox = os.path.join(cfg["root"], cfg["inbox"]["dir"])
    raw = os.path.join(cfg["root"], cfg["output"]["raw"])
    summary_dir = os.path.join(cfg["root"], cfg["output"]["summary"])
    for d in (inbox, raw, summary_dir):
        os.makedirs(d, exist_ok=True)
    return cfg


# ------------------------------------------------------------------- state ---

def state_path(cfg: dict) -> str:
    return os.path.join(cfg["root"], "data", "state.json")


def load_state(cfg: dict) -> dict:
    try:
        with open(state_path(cfg), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"files": {}}


def save_state(cfg: dict, state: dict) -> None:
    """Atomically store everything processed so far."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(state_path(cfg)), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, state_path(cfg))


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def raw_path_for(cfg: dict, rel: str, digest: str) -> str:
    """inbox/foo.jpg -> data/raw/foo.json (hash suffix only on stem collision)."""
    stem = os.path.splitext(os.path.basename(rel))[0]
    raw_dir = os.path.join(cfg["root"], cfg["output"]["raw"])
    name = os.path.join(raw_dir, stem + ".json")
    if os.path.exists(name):
        try:
            with open(name, encoding="utf-8") as f:
                src = json.load(f)
            if src.get("_source_sha256") == digest:
                return name  # corresponds to this exact file
        except Exception:
            pass
        return os.path.join(raw_dir, f"{stem}-{digest[:8]}.json")
    return name


# ------------------------------------------------------------ extraction ---

INSTRUCTION_NOTE = (
    "Include EVERY field in the document using snake_case keys: patient/claimant, "
    "insurance/company/policy ids and names, hospital/clinic details, doctor, "
    "treatments, line items, dates, amounts, currency, totals, reference numbers, "
    "notes. Reply with ONLY the JSON object — no prose, no markdown fences."
)


def call_opencode(cfg: dict, abs_path: str) -> str:
    """Delegate extraction to any running opencode instance (see AGENTS.md)."""
    oc = cfg.get("opencode", {})
    cmd = list(oc.get("cmd", "opencode run").split())
    model = oc.get("model", "")
    if model:
        cmd += ["-m", model]
    prompt = f"Read the file `{abs_path}` and extract all fields as one JSON object. {INSTRUCTION_NOTE}"
    ext = os.path.splitext(abs_path)[1].lower()
    if oc.get("attach_images", True) and ext != ".pdf":
        cmd += ["-f", abs_path]
    cmd += [prompt]
    proc = subprocess.run(
        cmd, cwd=cfg["root"], text=True, capture_output=True,
        timeout=int(cfg["run"].get("timeout_seconds", 600)),
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip()[-400:]
        raise RuntimeError(f"opencode failed ({proc.returncode}): {tail}")
    return proc.stdout


def mock_extract(abs_path: str) -> str:
    """Deterministic offline 'extractor' — lets the whole pipeline be tested
    without a model. Fabricates a plausible full capture from the file hash."""
    import random
    with open(abs_path, "rb") as f:
        seed = int(hashlib.sha256(f.read()).hexdigest()[:12], 16)
    rng = random.Random(seed)
    doc = {
        "mock": True,
        "claim_type": "insurance_claim",
        "insurance_company": "Continental Health Assurance",
        "insurance_id": f"INS-{rng.randint(100000, 999999)}",
        "policy_holder": "A. Person",
        "expense_id": f"INV-{rng.randint(10000, 99999)}",
        "hospital_name": rng.choice(["City Care Hospital", "MediPlus Clinic", "St. Anne's Hospital"]),
        "hospital_id": f"HSP-{rng.randint(100, 999)}",
        "doctor": rng.choice(["Dr. K. Iyer", "Dr. M. Shah", "Dr. P. Nair"]),
        "expense_description": rng.choice(
            ["Consultation and blood panel", "Dental extraction",
             "Day surgery", "Physiotherapy session"]),
        "expense_date": f"2025-0{rng.randint(1, 9)}-1{rng.randint(0, 8)}",
        "amount": rng.randint(500, 90000),
        "currency": "INR",
        "reimbursed": "no",
        "items": [
            {"item": "Room charges", "qty": rng.randint(1, 5), "price": rng.randint(1000, 5000)},
            {"item": "Medicines", "qty": 1, "price": rng.randint(200, 4000)},
        ],
    }
    return json.dumps(doc)


def parse_json_reply(text: str) -> dict:
    """Pull the largest JSON object out of a possibly chatty model reply."""
    text = text.strip()
    dec = json.JSONDecoder()
    if text.startswith("{"):
        try:
            obj, _ = dec.raw_decode(text)
            return obj
        except ValueError:
            pass
    best, best_blob = None, ""
    depth, start, in_str, esc = 0, 0, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                blob = text[start:i + 1]
                if len(blob) > len(best_blob):
                    try:
                        best, best_blob = json.loads(blob), blob
                    except ValueError:
                        pass
    if best is None:
        raise ValueError("no JSON object found in extraction reply")
    return best


# --------------------------------------------------------------- summary ---

SUMMARY_FIELDS = [
    "insurance_id", "expense_id", "hospital_id", "hospital_name",
    "expense_description", "expense_date", "amount", "reimbursed", "doctor",
]
SYNONYMS = {
    "insurance_id":        ["insurance_id", "insurance_number", "policy_number", "policy_no",
                            "claim_number", "insurance_policy_id", "tracking_number"],
    "expense_id":          ["expense_id", "medical_expense_id", "invoice_number", "invoice_id",
                            "bill_number", "receipt_number", "expense_number"],
    "hospital_id":         ["hospital_id", "hospital_code", "hospital_number"],
    "hospital_name":       ["hospital_name", "hospital", "clinic", "clinic_name", "provider_name"],
    "expense_description": ["expense_description", "expense_about", "description",
                            "diagnosis", "treatment", "treatment_desc", "service", "notes"],
    "expense_date":        ["expense_date", "date", "invoice_date", "bill_date", "visit_date",
                            "treatment_date", "date_of_expense"],
    "amount":              ["amount", "amount_paid", "total_amount", "total", "grand_total",
                            "bill_amount", "claim_amount", "amount_claimed"],
    "reimbursed":          ["reimbursed", "reimbursement_status", "status"],
    "doctor":              ["doctor", "doctor_name", "physician", "dentist", "practitioner",
                            "surgeon", "treating_doctor"],
}


def _norm(key: str) -> str:
    return str(key).lower().strip().replace(" ", "_").replace("-", "_")


def walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from walk(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk(item)


def find_field(obj, candidates):
    for strategy in (lambda k, c: _norm(k) == c, lambda k, c: c in _norm(k)):
        for cand in candidates:
            for k, v in walk(obj):
                if strategy(k, cand) and v not in (None, "", {}, []):
                    return v
    return None


def summarize(source_rel: str, raw: dict) -> dict:
    row = {}
    for field in SUMMARY_FIELDS:
        val = find_field(raw, SYNONYMS[field])
        if field == "reimbursed" and val is None:
            val = "pending"  # you track reimbursement status yourself
        row[field] = json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list)) else str(val)
    row["source_file"] = os.path.basename(source_rel)
    return row


def append_summary(cfg: dict, row: dict) -> None:
    csv_path = os.path.join(cfg["root"], cfg["output"]["summary"], "summary.csv")
    fields = SUMMARY_FIELDS + ["source_file"]
    exists = os.path.exists(csv_path)
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow(row)


# Self-healing rebuild of summary.csv from all processed raw files.
def rebuild_summary(cfg: dict, state: dict) -> int:
    raw_dir = os.path.join(cfg["root"], cfg["output"]["raw"])
    rows = []
    for rel in sorted(state.get("files", {})):
        if state["files"][rel].get("status") != "done":
            continue
        stem = os.path.splitext(os.path.basename(rel))[0]
        hits = glob.glob(os.path.join(raw_dir, stem + ".json")) + \
            glob.glob(os.path.join(raw_dir, stem + "-*.json"))
        for hit in sorted(set(hits)):
            try:
                with open(hit, encoding="utf-8") as f:
                    doc = json.load(f)
                rows.append(summarize(rel, doc))
                break
            except Exception:
                continue
    csv_path = os.path.join(cfg["root"], cfg["output"]["summary"], "summary.csv")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(csv_path), suffix=".tmp")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS + ["source_file"])
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, csv_path)
    return len(rows)


# ------------------------------------------------------------------- main ---

def scan_inbox(cfg: dict) -> list[tuple[float, str, str]]:
    inbox = os.path.join(cfg["root"], cfg["inbox"]["dir"])
    exts = {e.lower().lstrip(".") for e in cfg["inbox"]["extensions"]}
    found = []
    for dirpath, _, names in os.walk(inbox):
        for n in sorted(names):
            if os.path.splitext(n)[1].lower().lstrip(".") in exts:
                abs_path = os.path.join(dirpath, n)
                rel = os.path.relpath(abs_path, cfg["root"])
                found.append((os.path.getmtime(abs_path), rel, abs_path))
    found.sort(key=lambda t: (t[0], t[1]))
    return found


def run(cfg: dict, args) -> int:
    files = scan_inbox(cfg)
    print(f"inbox: {len(files)} supported file(s)", file=args.log)

    state = load_state(cfg)
    engine = "mock" if args.mock else cfg["run"]["engine"]
    budget = int(args.budget if args.budget is not None else cfg["run"]["max_files_per_run"])
    max_attempts = int(cfg["run"].get("max_attempts", 3))
    processed = failed = skipped = 0

    for mtime, rel, abs_path in files:
        if processed >= budget:
            print(f"budget exhausted ({budget} files this run) — rest resumes next cron run",
                  file=args.log)
            break
        digest = sha256_file(abs_path)
        meta = state["files"].get(rel, {})

        if meta.get("status") == "done" and meta.get("sha256") == digest:
            skipped += 1
            continue
        if (meta.get("status") == "failed"
                and meta.get("sha256") == digest
                and meta.get("attempts", 0) >= max_attempts):
            skipped += 1
            continue

        raw_path = raw_path_for(cfg, rel, digest)
        changed = bool(meta) and meta.get("sha256") != digest
        attempts = 1 if changed else int(meta.get("attempts", 0)) + 1
        print(f"processing {rel} [{engine}] attempt {attempts}", file=args.log)
        try:
            if engine == "mock":
                reply = mock_extract(abs_path)
            elif engine == "opencode":
                reply = call_opencode(cfg, abs_path)
            else:
                raise RuntimeError(f"unknown engine '{engine}' in config.toml [run].engine")
            doc = parse_json_reply(reply)
            if not isinstance(doc, dict):
                raise ValueError("extraction reply is not a JSON object")
            doc["_source_file"] = rel
            doc["_source_sha256"] = digest
            doc["_extracted_at"] = datetime.now(timezone.utc).isoformat()
            with open(raw_path, "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=2, ensure_ascii=False)
            state["files"][rel] = {
                "status": "done", "sha256": digest, "mtime": mtime, "attempts": attempts,
                "raw": os.path.relpath(raw_path, cfg["root"]),
                "processed_at": doc["_extracted_at"],
            }
            save_state(cfg, state)          # stored correctly before moving on
            append_summary(cfg, summarize(rel, doc))
            processed += 1
        except Exception as e:
            state["files"][rel] = {
                "status": "failed", "sha256": digest, "attempts": attempts,
                "error": str(e)[:500],
                "last_attempt": datetime.now(timezone.utc).isoformat(),
            }
            save_state(cfg, state)
            failed += 1
            print(f"  FAILED: {e}", file=args.log)

    n_rows = rebuild_summary(cfg, state)
    print(f"done: processed={processed} failed={failed} already-done={skipped} "
          f"summary_rows={n_rows}", file=args.log)
    return 0 if failed == 0 and processed >= 0 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="insurance-claim-helper processor")
    ap.add_argument("-c", "--config", default=os.path.join(ROOT, "config.toml"))
    ap.add_argument("--mock", action="store_true", help="use offline mock extractor (no LLM)")
    ap.add_argument("--budget", type=int, default=None,
                    help="max files to process this run (overrides config)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    args.log = sys.stderr
    cfg = load_config(args.config)
    return run(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
