"""Command line entry point for offline qualification."""

import argparse
import json

from .runner import run_manifest, write_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an offline qualification manifest")
    parser.add_argument("manifest")
    parser.add_argument("--json-output", default="")
    parser.add_argument("--markdown-output", default="")
    args = parser.parse_args()
    report = run_manifest(args.manifest)
    write_report(report, args.json_output, args.markdown_output)
    if not args.json_output:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
