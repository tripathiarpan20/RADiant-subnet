"""radiant-sdk: prepare | validate | submit | status.

    radiant-sdk prepare ./my-miner --out build
    radiant-sdk validate build/solution.zip --campaign testnet-cxr-01 [--api-url URL | --manifest FILE]
    radiant-sdk submit build/solution.zip --campaign testnet-cxr-01
    radiant-sdk status <sha256>

``submit`` checks the validation receipt before it does anything else: it
refuses to presign, upload or pay unless a passing receipt for this exact
zip, campaign manifest and SDK version is under 24 hours old. The upload,
fee payment and on-chain commitment arrive in R3.
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


def cmd_validate(args) -> int:
    from .validate import ValidateOptions, validate

    opts = ValidateOptions(campaign_id=args.campaign, api_url=args.api_url,
                           manifest_file=Path(args.manifest) if args.manifest else None,
                           modal_environment=args.modal_environment, static_only=args.static_only)
    result = validate(Path(args.zip), opts)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_submit(args) -> int:
    from .validate import check_receipt, fetch_campaign

    campaign = fetch_campaign(args.campaign, api_url=args.api_url,
                              manifest_file=Path(args.manifest) if args.manifest else None)
    try:
        receipt = check_receipt(Path(args.zip), args.campaign, campaign["manifest_hash"])
    except ValueError as exc:
        print(f"radiant-sdk submit: refused: {exc}", file=sys.stderr)
        return 2
    print(f"radiant-sdk submit: receipt OK ({receipt['validated_at']}, {receipt['sha256']}).", file=sys.stderr)
    print("radiant-sdk submit: upload, fee payment and on-chain commitment arrive in R3; nothing was sent.",
          file=sys.stderr)
    return 3


def cmd_status(args) -> int:
    print("radiant-sdk status: submission status arrives with the R3 intake API.", file=sys.stderr)
    return 3


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

    for name, fn, helptext in (("validate", cmd_validate, "run the validator's checks and an offline Modal self-test"),
                               ("submit", cmd_submit, "upload, pay and commit (needs a fresh receipt)")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("zip")
        sp.add_argument("--campaign", required=True)
        sp.add_argument("--api-url", default=None, help="default: RADIANT_API_URL")
        sp.add_argument("--manifest", help="a saved GET /v1/campaigns/{id} response, instead of the API")
        if name == "validate":
            sp.add_argument("--modal-environment", default=None)
            sp.add_argument("--static-only", action="store_true", help="skip Hugging Face and Modal")
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("status", help="submission state and gate log")
    sp.add_argument("sha256")
    sp.set_defaults(fn=cmd_status)

    args = p.parse_args(argv)
    _miner_env(args.miner_env)
    try:
        return args.fn(args)
    except R.SolutionRejected as exc:
        print(json.dumps({"status": "rejected", "reason_code": exc.code, "detail": exc.detail}, indent=2))
        print(f"radiant-sdk: rejected: {exc.code}: {exc.detail}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"radiant-sdk: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
