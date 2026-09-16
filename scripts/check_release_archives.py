"""Build and install both archives separately; retain logs and resolved versions.

Run in a clean build-tool environment with `build` installed. The output must
be absent and outside the checkout. This never publishes or changes Git.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import venv
import zipfile


RETIRED_PATHS = ("tests/fixtures/incumbent_catalog.json",
                 "scripts/parity_catalog.py", "THIRD_PARTY_NOTICES.md")
REQUIRED_SOURCE = ("LICENSE", "SECURITY.md", "CONTRIBUTING.md",
                   "tests/fixtures/capability_requirements.json",
                   "tests/pmax_oracle.py", "docs/pmax.md",
                   "tests/test_pmax_lifecycle_contract.py",
                   "tests/test_pmax_signals_contract.py",
                   "tests/test_pmax_url_controls_contract.py",
                   "tests/test_pmax_product_selection_contract.py",
                   "tests/test_pmax_workflow_contract.py",
                   "tests/test_pmax_provider_boundary_contract.py",
                   "tests/fixtures/pmax_provider_fields_v25.json",
                   "tests/fixtures/pmax/get_asset_groups.json",
                   "tests/fixtures/pmax/get_asset_group_signals.json",
                   "tests/fixtures/pmax/list_audiences.json",
                   "tests/fixtures/pmax/get_pmax_url_settings.json",
                   "tests/search_url_oracle.py",
                   "tests/test_search_ad_urls_contract.py",
                   "tests/test_keyword_urls_contract.py",
                   "tests/test_search_url_workflow_contract.py",
                   "tests/test_search_url_oracle_controls.py",
                   "tests/fixtures/search_url_provider_fields_v25.json",
                   "tests/fixtures/search_urls/get_responsive_search_ad_urls.json",
                   "tests/fixtures/search_urls/get_keyword_urls.json",
                   "docs/search-urls.md")


def check_inventory(paths, *, source_archive=False):
    """Reject retired assets, including a notice under wheel license metadata."""
    paths = set(paths)
    for retired in RETIRED_PATHS:
        if any(path == retired or path.endswith("/" + retired) for path in paths):
            raise ValueError("release inventory contains a retired comparison asset")
    if source_archive and not set(REQUIRED_SOURCE) <= paths:
        raise ValueError("source inventory lacks required authored contract or release files")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dependencies", choices=("minimum", "current"), default="current")
    args = parser.parse_args()
    source = Path(__file__).resolve().parent.parent
    for retired in RETIRED_PATHS:
        if (source / retired).exists() or (source / retired).is_symlink():
            parser.error("remove retired comparison assets before building archives")
    for required in REQUIRED_SOURCE:
        if not (source / required).is_file():
            parser.error("source tree lacks the authored contract or required release files")
    output = args.output.resolve()
    if output.exists() or output == source or source in output.parents:
        parser.error("choose an absent output outside the checkout")
    output.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GOOGLE_ADS_", "ADS_MCP_", "PYTHONPATH", "PYTHONHOME"))}
    env["PYTHONNOUSERSITE"] = "1"

    def run(argv, name, cwd=output):
        with (output / (name + ".log")).open("wb") as log:
            subprocess.run([str(x) for x in argv], cwd=cwd, env=env,
                           stdout=log, stderr=subprocess.STDOUT, check=True)

    run([sys.executable, "-m", "build", "--outdir", output / "dist", source], "build")
    archives = sorted((output / "dist").iterdir())
    assert len(archives) == 2
    records = []
    for archive in archives:
        kind = "wheel" if archive.suffix == ".whl" else "sdist"
        if kind == "wheel":
            with zipfile.ZipFile(archive) as bundle:
                check_inventory(bundle.namelist())
        else:
            with tarfile.open(archive) as bundle:
                # The sdist has one project-version root directory.
                members = [member.name for member in bundle.getmembers()]
                roots = {name.split("/", 1)[0] for name in members}
                if len(roots) != 1:
                    raise ValueError("source archive must have one root")
                check_inventory([name.split("/", 1)[1] for name in members if "/" in name],
                                source_archive=True)
        environment = output / (kind + "-env")
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / "bin/python"
        constraints = (["-c", source / "requirements-min.txt"]
                       if args.dependencies == "minimum" else [])
        run([python, "-m", "pip", "install", *constraints, archive], kind + "-install")
        run([python, "-m", "pip", "check"], kind + "-dependencies")
        run([python, "-m", "pip", "freeze", "--all"], kind + "-resolved")
        run([python, "-I", source / "scripts/check_installed.py"], kind + "-installed")
        if kind == "sdist":
            extracted = output / "source"
            with tarfile.open(archive) as bundle:
                bundle.extractall(extracted, filter="data")
            roots = list(extracted.iterdir())
            assert len(roots) == 1
            snapshot = roots[0]
            for required in REQUIRED_SOURCE:
                assert (snapshot / required).is_file(), required
        records.append({"archive": archive.name,
                        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                        "installed_checks": "passed"})
    (output / "archive-evidence.json").write_text(json.dumps({
        "python": sys.version, "platform": sys.platform,
        "dependencies": args.dependencies, "archives": records,
        "scope": "offline installed archive checks; no hosted or provider acceptance",
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
