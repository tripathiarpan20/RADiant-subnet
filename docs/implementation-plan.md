# RADiant subnet: vidaio competition-mode summary and ground-up build plan

Nov 25, 2026 · Arpan Tripathi. Copied into the repo on 27 Nov 2026 from the planning doc; the repo copy is the one the code tracks.

## Implemented so far

Updated as each phase lands. Status as of 27 Nov 2026 (R0 to R3).

| Phase | State | Where | Notes |
| --- | --- | --- | --- |
| R0 Feasibility | Complete (27 Nov) | [`docs/implementation/R0.md`](implementation/R0.md), `scripts/r0/`, `tests/r0/` | Passed: Modal offline sandbox on L4 and L40S, Modal timeout enforcement, base-model licences, DenseNet-121 scored on the 20 demo images. Moved, with probes ready: storage checks to R3, Hugging Face checks to R4, DataHive checks to R5. |
| R1 Foundations | Complete (27 Nov) | [`docs/implementation/R1.md`](implementation/R1.md), `config.py`, `campaign/`, `db/`, `storage/`, `api/`, `docker-compose.yml` | Exit gate passed: a fresh `docker compose up` seeds `testnet-cxr-01` and its hash is stable across reruns, restarts, the API and a recompute from storage. Stack: Postgres 17, MinIO (Chainguard build, since official images are gone), init job, API, Caddy, Adminer with a read-only role. |
| R2 SDK and reference miner | Complete (27 Nov) | [`docs/implementation/R2.md`](implementation/R2.md), `radiant_sdk/`, `gate/contract.py` | Exit gate passed: the DenseNet reference miner passes `radiant-sdk validate` (static checks, HF weights, Modal build, offline L4 qualification, receipt), and a miner missing `map_to_labels` gets `CONTRACT_MISSING_OVERRIDE` from both the SDK and the validator gate. `submit` refuses without a fresh receipt; upload and payment are R3. Since then: safetensors-only weights pinned, `radiant-sdk lock`, Modal setup checks, `docs/miners.md`, testnet 292 defaults. |
| R3 Chain intake | Built (27 Nov); testnet gate pending | [`docs/implementation/R3.md`](implementation/R3.md), `chain/`, `storage/s3.py`, `storage/upload_auth.py`, `worker/watcher.py`, `radiant_sdk/submit.py` | Signed upload API with presigned PUT, the `r1` commitment format shared by the SDK and watcher, Pareton's fee-proof checks, payment-reuse and locator-ownership checks, first-seen dedupe, and the watcher service. 178 tests pass. The testnet run of `scripts/r3_exit_gate.sh` waits on the safetensors reference weights and a coldkey-password payment. |
| R4 to R8 | Not started | | |

Findings from R0 that change later phases are listed in R0.md. The two that matter most: the base DenseNet has no tuberculosis or silicosis output, so R2's reference miner needs a fine-tuned head, and Modal L4 capacity can be refused or queue for minutes, so R6 must treat that as infrastructure failure, not the miner's.

## TL;DR

RADiant is a campaign-only subnet that runs Pareton's (SN10) round-based campaigns, with vidaio's (SN85) isolated Modal execution underneath. Revised on the evening of 25 Nov with your answers to all twelve open decisions.

- **Submitting.** A miner submits a zipped solution:
  - The code subclasses a per-campaign classifier base class.
  - A `.env` file points to a pinned Hugging Face model.
  - The miner uploads the zip to a presigned PUT URL, pays the campaign fee and commits the hash on-chain.
- **Checking.** A watcher picks up the commitment and verifies the fee. A worker then runs these gates in order:
  - integrity and archive safety
  - required method overrides
  - obfuscation
  - the `.env` contents and Hugging Face access
  - duplicate check, build, a timed model-load check on the GPU the miner chose and, last, pinning the weights
- **Running rounds.** A round starts when 5 qualified submissions are queued, with at most 3 rounds in any 24 hours. The current leader and the challengers each run in their own offline sandbox; there is no separate baseline run. All of them run on the same studies, fetched on the fly from the DataHive API for that round, and are scored on quality (per-label AUROC through a diminishing-returns curve), speed and cost.
- **Paying out.** A challenger who beats the leader by a margin takes the crown. The crown's emission share decays over blocks, from Pareton's default 0.10 to 0.02, and the remainder is burned. After a delay, which is shorter when the round's leader is weak, every evaluated solution is revealed, so later submissions can start from the leader's.
- **Auditing.** Other validators first copy the owner's weights from the public API, as in Pareton. Recomputing from published labels and predictions comes in the last phase. Images are never shared.

The submission pipeline, round engine and emission maths come from SN10. The sandbox isolation, GPU choice, timeouts, cost accounting and SDK self-test come from SN85. The next three sections record your decisions, show the pivots and explain how SN10 works. The older SN85 summary follows for reference.

## Decisions recorded on 25 Nov

These are your answers from 25 Nov, including the evening review of the twelve open decisions, plus the rows marked 27 Nov from the R0 review. The right column says how each changes the build.

