# Launch checklist

Every phase keeps this current. Anything deferred to launch goes here.

## Before mainnet launch

- [ ] Point `DATAHIVE_API_BASE` from DataHive's staging API to its production API.
- [ ] Make the production API domain the SDK's default `RADIANT_API_URL`.
- [ ] Seed testnet and mainnet campaigns with `RADIANT_FEE_RECIPIENT` set to the owner coldkey (the fixture's recipient is the public dev key Alice).
- [ ] Set `RADIANT_API_SITE` to the API domain and bind Caddy to 0.0.0.0:80/443 on the VPS.
- [ ] Re-pin the Chainguard MinIO digest (`docker-compose.yml`) to a current build before launch.

## DataHive checks (moved from R0 to R5)

- [ ] Run the DataHive reachability probe from the Hetzner validator host once staging exists (`python -m scripts.r0 datahive`), with the host IP on the key's allowlist.
- [ ] Run the DataHive fetch-time and rate-limit probe against staging, sized for 3 rounds of 100 studies a day.
- [ ] Re-run the DataHive probe against production before launch.

## Storage checks (moved from R0 to R3)

- [ ] Run `python -m scripts.r0 storage` with Hippius and Backblaze bucket credentials (`R0_<PROVIDER>_S3_*`), and pick the provider whose presigned PUT enforces both the checksum and `If-None-Match`.

## Hugging Face checks (moved from R0 to R4)

- [ ] Run `python -m scripts.r0 hf` with a private test repo, a fine-grained read token scoped to it, the pin org and its write token.
- [ ] Record the pin org's private storage quota.

## Carried from R1

- [ ] R3: put MinIO's S3 API behind Caddy on its own domain so miners can PUT to presigned URLs, and give the API a scoped MinIO user instead of the root credentials.
- [ ] R3/R4: per-service Postgres roles (API read-mostly, watcher and worker scoped) instead of the owner role.

## Carried from R2

- [ ] R3: `radiant-sdk submit` presign, PUT, fee payment and commitment (it stops after the receipt check today), and `status` against the intake API.
- [ ] R4: the validator's g7 build uses a killable build worker (as vidaio does) instead of the SDK's thread deadline, and g8 times load and evaluation from outside the sandbox.
- [ ] R4/R6: a sandbox that never starts (no GPU within `SANDBOX_GPU_WAIT_S`, default 900 s) is `infra_failed`, requeued once as in Pareton, never scored or rejected; a leader infra failure voids the round. Reuse the harness start marker and `GPU_UNAVAILABLE` from R2. (L4 queued 167 s and over 15 min during R2.)
- [ ] Before launch: decide whether to swap the synthetic warmup set for licensed public X-rays.

## Found in the 27 Nov review of R0 to R2

- [x] Safetensors reference weights published at `tripathiarpan20/densenet121-res224-all-safetensors@66fd21c9` and pinned in the reference miner. Move them to a RADiant org if one is created.
- [ ] R3: a public API domain behind Caddy for miners on other machines.
- [ ] R3: point the app's `RADIANT_S3_*` at whichever store the storage probe passes (Hippius or MinIO); the R0 probe uses `R0_HIPPIUS_S3_*`.
- [ ] R7: `radiant-sdk prepare --from <hash>` to fork a revealed solution.
- [ ] R8: nightly Postgres backups off the VPS (`pg_dump` to the bucket) and a restore drill in the runbook; the database holds fee history, leader history and round evidence.
- [ ] R8: a validator operator guide (host, .env, Modal, HF pin org, DataHive, wallets), alongside docs/miners.md.
- [ ] Push the repo and confirm `.github/workflows/ci.yml` runs green (not yet run on GitHub).

## Carried from R3

- [ ] Run `scripts/r3_exit_gate.sh` on testnet (needs the `bruh_dev_coldkey` password).
- [x] Hippius storage probe passed on 27 Nov (checksum and If-None-Match enforced).
- [x] The app's `RADIANT_S3_*` in the local `.env` point at the Hippius bucket.
- [ ] Add the mainnet owner coldkey to `TRUSTED_FEE_RECIPIENTS["finney"]` in `radiant_sdk/submit.py` before launch, and set it as `submission_fee.recipient` in each mainnet campaign manifest.
