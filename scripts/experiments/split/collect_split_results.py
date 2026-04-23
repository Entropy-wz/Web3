from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
COLLECT = ROOT / "scripts" / "experiments" / "collect_paper_results.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect split experiment outputs into final paper tables.")
    parser.add_argument("--runs-root", type=str, default="artifacts/paper_runs_split")
    parser.add_argument("--paper-data", type=str, default="paper/data/split")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cmd = [
        sys.executable,
        str(COLLECT),
        "--runs-root",
        str(Path(args.runs_root).resolve()),
        "--paper-data",
        str(Path(args.paper_data).resolve()),
    ]
    print("[RUN]", " ".join(cmd))
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    print("[DONE] split results collected.")


if __name__ == "__main__":
    main()
