"""Copy Secrets Manager secrets from the isol8/ name prefix to lightbulb/.

WHY A COPY AND NOT A RENAME
---------------------------
Secrets Manager has no rename. A "rename" is create-new + repoint + delete-old,
and the repoint here is a code deploy, so the two must be ordered:

    1. RUN THIS (--apply).  Both prefixes now hold the same values. The running
       backend still reads isol8/* and is completely unaffected.
    2. Deploy the writer flip.  The backend now reads and writes lightbulb/*,
       which this script has already populated.
    3. RUN THIS AGAIN (--apply --only-missing) to sweep anything written to the
       old prefix in the gap between 1 and 2.
    4. --verify, then delete the isol8/* secrets.

Doing it in that order is what removes the need for any dual-read fallback in
the backend: at no point does a live code path look for a name that does not
exist. Deploying the flip FIRST would strand every existing credential.

SAFETY
------
* Only ever CREATEs or overwrites DESTINATION secrets. Never deletes, never
  touches a source.
* NO SECRET VALUE IS PRINTED. Comparison is a truncated SHA-256 of the bytes.
* Reads and compares as BYTES, so a SecretString and a SecretBinary holding the
  same material compare equal rather than silently differing.
* Copies the source's KMS key so every existing kms:Decrypt grant — which is
  key-ARN-scoped, not name-scoped — already covers the destination.

USAGE
-----
    cd apps/backend
    uv run python scripts/migrate_secret_prefix.py --env prod            # plan
    uv run python scripts/migrate_secret_prefix.py --env prod --apply
    uv run python scripts/migrate_secret_prefix.py --env prod --verify   # exit 1 on any mismatch
    uv run python scripts/migrate_secret_prefix.py --env prod --apply --only-missing

Check $? UNPIPED — piping through head/tail gives you the pipe's exit code, not
the verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import sys

import boto3
from botocore.exceptions import ClientError

OLD_PREFIX = "isol8/"
NEW_PREFIX = "lightbulb/"


def _h(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()[:16]


def _read(sm, name: str) -> tuple[bytes, str | None] | None:
    """``(value_bytes, kms_key_id)``, or None when absent/scheduled for deletion."""
    try:
        r = sm.get_secret_value(SecretId=name)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ResourceNotFoundException", "InvalidRequestException"):
            return None
        raise
    value = r["SecretString"].encode() if "SecretString" in r else r["SecretBinary"]
    try:
        kms = sm.describe_secret(SecretId=name).get("KmsKeyId")
    except ClientError:
        kms = None
    return value, kms


def _sources(sm, env: str) -> list[str]:
    prefix = f"{OLD_PREFIX}{env}/"
    names: list[str] = []
    for page in sm.get_paginator("list_secrets").paginate(Filters=[{"Key": "name", "Values": [prefix]}]):
        for s in page.get("SecretList", []):
            # The server-side "name" filter is a fuzzy prefix match; the explicit
            # startswith is what keeps a sibling env out of a prod run.
            if s["Name"].startswith(prefix):
                names.append(s["Name"])
    return sorted(names)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, choices=["dev", "prod"])
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--apply", action="store_true", help="write; default is a read-only plan")
    ap.add_argument("--verify", action="store_true", help="hash-compare only; exit 1 on any mismatch")
    ap.add_argument("--only-missing", action="store_true", help="skip destinations that already exist")
    args = ap.parse_args()

    sm = boto3.client("secretsmanager", region_name=args.region)
    names = _sources(sm, args.env)
    print(f"{len(names)} secrets under {OLD_PREFIX}{args.env}/\n")

    stats = {"same": 0, "created": 0, "updated": 0, "drift": 0, "missing": 0, "skipped": 0, "error": 0}

    for src in names:
        dst = NEW_PREFIX + src[len(OLD_PREFIX) :]
        try:
            s = _read(sm, src)
            if s is None:
                print(f"  ERROR   {src}  source unreadable")
                stats["error"] += 1
                continue
            src_val, src_kms = s
            d = _read(sm, dst)
            dst_val = d[0] if d else None

            if dst_val is not None and dst_val == src_val:
                print(f"  same    {dst}  sha={_h(src_val)}")
                stats["same"] += 1
                continue
            if dst_val is not None and args.only_missing:
                stats["skipped"] += 1
                continue
            if args.verify:
                state = "MISSING" if dst_val is None else "DRIFT"
                print(f"  {state:7} {dst}  src={_h(src_val)} dst={'-' if dst_val is None else _h(dst_val)}")
                stats["missing" if dst_val is None else "drift"] += 1
                continue
            if not args.apply:
                print(f"  {'create' if dst_val is None else 'update'}  {dst}  src={_h(src_val)}")
                stats["created" if dst_val is None else "updated"] += 1
                continue

            if dst_val is None:
                kw: dict = {"Name": dst, "SecretString": src_val.decode()}
                if src_kms:
                    kw["KmsKeyId"] = src_kms
                sm.create_secret(**kw)
                print(f"  created {dst}  sha={_h(src_val)}")
                stats["created"] += 1
            else:
                sm.put_secret_value(SecretId=dst, SecretString=src_val.decode())
                print(f"  updated {dst}  sha={_h(src_val)}")
                stats["updated"] += 1
        except ClientError as e:
            print(f"  ERROR   {dst}  {e.response['Error']['Code']}")
            stats["error"] += 1

    print("\n" + "  ".join(f"{k}={v}" for k, v in stats.items() if v))
    bad = stats["error"] + (stats["drift"] + stats["missing"] if args.verify else 0)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
