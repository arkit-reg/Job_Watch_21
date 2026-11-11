"""Combines this run's results with whatever is in the repository now, so two writers never collide.
Standard library only. Usage: python3 save_state.py <folder with this run's files>
 - state.json: merged (lists and saved jobs from both sides are kept, so nothing is emailed twice;
   for single values this run's value wins, except time stamps, where the later one wins)
 - jobs_log.csv: the repository's rows are kept as they are (including your Applied and Notes edits);
   this run's new rows are added
 - manual_check.csv: this run's fresh list"""
import csv
import json
import shutil
import sys
from pathlib import Path

LATER_WINS = {"last_run", "last_slot", "jsearch_done"}
# current-status parts: this run's copy replaces the repository's (entries can be removed, e.g. a site
# that works again leaves the check-by-hand list), so they must not be combined
THIS_RUN_WINS = {"manual", "failures", "manual_since", "counts", "manual_last_list", "manual_last_date"}


def merge(repo, run, key=""):
    if isinstance(repo, dict) and isinstance(run, dict):
        out = dict(repo)
        for k, v in run.items():
            out[k] = merge(repo[k], v, k) if k in repo else v
        return out
    if isinstance(repo, list) and isinstance(run, list):
        out = list(repo)
        seen = {json.dumps(x, sort_keys=True) for x in repo}
        out += [x for x in run if json.dumps(x, sort_keys=True) not in seen]
        return out
    if key in LATER_WINS and isinstance(repo, str) and isinstance(run, str):
        return max(repo, run)
    return run


def read_csv(path):
    if not path.exists():
        return [], []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def row_key(row):
    return (row.get("Company", ""), row.get("Title", ""), row.get("Link", ""), row.get("Date found", ""))


def main(run_dir: Path, repo_dir: Path = Path(".")) -> None:
    run_state, repo_state = run_dir / "state.json", repo_dir / "state.json"
    if run_state.exists():
        ours = json.loads(run_state.read_text(encoding="utf-8"))
        theirs = json.loads(repo_state.read_text(encoding="utf-8")) if repo_state.exists() else {}
        merged = merge(theirs, ours)
        merged.update({k: ours[k] for k in THIS_RUN_WINS if k in ours})
        repo_state.write_text(json.dumps(merged, indent=1, sort_keys=True), encoding="utf-8")

    run_log, repo_log = run_dir / "jobs_log.csv", repo_dir / "jobs_log.csv"
    if run_log.exists():
        run_fields, run_rows = read_csv(run_log)
        repo_fields, repo_rows = read_csv(repo_log)
        fields = repo_fields or run_fields
        fields += [f for f in run_fields if f not in fields]
        have = {row_key(r) for r in repo_rows}
        rows = repo_rows + [r for r in run_rows if row_key(r) not in have]
        with repo_log.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    if (run_dir / "manual_check.csv").exists():
        shutil.copy(run_dir / "manual_check.csv", repo_dir / "manual_check.csv")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
    print("Saved: this run's results combined with the repository's latest copy.")
