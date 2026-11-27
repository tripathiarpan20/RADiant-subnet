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
- **R1 Foundations**: complete (27 Nov 2026). Campaign manifests, Postgres schema,
  seed CLI and the docker-compose stack; report in
  [docs/implementation/R1.md](docs/implementation/R1.md).
- **R2 SDK and reference miner**: complete (27 Nov 2026). `radiant-sdk`
  prepare, validate (offline Modal self-test and receipt) and a receipt-gated
  submit; DenseNet-121 reference miner; report in
  [docs/implementation/R2.md](docs/implementation/R2.md).

## Running the validator stack

```bash
cp .env.example .env    # set the passwords
docker compose up -d --build
curl -s 'localhost:8080/v1/campaigns?include_drafts=1'
```

## Miner SDK

```bash
pip install '.[sdk]'          # or: uv sync --extra sdk
modal setup                   # once: log in to your Modal account; validate runs one short sandbox there
cp -r radiant_sdk/reference_miner my-miner && cp my-miner/.env.template my-miner/.env
radiant-sdk prepare my-miner --out build
radiant-sdk validate build/solution.zip --campaign testnet-cxr-01 --api-url http://localhost:8080
```

## Development

```bash
uv sync --python 3.12 --extra validator --extra sdk --extra r0 --extra dev
uv run pytest                 # database tests start a throwaway Postgres container
scripts/r1_exit_gate.sh       # fresh stack, seed, rerun, restart, compare hashes
scripts/r2_exit_gate.sh --api-url http://localhost:8080   # reference miner validate (Modal) and contract codes
```
