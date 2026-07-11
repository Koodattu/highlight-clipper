from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    arguments = parser.parse_args()
    request = json.loads(arguments.request.read_text(encoding="utf-8"))
    time.sleep(float(request.get("delay_seconds", 0)))
    output = Path(request["output_path"])
    partial = output.with_name(f"{output.name}.partial")
    partial.write_text(json.dumps({"echo": request.get("value")}), encoding="utf-8")
    partial.replace(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
