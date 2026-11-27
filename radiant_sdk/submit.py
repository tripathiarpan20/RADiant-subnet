"""``submit``: upload, pay and commit one validated solution (Pareton's fee order).

1. The receipt must match this zip, the campaign's current manifest and the
   SDK version, and be under 24 hours old (checked before anything else).
2. The validator's /v1/info must report the network and netuid in miner.env.
   The fee recipient comes from the campaign (each campaign sets its own),
   and must be one this miner trusts for that network: the SDK's pinned
   owner addresses, plus any listed in RADIANT_TRUSTED_FEE_RECIPIENTS in
   miner.env. A wrong or compromised RADIANT_API_URL can't redirect a payment.
3. The hotkey must be registered on the subnet.
4. Upload: sign the upload request with the hotkey, get a presigned PUT,
   PUT the zip with its checksum and If-None-Match. No money has moved yet.
5. Pay: a coldkey transfer of the campaign fee (testTAO on testnet), after a
   confirmation prompt unless --yes. The proof is (block, extrinsic index).
6. Commit: ``r1|campaign|sha256|code|key|block|index`` via
   Commitments.set_commitment, signed by the hotkey.

If the commitment fails after paying, rerun with --payment-block/--payment-tx
to reuse the payment instead of paying twice.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from .validate import _check_url, check_receipt, fetch_campaign

# Owner addresses this SDK trusts as fee recipients, per network. A campaign
# sets its own recipient in its manifest (submission_fee.recipient, outside
# the hash, with block-effective history); the SDK pays it only if it is
# trusted here or in the miner's RADIANT_TRUSTED_FEE_RECIPIENTS. Mainnet's
# owner address is added at launch (TODO.md).
TRUSTED_FEE_RECIPIENTS: dict[str, frozenset[str]] = {
    "test": frozenset({"5GYfPfFRQPaLytjCyT78X769ZU9xm5tL43dN11sknTiZx34a"}),  # owner coldkey (rumayao_coldkey)
    "finney": frozenset(),
}


def trusted_recipients(network: str, env: dict[str, str] | None = None) -> frozenset[str]:
    extra = (env if env is not None else os.environ).get("RADIANT_TRUSTED_FEE_RECIPIENTS", "")
    return TRUSTED_FEE_RECIPIENTS.get(network, frozenset()) | {a.strip() for a in extra.split(",") if a.strip()}


RAO_PER_TAO = Decimal(1_000_000_000)


def say(msg: str) -> None:
    print(f"[radiant-sdk:submit] {msg}", file=sys.stderr, flush=True)


def _http(method: str, url: str, body: dict | None = None) -> dict:
    import requests

    r = requests.request(method, url, json=body, timeout=30)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:200]
        raise ValueError(f"{method} {url.split('?')[0]} -> {r.status_code}: {detail}")
    return r.json()


def _payment_ref(result: Any) -> tuple[int, int] | None:
    raw = getattr(result, "extrinsic_id", None)
    if not isinstance(raw, str):
        return None
    block, _, index = raw.rpartition("-")
    return (int(block), int(index)) if block.isdigit() and index.isdigit() else None


def _check_fee_at_block(base: str, campaign_id: str, block: int, paid: dict[str, str]) -> None:
    """The fee in force at the payment's block must be no more than what was paid, to the same recipient.

    A fee change can take effect between quoting and inclusion. Committing an
    underpaid proof would only be rejected, so stop before committing.
    """
    history = fetch_campaign(campaign_id, api_url=base)["submission_fee_history"]
    entry = next((e for e in reversed(history) if e["effective_from_block"] <= block), None)
    if entry is None:
        raise ValueError(f"no fee is in force at block {block}")
    if entry["recipient"] != paid["recipient"] or Decimal(entry["amount_tao"]) > Decimal(paid["amount_tao"]):
        raise ValueError(f"payment at block {block} does not meet the fee in force then ({entry['amount_tao']} to "
                         f"{entry['recipient']}). Nothing was committed; don't reuse this payment for this campaign.")


def submit(zip_path: Path, campaign_id: str, *, api_url: str | None, network: str, netuid: int, wallet_name: str,
           wallet_hotkey: str, yes: bool = False, dry_run: bool = False, max_fee_tao: str | None = None,
           payment_block: int | None = None, payment_tx: int | None = None,
           wait_s: float = 0) -> dict[str, Any]:
    import bittensor as bt

    from .checks import code_sha256 as code_hash
    from .checks import read_zip
    from .commitment import encode, plaintext_fields
    from .upload import upload_message

    base = _check_url(api_url or os.environ.get("RADIANT_API_URL", ""))
    campaign = fetch_campaign(campaign_id, api_url=base)
    receipt = check_receipt(zip_path, campaign_id, campaign["manifest_hash"])  # before anything else
    say(f"receipt OK ({receipt['validated_at']})")

    info = _http("GET", f"{base}/v1/info")
    if (info["network"], info["netuid"]) != (network, netuid):
        raise ValueError(f"validator runs {info['network']}/{info['netuid']} but miner.env says {network}/{netuid}; "
                         "refusing to pay on the wrong chain")
    fee = campaign["submission_fee"]
    if fee["recipient"] not in trusted_recipients(network):
        raise ValueError(f"campaign {campaign_id} wants its fee paid to {fee['recipient']}, which you don't trust on "
                         f"{network}. If that is the subnet owner's address, add it to RADIANT_TRUSTED_FEE_RECIPIENTS "
                         "in miner.env; otherwise don't pay")
    fee_rao = int(Decimal(fee["amount_tao"]) * RAO_PER_TAO)
    if max_fee_tao is not None and fee_rao > int(Decimal(max_fee_tao) * RAO_PER_TAO) and payment_block is None:
        raise ValueError(f"campaign fee {fee['amount_tao']} is above --max-fee-tao {max_fee_tao}")
    currency = info.get("fee_currency", "TAO")
    say(f"campaign {campaign_id}: fee {fee['amount_tao']} {currency} to {fee['recipient']} on {network}/{netuid}")

    data = zip_path.read_bytes()
    sha = "sha256:" + hashlib.sha256(data).hexdigest()
    limits = campaign["manifest"]["limits"]
    code = code_hash(read_zip(data, max_zip_mb=limits["max_solution_zip_mb"],
                              max_unzipped_mb=limits["max_solution_unzipped_mb"]))

    wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
    hotkey = wallet.hotkey.ss58_address
    upload_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"radiant:{campaign_id}:{hotkey}:{sha}"))  # retries reuse it
    fields = {"campaign_id": campaign_id, "hotkey": hotkey, "sha256": sha, "code_sha256": code,
              "size_bytes": len(data), "upload_id": upload_id, "expires_at": int(time.time()) + 240,
              "network": network, "netuid": netuid}
    subtensor = bt.Subtensor(network=network)
    from .chainview import registered

    if not registered(network, netuid, hotkey):
        raise ValueError(f"hotkey {hotkey} is not registered on netuid {netuid} ({network})")

    if dry_run:
        say(f"dry run: would upload {len(data)} bytes, pay {fee['amount_tao']} {currency}, then commit. Nothing sent.")
        return {"status": "dry_run", "sha256": sha, "code_sha256": code, "hotkey": hotkey}

    signed = {**fields, "signature": wallet.hotkey.sign(upload_message(fields)).hex()}
    presigned = _http("POST", f"{base}/v1/uploads/solution", signed)
    key = presigned["object_key"]
    encode(campaign_id=campaign_id, sha256=sha, code_sha256=code, object_key=key,
           payment_block=2**31 - 1, payment_tx=2**31 - 1)  # raises if the worst case won't fit
    if presigned["already_uploaded"]:
        say("zip already uploaded (a retry); not uploading again")
    else:
        import requests

        r = requests.put(presigned["upload_url"], data=data, headers=presigned["required_headers"], timeout=120)
        if r.status_code not in (200, 201):
            raise ValueError(f"upload failed with HTTP {r.status_code}")  # never print the presigned URL
        say(f"uploaded {len(data)} bytes to {key}")

    if payment_block is not None:
        block, index = payment_block, payment_tx
        say(f"reusing payment {block}-{index}")
        _check_fee_at_block(base, campaign_id, block, fee)
    elif fee_rao > 0:
        fresh = fetch_campaign(campaign_id, api_url=base)["submission_fee"]  # the fee may have changed meanwhile
        if fresh != fee:
            raise ValueError(f"the campaign fee changed to {fresh['amount_tao']} {currency} to {fresh['recipient']} "
                             "while uploading; rerun submit to see and confirm the new fee (nothing was paid)")
        if not yes:
            print(f"Pay {fee['amount_tao']} {currency} to {fee['recipient']} from coldkey {wallet_name}? [y/N] ",
                  end="", file=sys.stderr, flush=True)  # stderr: stdout stays one JSON document
            answer = input()
            if answer.strip().lower() not in ("y", "yes"):
                raise ValueError("cancelled before payment (the upload is kept; rerun to continue)")
        say("unlocking the coldkey to pay the fee")
        result = subtensor.execute(bt.Transfer(dest_ss58=fee["recipient"], amount_tao=fee["amount_tao"]), wallet)
        if not getattr(result, "success", False):
            raise ValueError(f"fee transfer failed: {getattr(result, 'message', None) or result}")
        ref = _payment_ref(result)
        if ref is None:
            raise ValueError("the transfer landed but returned no extrinsic id; find it in a block explorer and "
                             "rerun with --payment-block/--payment-tx")
        block, index = ref
        say(f"paid {fee['amount_tao']} {currency} (payment {block}-{index})")
        _check_fee_at_block(base, campaign_id, block, fee)
    else:
        block = index = None

    payload = encode(campaign_id=campaign_id, sha256=sha, code_sha256=code, object_key=key,
                     payment_block=block, payment_tx=index)
    call = bt.calls.Commitments.set_commitment(netuid=netuid, info={"fields": plaintext_fields(payload)})
    result = bt.Subtensor(network=network, policy=bt.Policy(allow_raw_calls=True)).submit_call(
        call, wallet, signer="hotkey")
    if not getattr(result, "success", False):
        hint = f" Reuse the payment with --payment-block {block} --payment-tx {index}." if block else ""
        raise ValueError(f"commitment rejected: {getattr(result, 'message', None) or result}.{hint}")
    say(f"committed on netuid {netuid}: {payload}")
    intake = wait_for_intake(base, sha, wait_s) if wait_s > 0 else None
    return {"status": "committed", "intake": intake, "sha256": sha, "code_sha256": code, "object_key": key,
            "payment": None if block is None else f"{block}-{index}", "commitment": payload,
            "status_url": f"{base}/v1/submissions/{sha}"}


def wait_for_intake(base: str, sha: str, wait_s: float, every_s: float = 12.0) -> dict[str, Any] | None:
    """Poll /v1/submissions until the watcher queues or rejects this zip, or the wait ends."""
    deadline = time.monotonic() + wait_s
    say(f"waiting up to {wait_s:.0f}s for the validator's watcher to pick it up")
    while time.monotonic() < deadline:
        try:
            body = _http("GET", f"{base}/v1/submissions/{sha}")
        except ValueError:
            body = None  # 404 until the watcher has seen it
        if body and (body.get("submissions") or body.get("rejections")):
            for sub in body.get("submissions", []):
                say(f"accepted as submission {sub['submission_id']} ({sub['state']}); the gates run next")
            for rej in body.get("rejections", []):
                say(f"rejected at intake: {rej['reason']}")
            return body
        time.sleep(every_s)
    say("not picked up yet; check later with `radiant-sdk status`")
    return None
