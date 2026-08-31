from __future__ import annotations

import argparse
from pathlib import Path

from orchestrator.platform import export_windows_roots


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    count = export_windows_roots(args.destination)
    print(f"Exported {count} public root certificates to {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