| Area | Your decision | What it means for the build |
| --- | --- | --- |
| Model family | Image classifiers (DenseNet, ViT, YOLO-style), not VLMs over 1B parameters. Base miner from [torchxrayvision densenet121-res224-all](https://huggingface.co/torchxrayvision/densenet121-res224-all), [rad-dino](https://huggingface.co/microsoft/rad-dino) or [cxr-foundation](https://huggingface.co/google/cxr-foundation). | Images stay small, so the image cap can drop to about 10 GB and cheap GPUs (L4) are viable. Output is a label-to-probability map, not a findings JSON. |
| Solution contract | Miners subclass an abstract class and override `load_model`, `load_model_checkpoint`, `run_single_evaluation`, `run_batched_evaluation`, `map_to_labels` and others. Each campaign manifest names the class and its labels. | A versioned `radiant_sdk` package holds the base classes. A validator-owned harness imports the miner class inside the sandbox, replacing vidaio's `/compress` HTTP route. |
| Who scores | The owner validator scores. Other validators audit the classification results it publishes. | One scoring stack. Other validators copy `/v1/weights` in the early phases, as in Pareton. Recomputing from published evidence comes in the last phase (R8). |
| Execution | Isolated Modal sandbox on a GPU or CPU the miner picks from the campaign allowlist, with a model-load timeout. | vidaio's sandbox supervisor, isolation probe and observed-hardware costing carry over almost unchanged. |
| Reward | F1 or quality, speed and cost, with weights set per campaign before launch. | A composite score with three campaign-set weights. Quality is a per-label metric (AUROC by default) mapped through a knee curve and averaged so every label counts equally. Speed and cost are relative within a round. |
| Submission channel | Zip upload to a presigned PUT on Hippius or Backblaze, a fee paid on-chain, and a public on-chain commitment, as in SN10. The `.env` file holds the HF repo and token. | vidaio's axon invitation and PAT polling are dropped. Pareton's upload auth, commitment format, fee order, fee verification and watcher are adopted: the presign needs no payment, and the watcher checks the fee. |
| Gates | Required methods overridden, HF model reachable with the `.env` token, obfuscation checks. | Same fail-fast gate pipeline as Pareton, with radiology-specific gates. |
| Competition shape | Rounds of `ROUND_SIZE` (default 5) submissions, decaying emissions per campaign, solution reveal via `patch_visibility`. This takes priority over SN85's single-phase competition. | SN85's lifecycle state machine and podium are dropped. Pareton's round, leader and emission-rule model is adopted. |
| Public API | An API like `api.pareton.ai` for audit data, submission status and campaigns. | A FastAPI service modelled on `pareton-api`. |
| Miner SDK | Comprehensive, in the style of the [vidaio competition SDK](https://github.com/vidAio-subnet/vidaio-subnet/tree/main/miner#competition-sdk-prepare-validate-and-publish-a-solution). | `prepare`, `validate` (Modal self-test with a receipt) and `submit` (presign, PUT, fee, commit). |
| Validator deployment | docker-compose with the official Postgres image. Adminer to view state. No frontend yet. | One compose file. Adminer is bound to localhost, reached over SSH, and logs in with a read-only Postgres role. |
| Storage | Hippius or Backblaze instead of AWS S3. | Pareton's S3 client already takes an endpoint URL; R0 checks presigned PUT support. The bucket holds solution zips only, because weights stay on Hugging Face. |
| Data protection | Liability sits with you and Wade. DataHive rate-limits the key. Modal compliance is out of scope for now. | No implementation work now. The sandbox design still keeps images from ever leaving it. |
| Evaluation data | Not sealed. Fetched on the fly from the DataHive API, with base URL and key in the validator .env. Development and testnet use DataHive's staging API, and launch uses production. Both follow the [DataHive API spec](https://claude.ai/artifact/MKJdB8oLbVgTVQyZkEh8N8). | Each round lists, selects and downloads its own studies, then deletes the images when it settles. Only the study IDs, labels and hashes are kept for audit. Rate limits and fetch time set how large a round can be. |
| Spam control | Submission fee only. No alpha-stake gate, not even as a per-campaign option. | Gate g0 checks registration, campaign status and disqualification only. The manifest has no stake field, and SN85's stake-check code is not ported. |
| Reveal | Every evaluated submission, as Pareton does. | public\_after\_reveal with Pareton's delay. The zip is published without .env, and the submission's pinned HF repo is made public. |
| Emissions | Pareton's defaults: start 0.10, floor 0.02, 201,600 blocks, rest burned. The first campaign pays minimal emissions to test the flywheel. | Pareton's linear\_decay rule, unchanged. |
| Weight pinning | The owner validator sets HF write credentials in its .env and copies each miner's HF repo into a private repo it owns. Names record campaign ID, hotkey and submission ID. Nothing is mirrored into the bucket. | Gate g9, the last gate, makes the copy, so only submissions that pass every gate are pinned. The miner's token is used only at g5, for one download that also feeds the build. See Weight pinning. |
| Round cadence | Re-run the leader every round, since each round draws new studies. Cap rounds per 24 hours in the validator .env, default 3, to stay inside DataHive's rate limit. | MAX\_ROUNDS\_PER\_24H counts across all campaigns, because they share one DataHive key. At defaults (100 studies a round) that is at most 300 studies a day. |
| Baseline | The base miner model is a reference for miners, not run every round like SN10's baseline. A functional baseline is still needed for the first testnet campaign. | Rounds have no baseline runs. A seed leader is optional per campaign and may score low; testnet campaign 1 uses the reference DenseNet solution. |
| Quality metric | AUROC (or another chosen metric) mapped to quality with fast gains up to 0.8 and diminishing returns after. Every label is weighed fairly despite class imbalance. | See Scoring. The curve and its test values go into the R6 implementation doc. |
| Logs shown to miners | Build logs only, as in SN10: image build and model loading, ending before any evaluation data is read. This must hold even if a miner overrides class methods. | Shown logs come only from sandboxes that never have evaluation data mounted. See Keeping evaluation data in. |
| Audit | Pareton's /v1/weights route in the early phases, with auditing in the last phase. Other validators and miners get true labels, never images. | The evidence route and the recomputing auditor move to R8. |
| Validator host | A bare-metal VPS, likely Hetzner, with open internet access. | R0 confirms DataHive's API answers from that host, with its IP on the key's allowlist. |
| Harness input | Raw uint16; miners own preprocessing. | Resize time counts toward speed. |
| Fast reveal | If the round leader scores below a threshold set in the manifest, that round's reveal period is shorter, so miners can iterate in later rounds. Reveals publish the cloned HF repo and the code zip, with the miner's .env stripped. (26 Nov) | fast\_reveal\_quality\_threshold and fast\_reveal\_delay\_s in the manifest, judged on the leader's quality Q. A code\_sha256 in the commitment keeps stripped zips checkable against the chain. |
| Base miner (27 Nov) | DenseNet-121 (`densenet121-res224-all`) only; rad-dino and cxr-foundation are dropped. For labels the base model has no output for (tuberculosis, silicosis), its `map_to_labels` returns 0. | R2's reference miner wraps DenseNet and maps its 18 outputs to the campaign labels, with a constant 0 for unmapped labels. It scores at chance on those labels, so it is a working starter, not a strong seed. |
| Model load timing (27 Nov) | Startup cost inside `load_model` (CUDA context, weight load, warm-up) is the miner's to optimise. | The harness clock starts when the harness begins importing the miner's module (so heavy import-time work can't dodge it) and stops when `load_model` returns. It never starts at `Sandbox.create`, so container start, image pull and GPU queueing don't count against the miner. `model_load_timeout` stays generous (120 s default). |
| GPU capacity in rounds (27 Nov) | A sandbox that gets no GPU within the wait cap is an infrastructure failure, like Pareton's `infra_failed`, and the entry is requeued for a later round. | Everything before the harness's start marker (GPU queueing, image pull, Modal API errors) is infrastructure: the entry becomes `infra_failed` with `GPU_UNAVAILABLE` or `SANDBOX_START_FAILED`, is never scored, and is requeued once, as in Pareton. A second infra failure is terminal and needs an operator requeue. Everything after the marker (crash, timeout, bad output) stays the miner's fault, as in vidaio. A leader infra failure voids the round and requeues its challengers without using their retry. The wait cap is `SANDBOX_GPU_WAIT_S` in the validator `.env` (default 900 s), and the same rule covers g8 qualification. |
| Weight format (27 Nov) | Weights must be safetensors; pickle-based checkpoints are refused. The reference weights are republished as safetensors under the RADiant HF org. | `gates.weights_formats` (only `safetensors` for now) is pinned in the manifest. g5 refuses any repo file that is neither a safetensors weight nor small text (config, card) with `HF_WEIGHTS_FORMAT`. The SDK also refuses an `HF_TOKEN` with write scope, since the token travels in the zip. `scripts/publish_reference_weights.py` converts torchxrayvision's weights and checks the outputs are identical before publishing. |
| Testnet (27 Nov) | Testnet is netuid 292, and fees there are testTAO. | `RADIANT_NETWORK=test` defaults `RADIANT_NETUID` to 292, and `/v1/info` reports network, netuid and fee currency. In R3 the SDK pays on the network in `miner.env` (`--network test` for btcli) and refuses to pay if `/v1/info` disagrees. |
| Local storage (27 Nov) | A local MinIO (official image) on the validator VPS is an alternative to Hippius and Backblaze for solution zips. | R1 adds MinIO to docker-compose. The storage layer stays endpoint-URL based, so MinIO, Hippius and Backblaze are interchangeable through `.env`. R3 runs the storage probe against whichever is chosen. |

## Where RADiant follows SN10 and where it follows SN85

SN10 sets how work enters, competes and is paid. SN85 sets how untrusted code runs and how miners test before submitting. Anything not listed is new for RADiant.

### Pivoted to SN10 (Pareton)

| Area | What we adopt | RADiant change |
| --- | --- | --- |
| Campaign model | Campaigns in Postgres with draft, open, closed and archived status. Several can be open at once. `manifest_hash` covers the pinned terms; fee and visibility sit outside the hash. | The pins are radiology-specific: labels, base-class version, data pool, scoring weights and timeouts such as model\_load\_timeout. |
| Upload auth | Miner signs `{campaign, hotkey, hash, upload_id, expiry, network, netuid}` with the hotkey. `POST /v1/uploads/*` checks the signature and returns a presigned PUT with checksum and `If-None-Match`. | The object is a solution zip, not a git diff. Bucket is Hippius or Backblaze. |
| Submission fee | Coldkey transfer to a recipient pinned in the miner CLI. The proof is `(block, extrinsic index)` in the commitment. Append-only fee history, checked at the payment's block. Each proof is single use. Dev hotkeys can be exempt. | None. |
| On-chain commitment | A positional pipe format within the 384-byte plaintext cap: version, campaign, hash, locator, payment ref. Latest commitment per hotkey wins. | Drop `baseline_commit`. Add a solution-format version. |
| Watcher | Scans commitments every cycle, verifies the fee and locator ownership, and dedupes on first-seen hash in chain order. Also creates and reaps rounds. | None. |
| Gate pipeline | Fail-fast gates with a persisted state per step (`committed`, `fetched`, `verified`, ... `scored`), events and reason codes. Deterministic obfuscation scan. Optional LLM review. | Gate contents are RADiant-specific; see Architecture. |
| Rounds | Fire at `ROUND_SIZE` queued (default 5) or after `ROUND_MAX_WAIT_S` (6 h). Each round runs baseline, leader and challengers on one seeded draw. Scores compare inside a round only. | Nothing is sealed in advance. Each round fetches its studies on the fly from the DataHive API (base URL and key in the validator .env), selected with a seed from a finalised block hash. Rounds run the leader and challengers only, with no baseline runs, and at most `MAX_ROUNDS_PER_24H` (default 3) start in any 24 hours. |
| Crown and ranking | A challenger takes the crown only above `leader x (1 + epsilon)`, epsilon 0.01. Rounds are void on baseline failure, leader infra failure or no surviving challenger. | The threshold applies to the weighted final score (quality, speed, cost), as in vidaio. No separate quality gate; campaigns give quality the largest weight when it matters most. |
| Emissions | One leader per campaign is paid. `emission_rule` decays linearly from `start_weight` to `floor_weight` over `decay_blocks` since the crown was won. The residual goes to the burn UID. The sum of open campaigns' start weights is at most 1. | None. Pareton's defaults are kept for the first campaign. |
| Reveal | `patch_visibility`: `private`, or `public_after_reveal` with a delay that starts at the first finalised round. Publication is lazy and checksum-verified. | Every evaluated submission is revealed, as in Pareton. Strip `.env` from the zip and make the submission's pinned HF repo public. A shorter delay applies when the round leader's quality is below a manifest threshold. |
| Public API | FastAPI behind Caddy, read-mostly, `no-store` on live data. Covers campaigns, submissions, leader, rounds, entry reports, weights, presign and downloads. | Per-item round evidence (opaque item IDs, true labels and rounded predictions, never images) arrives in R8 with the recomputing auditor. |
| Process split | Separate api, watcher, worker, round-worker and weights services with least-privilege secrets. | Run as docker-compose services, not systemd units. |
| Auditor | scripts/auditor.py fetches GET /v1/weights and sets it unchanged every 360 blocks. | Adopted as is until R8, which adds recomputing from evidence. |

### Pivoted to SN85 (vidaio)

| Area | What we adopt | RADiant change |
| --- | --- | --- |
| Execution boundary | The validator builds the image and creates the Modal Sandbox. `block_network=True`, no secrets, read-only inputs, active isolation probe, termination on timeout. Contender code is never imported in the validator. | One fresh sandbox per round entry, not per batch. There is no output Volume; predictions return as small JSON. Nothing from a round sandbox is shown to miners; they see only the build and qualification logs. |
| Miner-chosen hardware | GPU and CPU picked from the manifest allowlist through the solution descriptor. Cost uses the GPU actually observed in the sandbox at locked Modal rates. | Adds a timed model-load phase, set per campaign with `model_load_timeout`. |
| Timeouts | Deadlines computed from the workload, with a manifest minimum. | Computed from image count and pixels. |
| Static validation | Layout, path escape, opaque binaries, secrets and obfuscation checks with stable reason codes. | Applied to zip contents. Adds AST checks for the base class and overrides. |
| Miner SDK | `prepare`, `validate` in a validator-shaped Modal sandbox, a 24 h receipt, then publish. | `publish` becomes `submit`: presign, PUT, fee, commit. |
| Failure handling | A sandbox error is the miner's fault. Failed items count as wrong. No automatic replay. | The same rules, applied inside a round. |

### Dropped from both

- SN85 lifecycle state machine, single competition window, human tie-break review, boss contender and 70/20/10 podium. The SN10 crown and decay replace them.
- SN85 axon invitation and submission protocols, private GitHub repos and PATs.
- SN10 git patches against a pinned engine, hermetic GHCR builds, rented GPU pods (Lium, Shadeform) and the LLM workload sampler.
- SN10's per-round baseline runs and baseline drift ceiling. The base miner is only a reference for miners, and each campaign starts from a seed leader instead. That seed is optional and may be a low-scoring baseline.
- SN85's alpha-stake entry gate, including as a per-campaign option. The submission fee is the only spam control.

## How Pareton (SN10) works

This section covers the four parts you asked about, read from [Pareton-ai/pareton](https://github.com/Pareton-ai/pareton) at commit 9745a76. Pareton miners submit git patches against a pinned vLLM or SGLang baseline, and each round benchmarks them on a rented GPU pod.

### Submission: hotkey-signed upload, fee, then a public commitment

One correction to the flow as you described it: Pareton does not make miners pay before they get the PUT URL. The CLI (`miner/commit_patch.py`) runs these steps in order:

1. **Read the campaign** from `GET /v1/campaigns/{id}`, including `submission_fee {amount_tao, recipient}`. The CLI refuses to continue if the recipient differs from `TRUSTED_PAYMENT_RECIPIENT`, which is pinned in `campaign/fees.py`. It asks y/N before paying, or takes `--yes` and `--max-fee-tao`.
2. **Sign an upload request** locally with the hotkey. The message is `pareton.patch-upload.v1` plus canonical JSON of `{campaign_id, hotkey, patch_hash, upload_id (UUIDv4), expires_at, network, netuid}` (`storage/upload_auth.py`).
3. **Get a presigned PUT** from `POST /v1/uploads/patch`. The API checks the signature against the public hotkey, the expiry window and the subnet. It checks the campaign is open and the hotkey is not disqualified. It then presigns a PUT for `<prefix>/private/campaigns/<campaign>/patches/<hotkey>/<upload_id>.diff`, requiring `x-amz-checksum-sha256` and `If-None-Match: *`. A retry with the same UUID returns `already_uploaded` once the stored checksum matches. Nothing here checks the fee.
4. **PUT the bytes** to the bucket. The object is private; the URL identifies it but grants no read access.
5. **Pay the fee** as a normal `Balances.transfer` from the coldkey, keeping `(block, extrinsic_index)`. `--payment-block` and `--payment-tx` reuse an earlier payment after a failed commit.
6. **Commit on-chain** with `Commitments.set_commitment`, as `v2|campaign_id|baseline_commit|sha256:…|retrieval_url|pay_block|pay_tx`. It must fit 3 Raw fields of 128 bytes, so 384 bytes in total. An experimental timelock mode exists but has not revealed on Finney.

The **watcher** (`chain/watcher.py`) then ingests each latest commitment in chain order. It skips the commitment unless all of these hold:

- the campaign is open and the commitment is after the competition start
- the locator is a private object in this campaign's prefix
- the hotkey in the locator path matches the committing hotkey
- the fee proof is valid (`chain/payment.py`): payment block at or before the commit block; extrinsic is a `Balances.transfer_keep_alive` or `transfer_allow_death` to the recipient; it dispatched successfully; the amount is at least the fee in force at the payment block; the signer is the hotkey or its owning coldkey; the reference has not been used before
- the hash has not been seen before in that campaign, first seen in chain order

### Gates

`worker/pipeline.py` runs gates fail-fast and writes a `submission_events` row and a state for each step:

| Gate | Check | State on pass |
| --- | --- | --- |
| a identity | Hotkey registered, campaign open, `baseline_commit` matches the pin | `picked_up` |
| b integrity | Fetch with S3 credentials, size cap, sha256 equals the committed hash, locator ownership | `fetched`, `verified` |
| c base apply | `git apply --check` against the pinned baseline, no fuzz | `applied` |
| d surface | Allowed and denied path globs; rejects symlinks, binary patches, path traversal, submodule changes, renames and unparseable headers | `surface_ok` |
| d1 obfuscation (optional) | Deterministic regex and token scan of added lines only: encoded blobs and payloads, text aimed at reviewers, dynamic code, reflection with non-literal names, environment reads of precision flags, non-ASCII code. Rule set pinned in the manifest. | continues |
| d2 LLM review (optional) | One tool-free call on the comment-stripped diff, with a JSON verdict cached per hash, policy and model. Reject-only. Provider outages are retried, never skipped. | continues |
| e hermetic build | Apply in the pinned base image, then push a content-addressed image | `building`, `image_pushed`, `built` |
| dedupe | Byte-identical images in one cohort: the earliest commit keeps the slot | `rejected_duplicate` |

Terminal states are `scored`, `disqualified`, `rejected` and `rejected_duplicate`. `infra_failed` gets one requeue.

### Rounds, the crown and decaying emissions

- **Trigger** (`round/create.py`): a round fires when `ROUND_SIZE` submissions are queued (default 5) or the oldest has waited `ROUND_MAX_WAIT_S` (21,600 s). One live round per campaign. A round whose heartbeat goes stale for 1,800 s is voided.
- **Draw**: the seed is a hash of a finalised block hash and the campaign ID, so the draw is reproducible and cannot be chosen.
- **Run**: one pod runs the baseline twice, then the current leader and every challenger on the same draw. If the two baseline runs differ by more than the drift ceiling (0.05), the round is void.
- **Rank** (`round/rank.py`): a challenger must beat the leader's in-round score times (1 + `OVERTAKE_EPSILON`), epsilon 0.01. The leader must stay above the baseline to keep the crown. Rounds are void on baseline failure, leader infra failure or no surviving challenger. `leader_history` records each seat, overtake and vacancy.
- **Emissions** (`weights/build.py`): each open campaign pays its single leader `emission_rule(blocks_held)`. The only rule today is `linear_decay`, with defaults start 0.10, floor 0.02 and 201,600 blocks, about two weeks. The clock is the seed block of the round where the crown was won, so a successful defence does not reset it. A vacant, closed or deregistered crown pays nobody. The residual goes to `BURN_UID`, and a database trigger keeps the sum of open start weights at or below 1. The rule is signed into `manifest_hash`.
- **Weights**: `pareton-weights` writes `weight_sets` with a per-campaign breakdown. Other validators run `scripts/auditor.py`, which fetches `GET /v1/weights` and submits it unchanged every 360 blocks. It copies without verifying.

### Reveal (`patch_visibility`)

- **Modes**: `private` (the default) or `public_after_reveal` with `reveal_delay_s` (default 172,800 s). The policy sits outside `manifest_hash`, so changing it does not break signoffs.
- **Clock**: it starts at the first complete round where the entry was scored or disqualified. Void rounds, build failures and bans do not start it.
- **Who is revealed**: every evaluated submission, not just winners.
- **How**: publication is lazy. An availability or download request copies the object, after checksum verification, into a public prefix and redirects to it. List and detail reads never touch S3.
- **Permanence**: switching back to private cannot revoke copies already published.

### `pareton-api` and the other services

- **Server**: `python -m api` runs uvicorn on `127.0.0.1:8000` behind Caddy, which terminates TLS. It is a FastAPI app over Postgres (Neon).
- **systemd sandboxing**: `DynamicUser`, `ProtectSystem=strict`, no capabilities. `UnsetEnvironment` strips every credential the API does not need (GPU, registry, HF and alerting tokens), so it keeps only the DB and S3 credentials.
- **Endpoints**: campaigns (list and detail), submissions (list, detail, build log, patch availability, patch download), leader, rounds, round detail, entry report, score progress, stats, `GET /v1/weights`, `POST /v1/uploads/patch` and `/health`.
- **Caching**: settled resources cache for 30 s. Anything live uses `no-store`.
- **Other units**: `pareton-watcher` (chain scan and round creation), `pareton-worker` (gates and build), `pareton-round-worker` (rounds on rented pods), `pareton-weights` (vector and `set_weights`), and a deploy timer that drains work before restarting.
- **For RADiant**: the same split maps one to one onto docker-compose services. Each service gets its own env file instead of `UnsetEnvironment`, and the API container mounts no Modal or HF credentials.

## How vidaio (SN85) competition mode works

*Kept for reference from the first draft. RADiant adopts only the SN85 parts listed in the pivot section above.*

Competition mode is a scheduled benchmark that one validator operates end to end: it invites miners, collects private repos, freezes and archives them, builds each one once, runs it offline on a private dataset, scores every item outside the sandbox, and pays a podium. Source: [competition\_mode\_implementation\_plan.md](https://github.com/vidAio-subnet/vidaio-subnet/blob/main/docs/competition_mode_implementation_plan.md) and `docs/competitions.md` on [feature/compression-competitions](https://github.com/vidAio-subnet/vidaio-subnet/tree/feature/compression-competitions).

### Lifecycle

A persisted state machine in SQLite drives everything, advanced by short restart-safe scheduler ticks under a database lease. Only one competition can be active at a time, and any state before completion can move to FAILED or CANCELLED.

| State | What happens |
| --- | --- |
| SCHEDULED | Manifest validated, normalised, digest stored. Dataset already sealed. |
| ENROLLING | Invitations go to every serving miner. Opted-in miners are polled every `contender_ping_interval` for a private repo URL and a read-only PAT. Each new commit is cloned, pinned, statically checked, and the result is returned on the next poll so the miner can fix it. |
| FINALIZING\_SUBMISSIONS | Revisions freeze. Alpha stake is re-checked against a fresh metagraph. Every pinned repo plus an inventory is archived to a private S3 bucket, and nothing continues until the upload is verified. |
| VALIDATING | Layout, route contract, secrets, obfuscation and opaque-binary checks. Ambiguous readability goes to REVIEW\_REQUIRED for a human. |
| BUILDING | Each accepted repo is built once into an immutable Modal image (25 GB cap, build timeout). Builds run in parallel up to `max_parallel_contenders`. |
| EVALUATING | For every canonical batch of up to 5 items, a fresh network-blocked sandbox mounts only that batch read-only and a private output dir read-write, gets probed for isolation, is called over localhost, then terminated. Outputs are validated and scored by a separate trusted worker. |
| SCORING | Aggregates are rebuilt from per-item history rows, never from cached totals. |
| AWAITING\_END\_TIME | Provisional ranking is visible. Operators can disqualify or order an exact tie before the review deadline, each with identity, reason and an integrity hash. |
| COMPLETED | Ranks, winner and podium are written in one transaction and become the new incumbent for weights. |

### The pieces

- **Manifest** (`competitions/manifests/*.json`, Pydantic `CompetitionManifest`): schedule, allowed GPUs and CPU cap, image size cap, batch size and timeouts, media thresholds, scoring factors, optional boss repo, alpha-stake minimum, dataset Volume names. Edits to a live competition are allowed but logged as digest-bound audit events.
- **Protocols**: `CompetitionInvitationProtocol` and `CompetitionSubmissionProtocol`, versioned, bound to competition ID and manifest digest. Miners accept them only from the on-chain subnet-owner hotkey.
- **Miner SDK** (`miner/competition_sdk.py`): exports a sanitised standalone repo, runs it in a validator-shaped Modal sandbox on a warmup fixture, writes a 24-hour receipt, then creates or updates the private GitHub repo.
- **Intake and validation** (`intake.py`, `validation.py`): clone through ephemeral askpass, strip the remote, pin commit, tree and committer time, make it read-only, and reject unsafe layouts with stable reason codes. The PAT is never persisted or logged.
- **Trusted build and sandbox** (`build.py`, `modal_runner.py`): the validator, not the contender, sets GPU, CPU hard limit, timeout, `block_network=True`, no secrets, and the two mounts. Contender Python is never imported by the validator.
- **Dataset** (`dataset.py`, `scripts/competition_dataset.py`): prepare, validate, upload, read back and seal an index into immutable per-batch Modal Volume subdirectories.
- **Execution** (`execution.py`, `timeouts.py`): SQLite is the queue. Transactional batch claims, leases with expiry, idempotency on competition, hotkey, item and attempt, and dynamic timeouts computed from item metadata.
- **Scoring** (`scoring.py`, `scoring_api.py`): per-item media score, per-item cost relative to the cheapest valid contender, and length-weighted completion. Failures score zero, never skipped.
- **Ranking and weights**: exact-score ties go to a recorded human order, then earlier committer timestamp, then commit SHA. The latest podium (70/20/10) holds its share until a later competition atomically replaces it. Burn stays at zero.
- **Entry guards**: minimum alpha stake at opt-in and again at finalisation, with block-pinned evidence. A GitHub account or repo already used by another contender is rejected.

## How the SN85 competition branch was delivered

The engine landed as one large PR (#179, about 17.8k lines in `vidaio_subnet_core/competition/`, `services/`, `scripts/` and the miner SDK) built in phases 0 to 5, then about 16 smaller PRs hardened it through the first live competition, which concluded in #197. Phases 6 to 8 are partly done: owner-only handlers and the alpha-stake gate shipped, the public audit bundle did not.

| Phase | Delivered | Exit test it had to pass |
| --- | --- | --- |
| 0 Feasibility and safety gates | Scoring formula, PAT redaction boundary, cost allocation, live Modal probe of network block and billing access | Offline Modal and billing proven. Hostile 25 GB build limit only partially proven, so production needs an explicit "unattested size" acknowledgement flag. |
| 1 Contracts and persistence | Manifest model, protocols, dual-mode miner handlers, state machine, competition-only SQLite tables, leases, audited manifest edits | A fake-clock competition advances through every state and resumes after the manager is recreated at each boundary. |
| 2 Miner template and intake | `/health` and local-path `/compress`, warmup fixture, miner SDK with sandbox self-test and receipt, clone and pin, static checks, private S3 snapshot at freeze | Template repo accepted; malicious, path-escape, oversized and obfuscated fixtures rejected with stable codes. |
| 3 Trusted build and isolation | Modal build per contender, immutable image binding, sandbox supervisor with active isolation probe, restart reattach | No DNS or IP egress, no write to inputs, no access to other contenders, hard GPU and CPU caps, termination on timeout. |
| 4 Evaluation and scoring | Dataset seal CLI, SQLite-only dispatcher (Redis dropped as unnecessary), output validation, VMAF, cost from probed hardware | Multi-contender run processes each item exactly once through restarts, and aggregates reproduce from history. |
| 5 Finalisation and weights | Provisional ranking, review CLI, tie-break chain, boss contender, podium weights merged with inference pools | Ties, disqualification recompute, UID merging, zero burn, atomic incumbent replacement. |
| 6 Operator rollout | Checklists for miners and validators, dataset runbook | Dev and shadow runs. |
| 7 Public audit bundle | Owner-only handlers shipped; S3 reproduction bundle and `competition_audit verify` not built | A clean machine reproduces every score from the bundle. |
| 8 Alpha-stake entry | `minimum_alpha_stake`, checks at opt-in and finalisation, reason codes | Exact-threshold, below-threshold, metagraph movement, restart determinism. |

### Lessons from the post-launch PRs (#182 to #197)

These are the changes the real competition forced. Each should be designed in from day one for RADiant.

- **Fresh sandbox per batch, not per contender** (#182). A warm sandbox reused across batches could cache future inputs or do work outside measured time. Each batch now gets its own sandbox with only that batch mounted.
- **Deterministic scoring** (#185). Random VMAF frame sampling was removed so any rerun gives the same number.
- **Timeouts from item metadata** (#186, #187). A fixed timeout was too tight for long items and too loose for short ones. Deadlines are now computed per batch from sealed metadata with a manifest minimum.
- **Metagraph resync and stake re-check at freeze** (#189, #190, #192). Stake and registration change during a 4-day enrollment, so the validator resyncs and re-invites miners that were refused for low stake or declined earlier.
- **The scoring worker needs storage access too** (#194). The trusted scorer reads the Volumes directly, so it needs the Modal SDK and credentials, separate from the dispatcher.
- **A sandbox error is the miner's fault** (#195). Treating it as validator infrastructure let it be retried. Only a confirmed validator incident can be requeued, through an explicit operator command.
- **Automatic replay is off.** Retrying failed items let a contender fail expensive items and redo them cheaply. This matters for RADiant too: no free retries.
- **The first real manifest diverged from the design.** `competition-01` set cost efficiency to 0 and coverage to 0.4, capped videos at 50 s, and required 500 alpha stake. Plan for scoring factors to be tuned per competition.
- **Emission split is a live knob.** Commit 473aa62 raised the competition share from 20% to 60% by hand after launch.

## RADiant architecture

The owner validator is one docker-compose stack. Miners talk to it only through the chain, the API and the bucket, and their code runs only inside Modal sandboxes that have no network.

### Services

Five application services share one Postgres, alongside Caddy and Adminer. Adminer is bound to localhost and logs in with a read-only Postgres role. Campaign seeding is a one-off CLI job in the same image. Each container gets only the secrets it needs: the API holds Postgres and bucket credentials but no Modal, HF, DataHive or wallet keys, and only the worker holds the HF write token.

```text
                 miners: chain commitments, API, bucket
                                   |
             Caddy  ->  api   (Postgres + bucket creds only)
                                   |
   watcher -- worker -- round-worker -- weights      Adminer (localhost, read-only role)
        \        |          |              |
         \------ Postgres (shared) --------/
   chain (subtensor)   HF (pins)   Modal (builds, sandboxes)   DataHive (round fetch)
```

The watcher reads the chain, the worker runs gates and builds (Modal, HF, bucket), the round worker fetches from DataHive and runs Modal sandboxes, and the weights service sets weights on chain.

The miner signs and uploads through the API, commits on-chain, and from then on only reads the API. The worker and round runner are the only services that reach Modal.

### Submission flow

1. `radiant-sdk prepare` builds a clean solution directory from the miner's working tree. `radiant-sdk validate` runs the same gates and harness the validator uses, in a disposable offline Modal sandbox on the chosen GPU, and writes a 24-hour receipt.
2. `radiant-sdk submit` zips the prepared directory deterministically. It signs `radiant.solution-upload.v1` with the hotkey, gets a presigned PUT from `POST /v1/uploads/solution` and PUTs the zip with a checksum and `If-None-Match`.
3. The SDK pays the campaign fee from the coldkey, then commits `r1|campaign_id|sha256:…|code:…|locator|pay_block|pay_tx`, about 300 bytes. `sha256` covers the whole zip; `code` is `code_sha256`, the hash without `.env`, which keeps a reveal checkable.
4. The watcher verifies the commitment as Pareton does and enqueues a gate job.

### Solution format

```text
solution.zip
  radiant_solution.json     # base_class, sdk_version, gpu, cpus, batch_size
  .env                      # HF_REPO_ID, HF_REVISION (40-char commit), HF_TOKEN
  requirements.lock         # pinned wheels, hashes required
  model.py                  # one subclass of the campaign base class
  src/...                   # optional extra modules
```

Only those three `.env` keys are allowed. The miner's token is used once, at gate g5, to download the weights. It never enters the build or the runtime sandbox, is never stored or logged, and is removed before any reveal. Miner docs ask for a fine-grained read-only token scoped to that one repo.

The miner's own settings live outside the solution, in a `miner.env` that `radiant-sdk` reads: `RADIANT_API_URL` (a dev VM during development, `https://api.phaosai.org` in production), the network and netuid, and the wallet name and hotkey. The reference miner ships two templates, `miner.env.template` and `solution/.env.template`. `prepare` refuses a solution `.env` with any key beyond the three above, so a stray `RADIANT_API_URL` or wallet setting never ends up in a zip. The fee recipient stays pinned in the SDK for each network, as in Pareton, so a wrong `RADIANT_API_URL` can't redirect a payment. The SDK also requires HTTPS for anything but localhost.

### Model contract

The campaign manifest pins `base_class: radiant_sdk.models.v1:ChestXRayClassifier` and the label list. A sketch:

```python
class ChestXRayClassifier(ABC):
    labels: ClassVar[tuple[str, ...]]   # injected from the manifest

    @abstractmethod
    def load_model(self, weights_dir: Path, device: str) -> None: ...
    @abstractmethod
    def load_model_checkpoint(self, path: Path) -> None: ...
    @abstractmethod
    def run_single_evaluation(self, image: np.ndarray) -> Any: ...
    @abstractmethod
    def run_batched_evaluation(self, images: list[np.ndarray]) -> list[Any]: ...
    @abstractmethod
    def map_to_labels(self, raw: Any) -> dict[str, float]: ...   # label -> probability
```

The validator's harness runs inside the sandbox and owns the entrypoint. It calls `load_model` under `model_load_timeout` (set per campaign in the manifest and signed into manifest\_hash), feeds images as `uint16` arrays in manifest-sized batches, and accepts only a map from the manifest labels to floats in \[0, 1\], rounded to 3 decimals before scoring. Anything else counts as a failed item. Wall time is measured by the validator outside the sandbox, so miner code cannot fake its own speed.

### Gates

| # | Gate | Check | Source |
| --- | --- | --- | --- |
| g0 | Identity | Hotkey registered, campaign open, not disqualified. No stake check | SN10 a |
| g1 | Integrity | Zip size at most the campaign's max\_solution\_zip\_mb (default 10 MB), read from the object's size before download. Then fetch with bucket credentials; sha256 and code\_sha256 equal the commitment; locator owned by the hotkey | SN10 b |
| g2 | Archive safety | No zip-slip, symlinks, nested archives or opaque binaries (`.pyc`, `.so`, `.pt` in the zip); file count cap and an uncompressed-size cap (max\_solution\_unzipped\_mb, default 50 MB) against zip bombs; required files present | SN85 validation |
| g3 | Contract | AST check, without importing: exactly one class subclasses the pinned base class and overrides every abstract method. Network and process modules are banned in imports. | Your gate (i) |
| g4 | Obfuscation | Pareton d1-style deterministic scan plus vidaio's readability rules. Optional LLM review later. | Your gate (iii) |
| g5 | `.env` and Hugging Face | Only allowed keys. Revision is a full commit SHA. The repo is reachable with the miner's token at that revision, and the file sizes in its metadata are under the cap. The worker then downloads that revision once to its scratch disk and checks each file against the hash Hugging Face reports. | Your gate (ii) |
| g6 | Duplicate | `code_sha256` plus the weights hash, compared with earlier submissions in the campaign. The earliest commit keeps the slot. | SN10 dedupe |
| g7 | Build | Modal image from the pinned base image, the lockfile, the code and the weights downloaded at g5, added from the worker's disk. No HF token reaches the build. Image cap and `modal_build_timeout` come from the manifest. The build log is shown to the miner. | SN85 build |
| g8 | Qualification | Offline sandbox on the chosen GPU, with no evaluation data mounted. `load_model` within `model_load_timeout`, then `run_single_evaluation` and a batch on public warmup X-rays. Outputs cover every label. Its log is shown to the miner. | SN85 warmup |
| g9 | Pin | The last gate, so only submissions that pass everything are pinned. The worker uploads the files from g5 to a private repo in the validator's HF org (see Weight pinning), records per-file hashes and deletes its local copy. Passing queues the submission for a round. | Your 26 Nov change |

### Weight pinning

The validator's .env holds `HF_PIN_ORG` and a write token that only the pin gate uses. Pinning is the last gate, g9, so nothing is uploaded for a submission that fails earlier. Until then the worker works from a single download: at g5 it fetches the miner's repo at `HF_REVISION` with the miner's token, checks each file's hash, and keeps the files on its scratch disk for the build. At g9 it uploads those same files to a new private repo in `HF_PIN_ORG` and deletes the local copy. Hugging Face's server-side `duplicate_repo` can't do this: no single token can both read the miner's private repo and write to ours, and it copies the whole history rather than one revision.

Pins are named `rad-<campaign_id>-<hotkey>-s<submission_id>`, for example `rad-cxr-tb-01-5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY-s42`. Hugging Face allows at most 96 characters and forbids `--` and `..`, so with a 48-character hotkey the campaign ID is capped at 24 characters and the submission ID is the short numeric ID from Postgres.

Rounds run the image built at g7, so a miner can't swap or delete weights after submitting. The pin is the durable copy: it rebuilds an image if Modal loses one (with a read-only token), and it is switched to public on reveal. Only submissions that pass every gate are pinned, which keeps the org inside its private storage quota.

### Rounds

- **Trigger**: a round fires at `round_size` queued qualified submissions (default 5) or after `round_max_wait`, but only while fewer than `MAX_ROUNDS_PER_24H` rounds (validator .env, default 3) have started in the last 24 hours across all campaigns; otherwise it waits for the oldest round to leave the window. There is one live round per campaign. If the queue grows faster than 15 a day, raise `round_size` rather than the cap, so the DataHive volume stays the same.
- **Draw**: the seed is `hash(finalised block hash, campaign_id, round ordinal)`. At round start the runner lists that UTC day's images from DataHive's `/files`, filtered by the campaign's conditions and country and sorted by filename, then picks `round_sample_size` studies with that seed (default 100, set per campaign, so later campaigns with more conditions can draw more; per-label quotas of positives and negatives first, then a random fill; see Scoring), and downloads each pick through its short-lived `/files/{filename}/url` link. Development and testnet point `DATAHIVE_API_BASE` at DataHive's staging API, or at the local stand-in until staging is ready. The round stores the full listing it drew from, the study IDs, label snapshot and image hashes in the validator's Postgres, which no sandbox can reach, so an auditor can replay the draw even after DataHive has served those images.
- **Run**: the current leader and each challenger get their own fresh sandbox on their chosen hardware, all on that draw. The leader is re-run every round because every round draws new studies. There is no baseline run. The fetched images sit on a read-only per-round Volume that is deleted once the round settles. Nothing from a round sandbox is shown to miners (see Keeping evaluation data in).
- **Score and rank**: entries are scored as below and ranked with Pareton's crown rule. The round is void on leader infra failure or when no challenger survives. An entry whose sandbox never starts (no GPU within `SANDBOX_GPU_WAIT_S`, image pull or Modal errors) is `infra_failed`, not scored, and requeued once for a later round; a crash after the harness has started is the miner's.
- **Report**: when the round settles, the runner posts each served image's outcome to DataHive's `/feedback`: used if it was scored, unused if the round is void, and invalid with a reason if it couldn't be used. DataHive counts an unreported image as used after 72 hours.
- **Seed leader (optional)**: a campaign may open with a solution submitted by the owner hotkey through the normal pipeline, fee-exempt, and it may be a low-scoring baseline. It holds the crown until a challenger beats it, and its emission share burns while it does. Without one, the crown starts vacant and the best entry in the first round takes it. Testnet campaign 1 seeds the reference DenseNet solution so the loop runs end to end.

### Keeping evaluation data in

Images come from DataHive into the round runner and go only to that round's Modal Volume. Miners, other validators and the API never get them. The validator enforces every rule below itself, so nothing a miner writes (overridden methods, a patched harness, threads, import-time code, delayed prints) can change them.

- **Two kinds of sandbox.** A qualification sandbox (gate g8) never has evaluation data mounted; it loads the model and runs public warmup X-rays. A round sandbox has the round Volume mounted from the moment it starts, so even its `load_model` could read the data.
- **Labels never enter a sandbox.** The round runner on the validator splits each DataHive response: pixels go to the round Volume, labels go only to Postgres, which no sandbox can reach. Each image is decoded and written as a bare `uint16` array, so no PNG text chunks, DICOM tags or report text travel with it. Files are named with random tokens, not DataHive IDs or evidence IDs, and each sandbox gets its own order, shuffled with a validator secret; otherwise the quota-first draw would put the positives first. The harness only writes predictions. Scoring runs on the validator after the sandbox exits, and nothing secret is ever in the sandbox (no labels, keys, shuffle secret or HMAC key), so overriding methods or patching the harness finds nothing. Per-item labels are published only after the round settles, from R8, and those items are never served again.
- **Only data-free logs are shown.** Miners see the Modal build log and the qualification sandbox's log, which together cover package installs and model loading. Everything a round sandbox prints stays internal. Its failures reach the miner as a fixed reason code, such as `EVAL_TIMEOUT`, `EVAL_CRASH` or `BAD_OUTPUT`, never as exception text.
- **Outputs are the only way out, and they are narrow.** No network, no secrets, read-only inputs. The harness accepts only floats for the manifest labels, rounded to 3 decimals. Per-item timing is never published; reports give round totals only.
- **What leaves the validator.** Scores, round totals and, from R8, per-item evidence: an opaque item ID (an HMAC of the DataHive ID with a validator secret, so items can't be linked across rounds or back to DataHive), the true labels and the rounded predictions. No pixels, no image hashes and no DataHive IDs. The API container has no Modal or DataHive credentials, so no route can serve an image, even by mistake.
- **Daily breach check.** A daily job reads DataHive's `/usage` events for the past day and matches them against the validator's own fetch log. Any listing, download link or storage download the validator didn't make, or any request from an IP that isn't the validator's, raises an alert, and the key is rotated.
- **Tested.** R4 and R8 include hostile fixtures that try to print, raise or encode data from every override and from import time. They pass only if none of it reaches the API, the shown logs or the reason codes. Another fixture searches the mount, the environment, file names and file order for labels and predicts from whatever it finds; it passes only if it scores at chance (AUROC about 0.5).

### Scoring

Quality starts from a per-label metric m, AUROC by default. A campaign can choose F-beta instead, with beta and the threshold in the manifest (beta 1 and 0.5 in v0). Each label's metric goes through a quality curve that rises fast up to a knee, AUROC 0.8 by default, and gives diminishing returns after it. Q is the weighted average of the curve values over labels:

```latex
q(m) = \begin{cases} 0 & m \le m_0 \\ q_k \dfrac{m - m_0}{k - m_0} & m_0 < m \le k \\ q_k + (1 - q_k)\left[1 - \left(1 - \dfrac{m - k}{1 - k}\right)^{p}\right] & m > k \end{cases}
```

```latex
Q_i = \frac{\sum_l w_l \, q(m_{i,l})}{\sum_l w_l}
```

Defaults, all set in the manifest: chance level m\_0 = 0.5 for AUROC (0 for F-beta), knee k = 0.8, knee value q\_k = 0.8 and curvature p = 2. Below the knee, q climbs 2.67 per AUROC point. Just above it the slope is 2.0, and it falls to zero at 1.0. The seed CLI refuses parameters where the slope just above the knee is steeper than below it. I used a curve rather than literal steps: a step function pays nothing for gains between steps, so the 1% crown margin could only be met at a step edge.

With w\_q = 0.7, every label at the leader's AUROC, and speed and cost tied at 0.8, this is the AUROC gain a challenger needs to take the crown on quality alone:

| Leader's AUROC | q | Gain needed |
| --- | --- | --- |
| 0.60 | 0.27 | +0.002 |
| 0.70 | 0.53 | +0.003 |
| 0.80 | 0.80 | +0.006 |
| 0.85 | 0.89 | +0.009 |
| 0.90 | 0.95 | +0.014 |
| 0.95 | 0.99 | Not reachable on quality alone; it needs a speed or cost gain too |

**Class imbalance.** DataHive's labels won't be balanced; about 30% of DataHive's images are positive. Four rules keep every label fair:

- AUROC is computed per label and doesn't depend on how common the label is, unlike accuracy or a pooled F1. With F-beta, each label's F-beta is likewise computed on its own.
- Labels are averaged with equal weights (w\_l = 1) by default, so a rare label counts as much as a common one. A campaign can set `label_weights` for clinical priority, signed into `manifest_hash`.
- The curve is applied per label before averaging, so lifting a weak label from 0.65 to 0.75 earns five times more than lifting a strong one from 0.90 to 1.0.
- The draw fills per-label quotas first (`min_positives_per_label` and `min_negatives_per_label`, default 20 each), then the rest at random. A label that can't meet its quota in a round is left out of that round's Q, and the round report says so.

A failed or invalid item is scored as maximally wrong: probability 0 for its positive labels and 1 for its negative ones. Speed and cost are relative within the round, as in vidaio's cost efficiency:

```latex
S_i = \frac{\min_j t_j}{t_i}, \qquad C_i = \frac{\min_j c_j}{c_i}
```

```latex
\text{score}_i = w_q Q_i + w_s S_i + w_c C_i, \qquad w_q + w_s + w_c = 1
```

Here t is the validator-measured batched evaluation time, and c is the cost at locked Modal rates for the hardware actually observed. The weights and curve parameters are set in the manifest before launch and signed into `manifest_hash`. There is no separate quality gate. The crown rule, beat the leader's score times (1 + epsilon), applies to this weighted score, and each campaign sets w\_q highest when quality matters most. An all-negative model now gets AUROC 0.5 and Q = 0; the seed CLI still warns when the weights would let it beat a working model on speed and cost alone. The R6 implementation doc carries the curve, its parameters, these imbalance rules and a table of test values.

### Emissions and reveal

- **Emissions**: each campaign carries Pareton's `emission_rule` (`linear_decay`) with Pareton's defaults for the first campaign: start 0.10, floor 0.02, 201,600 blocks (about two weeks). The first campaign pays little on purpose, to test the flywheel, and the residual is burned. Several campaigns can run at once as long as their start weights sum to at most 1.
- **Reveal**: every evaluated submission is revealed once the campaign's `reveal_delay_s` has passed (Pareton's default, 48 h). If a round's leader has quality Q below `fast_reveal_quality_threshold`, every entry scored in that round uses the shorter `fast_reveal_delay_s` instead, so miners can build on weak solutions in the next rounds. The threshold is on Q, not the weighted score, because speed and cost are relative to each round. A reveal publishes the solution zip with `.env` removed and switches the submission's pinned HF repo to public. `.env` must never be published: it holds the miner's HF token, and anyone with it could read that miner's future repos before they are submitted. Miners can fork any revealed solution with `radiant-sdk prepare --from <hash>`.
- **A reveal stays verifiable without `.env`**: the commitment carries a second hash, `code_sha256`, over every file in the zip except `.env` (the same hash gate g6 uses). Once g5 has downloaded the weights, or as soon as a submission is rejected, the worker replaces the private zip in the bucket with the stripped copy, checked against `code_sha256`. The miner's token then no longer sits in the bucket, and anyone can check a revealed zip against the chain.

### Public API

A FastAPI service on Pareton's model:

- **Campaigns**: `/v1/campaigns`, `/v1/campaigns/{id}` (manifest, fee, visibility, labels, allowed GPUs, the base class source) and `/v1/campaigns/{id}/leader`.
- **Submissions**: `/v1/campaigns/{id}/submissions` and `/v1/submissions/{hash}`, giving state, the gate event log with reason codes, and the build and qualification log (never round sandbox output).
- **Rounds**: `/v1/rounds/{id}` and `/v1/rounds/{id}/entries/{entry}/report` (metrics, timings, cost, hardware).
- **Audit (R8)**: `/v1/rounds/{id}/evidence`, per-item evidence for auditors: opaque item IDs, true labels and rounded predictions, never images.
- **Weights and reveal**: `/v1/weights` and `/v1/campaigns/{id}/submissions/{hash}/solution`.
- **Writes and health**: `POST /v1/uploads/solution` and `/health`.

### Auditing by other validators

Until R8, other validators do what Pareton's `auditor.py` does: fetch `/v1/weights` and set it. In R8, `radiant-auditor` also fetches the leader history and round evidence, recomputes every round's scores and crown decisions and the decayed weight vector at the current block, and sets weights only if the result matches. On a mismatch it alerts and falls back to its last verified vector. Auditors work from labels and predictions only, so a modified auditor has nothing more to unlock.

## Ground-up implementation plan

The RADiant repo starts clean. Each phase ports named modules from SN10 or SN85 and ends with a test gate. The current `validator.py` is kept only as a reference until R6 replaces it.

### Repository layout

```text
config.py                     # env-driven settings, one place (SN10 style)
docker-compose.yml  Caddyfile  .env.example
TODO.md                       # launch checklist, kept current every phase
docs/implementation/          # one spec per phase, R0.md to R8.md
db/schema.sql  db/migrations/
campaign/   models.py manifest.py store.py seed.py fees.py visibility.py set_fee.py
chain/      commitment.py payment.py watcher.py rpc.py weights.py
storage/    s3.py upload_auth.py visibility.py          # Hippius / Backblaze via endpoint URL
gate/       identity.py integrity.py archive.py env_hf.py contract.py obfuscation.py dedupe.py
build/      modal_image.py hf_pin.py                    # private HF pins, no bucket mirror
sandbox/    modal_runner.py isolation_probe.py pricing.py timeouts.py   # from SN85
data/       datahive_client.py adapter.py draw.py round_volume.py   # live fetch per round
round/      create.py budget.py runner.py harness_host.py quality.py score.py rank.py store.py
weights/    build.py loop.py
worker/     __main__.py watcher.py pipeline.py round_job.py
api/        server.py
radiant_sdk/                  # pip-installable; also baked into every sandbox image
  models/v1.py  harness.py  cli.py (prepare | validate | submit | status)
  reference_miner/            # DenseNet-121 starter; packaged as the testnet seed leader
scripts/auditor.py  scripts/preflight.py
tests/
```

### Phases

| Phase | Build | Port from | Exit gate |
| --- | --- | --- | --- |
| R0 Feasibility | ~~DataHive's staging and production APIs reachable from the Hetzner validator host. Fetch time and rate limits for 3 rounds of 100 studies a day.~~ (Moved to R5 on 27 Nov: DataHive isn't needed until then.) ~~Hippius and Backblaze presigned PUT with checksum and `If-None-Match`.~~ (Moved to R3.) Modal offline sandbox on L4 and L40S. ~~Private HF download with a fine-grained miner token, upload into the validator's org, and that org's private storage quota.~~ (Moved to R4.) Base models scored on the demo images. | SN85 phase0 probe | Written report with each gate passed or explicitly accepted. |
| R1 Foundations | docker-compose (Postgres, Caddy, Adminer with a read-only role, MinIO as a local S3-compatible store for solution zips), `config.py`, schema and migrations, campaign manifest model and hash, fee history, visibility, seed CLI, `TODO.md` | SN10 `campaign/`, `db/` | Fresh `docker compose up` seeds a campaign and the hash is stable across reruns. |
| R2 SDK and reference miner | `radiant_sdk.models.v1`, the harness, the reference miner on DenseNet-121 (a starter for miners, never run in rounds), `prepare` and `validate` with an offline Modal self-test and receipt; `submit` refuses to presign or pay without a matching fresh receipt | SN85 `competition_sdk.py`, `common_preflight.py` | The reference miner passes `validate`; a miner missing an override fails with the same reason code the validator would give. |
| R3 Chain intake | Hippius and Backblaze presigned PUT with checksum and `If-None-Match` (moved from R0; probe: `python -m scripts.r0 storage`). Upload auth, `POST /v1/uploads/solution`, `submit` (sign, PUT, pay, commit), commitment codec, watcher, fee verification, payment reference consumption, first-seen dedupe | SN10 `storage/`, `chain/`, `miner/commit_patch.py` | On testnet, a paid submission is ingested exactly once. Reused, underpaid, wrong-recipient and foreign-locator proofs are all rejected. Before paying, `submit` checks `/v1/info` against `miner.env` (network, netuid 292 on testnet) and refuses on a mismatch. |
| R4 Gates, build, qualification | Private HF download with a fine-grained miner token, upload into the validator's org, and that org's private storage quota (moved from R0; probe: `python -m scripts.r0 hf`). g0 to g9, with HF pinning into the validator's org as the last gate, Modal image build with `modal_build_timeout`, qualification sandbox and its shown log, persisted states and events | SN10 `worker/pipeline.py`, `gate/obfuscation.py`; SN85 `validation.py`, `build.py`, `modal_runner.py` | Malicious fixtures (zip-slip, symlink, `.pyc`, missing override, wrong HF revision, obfuscated loader, slow `load_model`) each fail with a stable code. A submission that fails before g9 leaves no pin. Pins are private and named `rad-<campaign>-<hotkey>-s<id>`. The reference miner reaches `queued`. A qualification sandbox that gets no GPU within `SANDBOX_GPU_WAIT_S` leaves the submission `infra_failed` and requeued once, never rejected. g5 refuses non-safetensors weight files; the reference weights are published as safetensors under the RADiant HF org and pinned in the reference miner's `.env.template`. |
| R5 Live data fetch | DataHive staging (then production) reachable from the Hetzner validator host, with fetch time and rate limits for 3 rounds of 100 studies a day (moved from R0; probe: `python -m scripts.r0 datahive`). DataHive client built to the DataHive API spec (base URL and key from .env, staging API for development and testnet), a local stand-in that implements the spec over the demo API's 20 images until staging is ready, a daily `/usage` breach check, label adapter, metadata stripping, random file names and a secret per-sandbox shuffle, opaque IDs, seeded stratified selection, per-round Volume upload and teardown, per-round stored listing and label snapshot | SN85 `dataset.py` (Volume upload and read-back only) | Against the stand-in, or staging after a reset, the same block hash and stored listing give the same draw. Labels never appear on any sandbox mount, and the Volume is gone after the round. A download the validator didn't make, planted in the stand-in's `/usage` log, raises an alert. |
| R6 Rounds and scoring | Round trigger with `MAX_ROUNDS_PER_24H`, optional seed leader, runner with one sandbox per entry, quality curve over per-label AUROC (F-beta optional), per-label draw quotas, speed, cost, crown rule, void rules, leader history, a `/feedback` report when a round settles | SN10 `round/`; SN85 `execution.py`, `pricing.py`, `timeouts.py` | A 5-challenger round reproduces its scores from stored evidence. The curve matches the test table in `docs/implementation/R6.md`. A label at 5% prevalence moves Q as much as one at 50%. With quality-heavy weights, a faster but worse model does not take the crown. A fourth round within 24 hours waits. A sandbox crash scores as wrong, not void. Every served image gets a `/feedback` report, and a void round's images are reported as unused. A challenger whose sandbox gets no GPU is `infra_failed` and runs in a later round; a second infra failure is terminal; a leader infra failure voids the round without spending its challengers' retry. |
| R7 Emissions, weights, reveal | Emission rule with Pareton's defaults, weight vector with burn, `set_weights` loop, reveal of every evaluated submission (zip without `.env`, pinned HF repo made public, fast reveal below the quality threshold, stripped zip swapped into the bucket after g5), Pareton-style auditor that copies `/v1/weights` | SN10 `weights/`, `storage/visibility.py`, `scripts/auditor.py` | The auditor's vector matches the owner's at a given block. A revealed zip never contains `.env`, a revealed zip matches the committed code\_sha256, a revealed pin is public at the recorded revision, and a round led below the threshold reveals on the short delay. |
| R8 API, audit and rollout | All public endpoints, the evidence route, the recomputing `radiant-auditor`, a data-exfiltration red-team suite, runbooks, a testnet dev campaign with short timers, and a shadow campaign on mainnet with zero emission | SN10 `api/server.py`, `ops/runbook.md` | No hostile fixture gets data into the API, the shown logs or the reason codes. The auditor recomputes the owner's vector from evidence. Dev and shadow sign-off, then the first live campaign. |

R1 and R2 can run in parallel; R5 can run alongside R3 and R4. The first end-to-end testnet loop is at the end of R6.

### Docs kept with the code

- `docs/implementation/R<n>.md` is written when each phase starts and updated when it closes. `R6.md` holds the quality curve, its parameters, the class-imbalance rules and a table of test values for the quality share of the score.
- `TODO.md` is a launch checklist that every phase keeps current. Its first entry: point `DATAHIVE_API_BASE` from DataHive's staging API to its production API before launch. Another: make the production API domain the SDK's default `RADIANT_API_URL`. Each phase adds anything it defers to launch.

## Design decisions (all settled)

Every decision is now settled. You took my recommendation on all five below on 25 Nov, then made the seed leader optional on 26 Nov; the twelve earlier ones are recorded in Decisions recorded on 25 Nov.

| # | Decision | Options | Settled, and why |
| --- | --- | --- | --- |
| 1 | Where the quality curve applies | (a) Per label, then averaged. (b) Average first, then the curve. | **(a).** It rewards lifting weak labels first, which is the class fairness you asked for, at no extra cost. |
| 2 | Curve defaults | Knee 0.8, knee value 0.8, curvature 2, or other values per campaign. | **These defaults for campaign 1.** Past AUROC 0.9 a challenger needs about +0.014 AUROC to take the crown on quality alone, and past 0.95 it can't. If the crown stalls, lower epsilon rather than flatten the curve. |
| 3 | Seed leader on mainnet | (a) Every campaign opens with an owner seed leader. (b) Testnet only; mainnet opens with the crown vacant. | **Changed 26 Nov: optional per campaign, and it may be a low-scoring baseline.** A weak leader is handled by the fast reveal instead (see Emissions and reveal). |
| 4 | Scope of the round cap | (a) MAX\_ROUNDS\_PER\_24H across all campaigns. (b) Per campaign. | **(a).** All campaigns share one DataHive key, so only a global cap bounds the fetch volume. |
| 5 | Prediction rounding | 3 decimals, 2 decimals, or none. | **3 decimals.** Scores use the rounded values, so R8 evidence reproduces exactly, and it narrows what a model could encode in its outputs. |

Settled on 25 Nov: Adminer, Pareton's fee order, reveal of every evaluated submission, the weighted score with the quality curve, Pareton's emission defaults, HF pinning with no bucket mirror, the live DataHive draw, Pareton-style audit until R8, re-running the leader, the reference miner, no alpha stake, the curve applied per label with its defaults, an optional seed leader with a fast reveal for weak leaders, a global round cap, 3-decimal rounding and raw `uint16` input.

## Risks and open questions

- **Data volume decides everything.** You'll work with DataHive on volume and quality. The demo API, and so the stand-in, has 20 images, so rounds against it test the plumbing, not the ranking: per-label AUROC on 5 positives is noise. Staging's 1,000 or more images (proposed in the spec) allow full-size testnet rounds.
- **Probing the private pool.** DataHive will make the API return unique entries, so a study is never served twice. The validator also keeps image hashes internally and drops a repeat as a backstop, and the round cap limits how often anyone gets feedback.
- **Synthetic labels.** The demo labels are synthetic over real images; DataHive says staging will have real labels, which isn't confirmed yet. Testnet moves from the stand-in to staging once staging is ready, and `TODO.md` tracks the switch to production before launch.
- **Leaking through the sandbox.** Handled as in SN10: miners see build and qualification logs only, from sandboxes that never hold evaluation data. See Keeping evaluation data in.
- **The HF token in `.env`.** Agreed: miner docs ask for a fine-grained read-only token scoped to one repo. The validator uses it only at g5, for one download of the weights, never stores it, and strips it from every reveal.
- **Unattested image size on Modal.** Handled as in SN85: the manifest sets `modal_build_timeout` and an image cap.
- **Single scorer.** Recomputing auditors come in R8. Other validators and miners get true labels and rounded predictions only, never images. The contract is enforced on the owner's side, so changing anyone else's code can't break it.
- **Partner host reachability.** The validator runs on a bare-metal VPS, likely Hetzner, with open internet. The November block came from egress allowlists in the environments I probed from, so R0 only needs to confirm it from that host.
- **Adminer is a full database client.** Bound to localhost and logged in with a read-only role, it can't change state. Never route it through Caddy.
- **Hugging Face private storage.** Only submissions that pass every gate are pinned, and each pin stays private until it is revealed. R0 checks the org's quota.
- **Diminishing returns meet the 1% margin.** Past AUROC 0.9, small real gains no longer take the crown on quality alone (see the table in Scoring). Watch the first campaign and tune epsilon if the crown stalls.

## Sources

- [Pareton-ai/pareton](https://github.com/Pareton-ai/pareton), read at commit 9745a76: `miner/commit_patch.py`, `storage/upload_auth.py`, `storage/s3.py`, `chain/commitment.py`, `chain/payment.py`, `chain/watcher.py`, `gate/*`, `worker/pipeline.py`, `round/create.py`, `round/rank.py`, `weights/build.py`, `config.py`, `api/server.py`, `ops/systemd/pareton-api.service`, `docs/campaign-fees.md`, `docs/patch-visibility.md`, `docs/auditor.md`, `docs/technical-decisions.md`
- [Pareton API docs](https://api.pareton.ai/docs) and [Pareton-ai/pareton-frontend](https://github.com/Pareton-ai/pareton-frontend), cited from your message and not opened
- [vidaio competition\_mode\_implementation\_plan.md](https://github.com/vidAio-subnet/vidaio-subnet/blob/main/docs/competition_mode_implementation_plan.md)
- [vidaio feature/compression-competitions](https://github.com/vidAio-subnet/vidaio-subnet/tree/feature/compression-competitions), read at commit 171ace8: `docs/competitions.md`, `docs/competition_phase0_feasibility_report.md`, the archived `competition-01` manifest, and PRs #179 to #197
- [vidaio competition SDK README](https://github.com/vidAio-subnet/vidaio-subnet/tree/main/miner#competition-sdk-prepare-validate-and-publish-a-solution)
- Base model candidates: [torchxrayvision](https://github.com/mlmed/torchxrayvision#getting-started), [densenet121-res224-all](https://huggingface.co/torchxrayvision/densenet121-res224-all), [rad-dino](https://huggingface.co/microsoft/rad-dino), [cxr-foundation](https://huggingface.co/google/cxr-foundation). Their label sets and licences are from memory and are checked in R0.
- RADiant repo at commit 6d4167c, [RADient subnet design overview](https://claude.ai/artifact/XXz7R3LDAdVZxqmdVKBp5a) and [RADient miner baselines](https://claude.ai/artifact/GmgnTrdDq2KuRQiRX1CtcY)
