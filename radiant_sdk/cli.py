"""radiant-sdk: lock | prepare | validate | submit | status.

    radiant-sdk lock requirements.in --out requirements.lock
    radiant-sdk prepare ./my-miner --out build
    radiant-sdk validate build/solution.zip --campaign testnet-cxr-01 [--api-url URL | --manifest FILE]
    radiant-sdk submit build/solution.zip --campaign testnet-cxr-01
    radiant-sdk status <sha256>

``submit`` checks the validation receipt before it does anything else: it
refuses to presign, upload or pay unless a passing receipt for this exact
zip, campaign manifest and SDK version is under 24 hours old. Then it
uploads, pays the fee from the coldkey and commits with the hotkey.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import SDK_VERSION
from . import reasons as R


def _miner_env(path: str | None) -> None:
    """Load miner.env (RADIANT_API_URL, wallet, network) without overriding the environment."""
    p = Path(path or "miner.env")
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = (s.strip() for s in line.split("=", 1))
            os.environ.setdefault(k, v)


def cmd_prepare(args) -> int:
    from .packaging import prepare

    result = prepare(Path(args.src), Path(args.out))
    print(json.dumps({"zip": result.zip_path, "sha256": result.sha256, "code_sha256": result.code_sha256,
                      "files": result.files, "skipped": result.skipped}, indent=2))
    return 0


def cmd_lock(args) -> int:
    from .lock import main as lock_main

    return lock_main(args.requirements, args.out)


def cmd_validate(args) -> int:
    from .validate import ValidateOptions, validate

    opts = ValidateOptions(campaign_id=args.campaign, api_url=args.api_url,
                           manifest_file=Path(args.manifest) if args.manifest else None,
                           modal_environment=args.modal_environment, static_only=args.static_only,
                           gpu_wait_s=args.gpu_wait)
    result = validate(Path(args.zip), opts)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_submit(args) -> int:
    from .validate import check_receipt, fetch_campaign

    if args.manifest:  # offline check only: is there a receipt for this zip?
        campaign = fetch_campaign(args.campaign, manifest_file=Path(args.manifest))
        try:
            receipt = check_receipt(Path(args.zip), args.campaign, campaign["manifest_hash"])
        except ValueError as exc:
            print(f"radiant-sdk submit: refused: {exc}", file=sys.stderr)
            return 2
        print(f"radiant-sdk submit: receipt OK ({receipt['validated_at']}, {receipt['sha256']}); "
              "submitting needs --api-url, not --manifest. Nothing was sent.", file=sys.stderr)
        return 3
    from .submit import submit

    try:
        result = submit(Path(args.zip), args.campaign, api_url=args.api_url,
                        network=args.network or os.environ.get("RADIANT_NETWORK", "test"),
                        netuid=int(args.netuid or os.environ.get("RADIANT_NETUID", "292")),
                        wallet_name=args.wallet_name or os.environ.get("WALLET_NAME", "default"),
                        wallet_hotkey=args.wallet_hotkey or os.environ.get("WALLET_HOTKEY", "default"),
                        yes=args.yes, dry_run=args.dry_run, max_fee_tao=args.max_fee_tao,
                        payment_block=args.payment_block, payment_tx=args.payment_tx, wait_s=args.wait)
    except ValueError as exc:
        print(f"radiant-sdk submit: refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


def cmd_status(args) -> int:
    from .submit import _http
    from .validate import _check_url

    base = _check_url(args.api_url or os.environ.get("RADIANT_API_URL", ""))
    sha = args.sha256 if args.sha256.startswith("sha256:") else "sha256:" + args.sha256
    try:
        print(json.dumps(_http("GET", f"{base}/v1/submissions/{sha}"), indent=2))
    except ValueError as exc:
        print(f"radiant-sdk status: {exc}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="radiant-sdk", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"radiant-sdk {SDK_VERSION}")
    p.add_argument("--miner-env", help="miner settings file (default ./miner.env)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("prepare", help="clean solution directory -> deterministic solution.zip")
    sp.add_argument("src")
    sp.add_argument("--out", default="build")
    sp.set_defaults(fn=cmd_prepare)

    sp = sub.add_parser("lock", help="requirements.in -> hash-locked requirements.lock for the sandbox")
    sp.add_argument("requirements", nargs="?", default="requirements.in")
    sp.add_argument("--out", default="requirements.lock")
    sp.set_defaults(fn=cmd_lock)

    for name, fn, helptext in (("validate", cmd_validate, "run the validator's checks and an offline Modal self-test"),
                               ("submit", cmd_submit, "upload, pay and commit (needs a fresh receipt)")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("zip")
        sp.add_argument("--campaign", required=True)
        sp.add_argument("--api-url", default=None, help="default: RADIANT_API_URL")
        sp.add_argument("--manifest", help="a saved GET /v1/campaigns/{id} response, instead of the API")
        if name == "submit":
            sp.add_argument("--network", help="default: RADIANT_NETWORK from miner.env (test)")
            sp.add_argument("--netuid", help="default: RADIANT_NETUID from miner.env (292)")
            sp.add_argument("--wallet-name", help="default: WALLET_NAME from miner.env")
            sp.add_argument("--wallet-hotkey", help="default: WALLET_HOTKEY from miner.env")
            sp.add_argument("--yes", action="store_true", help="pay without the confirmation prompt")
            sp.add_argument("--dry-run", action="store_true", help="check everything, send nothing")
            sp.add_argument("--max-fee-tao", help="refuse if the campaign fee is higher")
            sp.add_argument("--payment-block", type=int, help="reuse a fee already paid (after a failed commit)")
            sp.add_argument("--payment-tx", type=int)
            sp.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                            help="after committing, wait this long for the validator to queue or reject it")
        if name == "validate":
            sp.add_argument("--modal-environment", default=None)
            sp.add_argument("--static-only", action="store_true", help="skip Hugging Face and Modal")
            sp.add_argument("--gpu-wait", type=float, default=900.0,
                            help="seconds to wait for Modal GPU capacity before giving up (default 900)")
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("status", help="submission state and gate log")
    sp.add_argument("sha256")
    sp.add_argument("--api-url", default=None)
    sp.set_defaults(fn=cmd_status)

    args = p.parse_args(argv)
    _miner_env(args.miner_env)
    try:
        return args.fn(args)
    except R.SolutionRejected as exc:
        print(json.dumps({"status": "rejected", "reason_code": exc.code, "detail": exc.detail}, indent=2))
        print(f"radiant-sdk: rejected: {exc.code}: {exc.detail}", file=sys.stderr)
        return 1
    except R.InfraUnavailable as exc:
        print(json.dumps({"status": "infra_unavailable", "reason_code": exc.code, "detail": exc.detail}, indent=2))
        print(f"radiant-sdk: not your solution's fault: {exc.detail}", file=sys.stderr)
        return 4
    except ValueError as exc:
        print(f"radiant-sdk: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
