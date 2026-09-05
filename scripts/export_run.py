"""Export a completed synthetic batch into a portable, labeled evidence bundle."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from src.recovery import ledger as L


def export(run_id: str, output: Path, root=Path("data/console")):
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("Expected a batch run ID")
    base = root / run_id
    job = json.loads((base/"job.json").read_text(encoding="utf-8"))
    if job["status"] != "complete" or job.get("kind"):
        raise ValueError("Only completed synthetic runs can be bundled by this command")
    rows = L.read(base/"ledger.jsonl")
    opened = [e for e in rows if e["event"] == L.SEQUENCE_OPENED]
    paid = [e for e in rows if e["event"] == L.ATTEMPT_SUCCEEDED]
    summary = {"mode": "SIMULATED", "run_id": run_id, "config": job["config"],
               "generated_cases": job["total_cases"], "opened_cases": len(opened),
               "below_floor_cases": job.get("rejected"),
               "paid_cases": len({e["reference"] for e in paid}),
               "cohort_exposure_paise": sum(e["amount_paise"] for e in opened),
               "recovered_paise": sum(e["amount_paise"] for e in paid),
               "invariants": job["evidence"],
               "caveat": "Synthetic customer outcomes; this does not measure commercial uplift or production cash."}
    files = {name: (base/name).read_bytes() for name in
             ("job.json", "ledger.jsonl", "notifications.jsonl", "suppression.jsonl", "promises.jsonl")
             if (base/name).exists()}
    files["summary.json"] = json.dumps(summary, indent=2).encode()
    files["SHA256SUMS.txt"] = "\n".join(f"{hashlib.sha256(body).hexdigest()}  {name}" for name, body in files.items()).encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", ZIP_DEFLATED) as z:
        for name, body in files.items():
            z.writestr(name, body)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.run_id, args.output), indent=2))


if __name__ == "__main__":
    main()
