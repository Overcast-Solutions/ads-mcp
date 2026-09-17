"""Fail closed when pip-audit finds advisories or cannot complete its scan."""

import json
from pathlib import Path
import subprocess
import sys


def main():
    packages = json.loads(Path(sys.argv[1]).read_text())
    output = Path(sys.argv[2])
    requirements = output.with_suffix(".requirements.txt")
    # The project is reviewed as source. pip/setuptools are
    # installer tools; all other packages in the clean runtime are scanned.
    runtime = sorted((p["name"], p["version"]) for p in packages
                     if p["name"].lower() not in {"ads-mcp", "pip", "setuptools"})
    if not runtime:
        raise RuntimeError("DEPENDENCY_SCAN_EMPTY: resolve runtime dependencies first")
    requirements.write_text("".join(f"{name}=={version}\n" for name, version in runtime))
    # Retain a machine-readable failure if tool execution never produces output.
    output.write_text(json.dumps({"status": "scan_incomplete", "clean": False}) + "\n")
    subprocess.run([sys.executable, "-m", "pip_audit", "--strict", "--disable-pip",
                    "--no-deps", "-r", str(requirements), "-f", "json",
                    "-o", str(output)], check=True, timeout=600)
    result = json.loads(output.read_text())
    scanned = result["dependencies"]
    normalize = lambda name: name.lower().replace("_", "-").replace(".", "-")
    expected = {(normalize(name), version) for name, version in runtime}
    actual = {(normalize(p["name"]), p["version"]) for p in scanned}
    if (actual != expected or any(p.get("skip_reason") or p.get("vulns") for p in scanned)):
        raise RuntimeError(
            "DEPENDENCY_SCAN_FAILED: incomplete coverage or advisories require maintainer review"
        )


if __name__ == "__main__":
    main()
