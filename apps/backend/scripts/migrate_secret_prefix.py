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

Pass 1 covers only the backend-owned prefixes; the CloudFormation-owned static
secrets are copied in a later pass, AFTER their stack has created them at the
new name (CreateSecret on a name that already exists fails the deploy):

    --only user-keys/,integrations/,paperclip-agent-keys/

`paperclip_admin_credentials` is NOT in pass 1. Its VALUE is backend-owned (the
bootstrap script writes it) but its RESOURCE is CloudFormation-owned, and that
is the half that matters here — copying it early is what rolled the dev auth
stack back on 2026-08-29, taking all 17 twins with it. The `_is_cfn_owned`
guard now refuses that create outright, so the rule is enforced rather than
remembered.

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


def _is_cfn_owned(sm, name: str) -> bool:
    """True when CloudFormation owns this secret RESOURCE.

    Load-bearing guard, not a nicety. A CFN-owned source has a twin that CFN
    will create at the destination name, and ``CreateSecret`` on a name that
    already exists FAILS THE DEPLOY and rolls the whole stack back — which then
    deletes every other twin in it. Copying a CFN-owned secret ahead of its
    stack is therefore not "early", it is a stack-wide outage. The destination
    for one of these may only ever be SEEDED after CFN has made it.
    """
    try:
        tags = sm.describe_secret(SecretId=name).get("Tags") or []
    except ClientError:
        return False
    return any(t.get("Key") == "aws:cloudformation:stack-name" for t in tags)


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
    ap.add_argument(
        "--only",
        help=(
            "comma-separated substrings; copy only sources whose name contains one. "
            "The CFN-owned static secrets must NOT be copied ahead of their stack "
            "creating them at the new name — CreateSecret on an existing name fails "
            "the deploy. Pass --only to restrict a run to the backend-owned prefixes."
        ),
    )
    args = ap.parse_args()

    sm = boto3.client("secretsmanager", region_name=args.region)
    names = _sources(sm, args.env)
    if args.only:
        wanted = [t.strip() for t in args.only.split(",") if t.strip()]
        names = [n for n in names if any(t in n for t in wanted)]
    print(f"{len(names)} secrets under {OLD_PREFIX}{args.env}/\n")

    stats = {"same": 0, "created": 0, "updated": 0, "drift": 0, "missing": 0, "skipped": 0, "blocked": 0, "error": 0}

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
            # BEFORE the plan/verify/apply split, so a dry run reports this too
            # — a plan that says "create" for a name CloudFormation is about to
            # create is the exact misreading that caused the rollback.
            if dst_val is None and _is_cfn_owned(sm, src):
                print(f"  BLOCKED {dst}  source is CloudFormation-owned and the twin does not exist yet")
                stats["blocked"] += 1
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
                try:
                    sm.create_secret(**kw)
                    print(f"  created {dst}  sha={_h(src_val)}")
                    stats["created"] += 1
                except sm.exceptions.ResourceExistsException:
                    # The destination secret EXISTS but holds no version, so the
                    # read above came back empty and looked like "absent". A
                    # CloudFormation-minted twin lands in exactly this state when
                    # its construct carries neither an initial value nor a
                    # generate config. Seed it rather than failing the run.
                    sm.put_secret_value(SecretId=dst, SecretString=src_val.decode())
                    print(f"  seeded  {dst}  sha={_h(src_val)}")
                    stats["updated"] += 1
            else:
                sm.put_secret_value(SecretId=dst, SecretString=src_val.decode())
                print(f"  updated {dst}  sha={_h(src_val)}")
                stats["updated"] += 1
        except ClientError as e:
            print(f"  ERROR   {dst}  {e.response['Error']['Code']}")
            stats["error"] += 1

    print("\n" + "  ".join(f"{k}={v}" for k, v in stats.items() if v))
    bad = stats["error"] + stats["blocked"] + (stats["drift"] + stats["missing"] if args.verify else 0)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
