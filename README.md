# RADiant subnet

RADiant is a campaign-based Bittensor subnet for chest X-ray classifiers. Miners
submit zipped solutions that subclass a per-campaign base class; the owner
validator gates, builds and runs them in isolated offline Modal sandboxes on
studies fetched live from the DataHive API, and scores them on quality, speed
and cost in rounds. The round, crown and emission model follows Pareton (SN10);
the sandbox execution follows vidaio (SN85).

The repo is built ground-up in phases R0 to R8, following
[docs/implementation-plan.md](docs/implementation-plan.md). Each phase has a spec and
report in `docs/implementation/R<n>.md`, and `TODO.md` is the launch checklist.

## Status

- **R0 Feasibility**: complete (27 Nov 2026). Probes in `scripts/r0/`, report in
  [docs/implementation/R0.md](docs/implementation/R0.md).

## Development

```bash
uv sync --python 3.12 --extra r0 --extra dev
uv run pytest
```
