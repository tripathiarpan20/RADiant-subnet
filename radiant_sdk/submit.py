"""``submit``: upload, pay and commit one validated solution (Pareton's fee order).

1. The receipt must match this zip, the campaign's current manifest and the
   SDK version, and be under 24 hours old (checked before anything else).
2. The validator's /v1/info must report the network and netuid in miner.env,
   and the campaign's fee recipient must be the one pinned in this SDK for
   that network, so a wrong RADIANT_API_URL can't redirect a payment.
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

# Fee recipients this SDK will pay, per network. A campaign naming anyone
# else is refused. Mainnet's is set at launch (TODO.md).
TRUSTED_FEE_RECIPIENTS = {
    "test": "5GYfPfFRQPaLytjCyT78X769ZU9xm5tL43dN11sknTiZx34a",  # owner coldkey (rumayao_coldkey), 27 Nov
    "finney": None,
}
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


def submit(zip_path: Path, campaign_id: str, *, api_url: str | None, network: str, netuid: int, wallet_name: str,
           wallet_hotkey: str, yes: bool = False, dry_run: bool = False, max_fee_tao: str | None = None,
           payment_block: int | None = None, payment_tx: int | None = None) -> dict[str, Any]:
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
    trusted = TRUSTED_FEE_RECIPIENTS.get(network)
    if trusted is None or fee["recipient"] != trusted:
        raise ValueError(f"campaign fee recipient {fee['recipient']} is not the one this SDK trusts on {network} "
                         f"({trusted}); refusing to pay")
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
    elif fee_rao > 0:
        if not yes:
            answer = input(f"Pay {fee['amount_tao']} {currency} to {fee['recipient']} from coldkey "
                           f"{wallet_name}? [y/N] ")
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
    return {"status": "committed", "sha256": sha, "code_sha256": code, "object_key": key,
            "payment": None if block is None else f"{block}-{index}", "commitment": payload,
            "status_url": f"{base}/v1/submissions/{sha}"}
