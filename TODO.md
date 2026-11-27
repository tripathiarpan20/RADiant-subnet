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
- [ ] R4/R6: treat Modal GPU capacity waits as infrastructure, never miner fault (L4 queued 167 s and longer during R2).
- [ ] Before launch: decide whether to swap the synthetic warmup set for licensed public X-rays.
