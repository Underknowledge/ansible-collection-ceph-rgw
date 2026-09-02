#!/usr/bin/env python3
"""Idempotently create one tenant's S3 buckets as its account-root.

RGW buckets belong to a user, so they are created with the
tenant's account-root credentials, never end-user keys.
Run once per tenant.

Environment (keeps secrets off the command line):
  S3_ENDPOINT   RGW S3 endpoint (e.g. http://192.168.70.10:8080)
  BUCKETS       comma-separated bucket names for this tenant
  AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   the account-root keys

Exit 0 whether it created each bucket or found it already owned.

CreateBucket is not a reliable existence probe. RGW returns 200
when you re-create a bucket you already own, not
BucketAlreadyOwnedByYou, so keying "changed" off CreateBucket
reports a change every converge.
Probe HeadBucket first and create only on a genuine 404.

AWS_REGION (default us-east-1) is a SigV4 signing placeholder,
not a cloud region. Keep us-east-1 unless a zonegroup api_name
is set. It is the only region for which boto3 omits CreateBucket's
LocationConstraint, and RGW's default zonegroup rejects a mismatch.
"""
import os
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ENDPOINT = os.environ["S3_ENDPOINT"]
BUCKETS = [b.strip() for b in os.environ["BUCKETS"].split(",") if b.strip()]
REGION = os.environ.get("AWS_REGION", "us-east-1")

# HeadBucket outcomes: 404/NoSuchBucket -> create it.
# 403/AccessDenied -> the name exists but belongs to another
# account (names are cluster-global) -> error.
_ABSENT = ("404", "NoSuchBucket")


def main():
    s3 = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        region_name=REGION,
        config=Config(signature_version="s3v4"),
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    )
    changed = False
    for bucket in BUCKETS:
        try:
            s3.head_bucket(Bucket=bucket)
            print(f"EXISTS {bucket}")
            continue
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in _ABSENT:
                # 403 = name held by another account. Anything else is unexpected.
                print(f"ERROR {code} on {bucket}: {exc}", file=sys.stderr)
                return 1
        s3.create_bucket(Bucket=bucket)
        changed = True
        print(f"CREATED {bucket}")
    print("CHANGED" if changed else "OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
