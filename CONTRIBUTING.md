# Contributing

Discuss substantial interface changes with the maintainer before implementation.
Use synthetic fixtures and reproducible steps; never contribute OAuth material,
real customer payloads, private workstation paths or local client integrations.
Contributions are under this project's Apache License 2.0; retain applicable third-party
notices and explain provenance for copied material.

Use a separate CPython 3.12–3.14 environment on Linux or macOS, install
`.[dev]`, and run `python -m pytest -q`. Check the generated tool documentation
with `python scripts/gen_tools_md.py --stdout` and declared workflow support
with `python scripts/check_capabilities.py`. Requirements belong in the
independently authored `tests/fixtures/capability_requirements.json`: explain
the operator purpose and review changes against intended workflows, rather
than deriving expectations from the current schema. The check covers declared
structure; retain executable safety and behavior tests. Run
`python scripts/parity.py` for this project's expected read outputs.
See [release checks](docs/releasing.md)
for minimum dependencies and installed archives. Do not contact real accounts
from tests or include credentials in issue/PR descriptions.

Explain the problem, resulting behavior, compatibility implications and executed
validation in a pull request. Maintain read-only defaults, truthful previews,
account binding and refusal behavior. A failing acceptance test needs a reviewed
contract decision, not removal or a platform skip to turn CI green. Report
vulnerabilities through [SECURITY.md](SECURITY.md). Maintainers review and merge;
publication and any deployment changes are separate operator decisions.

## Development checks

Create a virtual environment and install the development dependencies:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/python scripts/parity.py
.venv/bin/python scripts/check_capabilities.py
.venv/bin/python scripts/verify_apply_surface.py
.venv/bin/python scripts/gen_tools_md.py --stdout
```

The source archive includes fixtures, test helpers, documentation and these
scripts. The wheel provides the runtime and all three console entry points,
including `ads-mcp-export-source`. The [tool catalog](docs/tools.md) is generated
from the registry; run `python scripts/gen_tools_md.py` to update it, never edit
it by hand.

CI covers six current Linux/macOS interpreter cells and Linux 3.12 minimum
dependencies. Unexecuted hosted jobs remain missing release evidence, not
passing results. See [release checks and source export](docs/releasing.md)
and [integration and migration](docs/migration.md) for the remaining checks.
