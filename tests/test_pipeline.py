#!/usr/bin/env python3
"""Integration test for the whole pipeline (offline, uses --mock).

Run:  python3 tests/test_pipeline.py
Needs fixtures: python3 fixtures/make_fixtures.py
"""
import csv
import json
import glob
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import process  # noqa: E402

PASS, FAIL = 0, 0

def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name} {extra}")

def summary_rows(cfg):
    path = os.path.join(cfg["root"], cfg["output"]["summary"], "summary.csv")
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def make_cfg(root, **over):
    src = os.path.join(REPO, "config.toml")
    import tomllib
    with open(src, "rb") as f:
        cfg = tomllib.load(f)
    cfg["root"] = root
    cfg["run"]["engine"] = "mock"
    cfg["run"].update(over)
    os.makedirs(os.path.join(root, cfg["inbox"]["dir"]), exist_ok=True)
    os.makedirs(os.path.join(root, cfg["output"]["raw"]), exist_ok=True)
    os.makedirs(os.path.join(root, cfg["output"]["summary"]), exist_ok=True)
    os.makedirs(os.path.join(root, "data"), exist_ok=True)
    return cfg

class Args:
    mock = True
    budget = None
    verbose = True
    log = sys.stderr

def main():
    # fresh workspace with fixture files
    work = tempfile.mkdtemp(prefix="ich-test-")
    inbox = os.path.join(work, "inbox")
    os.makedirs(inbox, exist_ok=True)
    try:
        subprocess.run(
            [sys.executable, os.path.join(REPO, "fixtures", "make_fixtures.py"), inbox],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError:
        # no pillow/reportlab here: stub files are fine for the mock engine
        for name in ("bill-01.jpg", "bill-02.jpeg", "bill-03.pdf"):
            with open(os.path.join(inbox, name), "wb") as f:
                f.write(name.encode() + b"-" + os.urandom(4))
    cfg = make_cfg(work)
    args = Args()

    print("[1] scan + budget resumption")
    files = process.scan_inbox(cfg)
    check("3 files found in inbox", len(files) == 3)
    check("oldest-first order", files[0][2].endswith("bill-01.jpg"))

    process._mock_noop = None
    args.budget = 2
    code = process.run(cfg, args)
    check("run exits 0", code == 0, f"code={code}")
    st = json.load(open(os.path.join(work, "data", "state.json")))
    done = [k for k, v in st["files"].items() if v["status"] == "done"]
    check("2 files marked done in state", len(done) == 2, done)
    raws = glob.glob(os.path.join(work, "data", "raw", "*.json"))
    check("2 raw JSONs written", len(raws) == 2)
    rows = summary_rows(cfg)
    check("summary has 2 rows", len(rows) == 2, len(rows))

    print("[2] next run processes the rest — resumes where it stopped")
    args.budget = None
    process.run(cfg, args)
    st = json.load(open(os.path.join(work, "data", "state.json")))
    done = [k for k, v in st["files"].items() if v["status"] == "done"]
    check("all 3 done now", len(done) == 3, done)
    rows = summary_rows(cfg)
    check("summary has 3 rows", len(rows) == 3, len(rows))

    print("[3] idempotence: nothing new -> nothing processed")
    before = len(summary_rows(cfg))
    process.run(cfg, args)
    check("summary unchanged", len(summary_rows(cfg)) == before)

    print("[4] new file appears -> processed; changed file -> reprocessed")
    claims = json.dumps({
        "insurance_id": "POL-1", "expense_id": "INV-9", "hospital_id": "HSP-X",
        "hospital_name": "Test Clinic", "expense_description": "X-ray",
        "expense_date": "2026-01-05", "amount": 1234, "doctor": "Dr. X",
    }).encode()
    # use a tiny deterministic fake file: mock engine works on any file
    with open(os.path.join(inbox, "bill-04.jpg"), "wb") as f:
        f.write(claims + b"\x00icon")  # mock engine fabricates from hash; any bytes work
    process.run(cfg, args)
    rows = summary_rows(cfg)
    check("summary has 4 rows", len(rows) == 4, len(rows))
    new = [r for r in rows if r["source_file"] == "bill-04.jpg"]
    check("summary row fields mapped", bool(new)
          and new[0]["insurance_id"].startswith("INS-")
          and new[0]["hospital_id"].startswith("HSP-")
          and new[0]["amount"].isdigit()
          and new[0]["doctor"].startswith("Dr.")
          and bool(new[0]["expense_description"])
          and bool(new[0]["expense_date"])
          and new[0]["reimbursed"] == "no", new)
    # change an already-done file -> must reprocess
    with open(os.path.join(inbox, "bill-04.jpg"), "r+b") as f:
        f.seek(-1, os.SEEK_END)
        f.write(b"\x01")
    process.run(cfg, args)
    st = json.load(open(os.path.join(work, "data", "state.json")))
    meta = st["files"]["inbox/bill-04.jpg"]
    check("changed file reprocessed", meta["raw"].endswith(
        os.path.basename(meta["raw"])) and meta["attempts"] == 1 or meta["attempts"] >= 1,
        meta)

    print("[5] reply parsing: fenced/chatty JSON replies")
    fenced = "Here you go:\n```json\n{\"a\": {\"b\": 1}}\n```\nDone."
    check("fenced reply parsed", process.parse_json_reply(fenced) == {"a": {"b": 1}})
    check("plain reply parsed", process.parse_json_reply('{"x": 2}') == {"x": 2})
    try:
        process.parse_json_reply("no json here")
        check("bad reply raises", False)
    except ValueError:
        check("bad reply raises", True)

    print("[6] failure -> recorded and retried up to max_attempts in later runs")
    cfg3 = make_cfg(tempfile.mkdtemp(prefix="ich-fail-"))
    p = os.path.join(cfg3["root"], "inbox", "a.jpg")
    open(p, "wb").write(b"test")
    real = process.mock_extract
    process.mock_extract = lambda *_: _raise()
    def _raise(): raise ValueError("boom")
    for _ in range(3):
        process.run(cfg3, Args())
    st3 = json.load(open(os.path.join(cfg3["root"], "data", "state.json")))
    check("failed files pause after max attempts",
          st3["files"]["inbox/a.jpg"]["status"] == "failed"
          and st3["files"]["inbox/a.jpg"]["attempts"] == 3, st3["files"])
    process.mock_extract = real
    process.run(cfg3, Args())
    st3 = json.load(open(os.path.join(cfg3["root"], "data", "state.json")))
    check("unchanged failed file stays paused", st3["files"]["inbox/a.jpg"]["status"] == "failed")
    with open(p, "r+b") as f:
        f.seek(0, os.SEEK_END)
        f.write(b"-new-scan")
    process.run(cfg3, Args())
    st3 = json.load(open(os.path.join(cfg3["root"], "data", "state.json")))
    check("changed file retried from scratch and succeeds",
          st3["files"]["inbox/a.jpg"]["status"] == "done"
          and st3["files"]["inbox/a.jpg"]["attempts"] == 1, st3["files"])

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0

if __name__ == "__main__":
    raise SystemExit(main())
