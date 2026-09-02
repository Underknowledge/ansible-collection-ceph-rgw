#!/usr/bin/env python3
"""Idempotently create-or-update a single tenant-tier IAM role via the RGW IAM API.

Replaces the `radosgw-admin role get || role create` shell path.
radosgw-admin has no trust-policy update, so an edited trust doc
never takes effect there. The IAM API has UpdateAssumeRolePolicy:
create if absent, update the trust policy on drift,
and always (re)put the permission policy as an overwrite.

Roles are account-scoped IAM resources, so this runs once per role
as that tenant's account-root credentials. All input comes from
the environment, which keeps secrets and JSON off the command line
and clear of shell quoting:

  IAM_ENDPOINT   RGW IAM endpoint (e.g. http://rgw:8080)
  ROLE_NAME      the <tenant>-<tier> role name
  TRUST_DOC      the assume-role (trust) policy document, as JSON
  PERM_DOC       the permission policy document, as JSON
  POLICY_NAME    permission-policy name (default "tenant")
  AWS_REGION     SigV4 signing region (default us-east-1). RGW ignores it.
  AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   the account-root keys

Prints CREATED / UPDATED-TRUST / OK, then always PERM-PUT.
Exit 0 on success.
"""
import json
import os
import sys
import urllib.parse

import boto3
from botocore.exceptions import ClientError

ENDPOINT = os.environ["IAM_ENDPOINT"]
ROLE_NAME = os.environ["ROLE_NAME"]
TRUST_DOC = os.environ["TRUST_DOC"]
PERM_DOC = os.environ["PERM_DOC"]
POLICY_NAME = os.environ.get("POLICY_NAME", "tenant")
REGION = os.environ.get("AWS_REGION", "us-east-1")


def _canon(doc):
    """Normalise a policy doc (dict, JSON string, or URL-encoded JSON) for comparison."""
    if isinstance(doc, dict):
        return json.dumps(doc, sort_keys=True)
    text = doc
    try:
        return json.dumps(json.loads(text), sort_keys=True)
    except (ValueError, TypeError):
        return json.dumps(json.loads(urllib.parse.unquote(text)), sort_keys=True)


def main():
    iam = boto3.client(
        "iam",
        endpoint_url=ENDPOINT,
        region_name=REGION,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    )
    try:
        cur = iam.get_role(RoleName=ROLE_NAME)["Role"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
            raise
        iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=TRUST_DOC)
        print(f"CREATED {ROLE_NAME}")
    else:
        if _canon(cur.get("AssumeRolePolicyDocument", {})) != _canon(TRUST_DOC):
            iam.update_assume_role_policy(RoleName=ROLE_NAME, PolicyDocument=TRUST_DOC)
            print(f"UPDATED-TRUST {ROLE_NAME}")
        else:
            print(f"OK {ROLE_NAME}")

    # Permission policy: put overwrites, so repeating it is idempotent.
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName=POLICY_NAME, PolicyDocument=PERM_DOC)
    print(f"PERM-PUT {ROLE_NAME}/{POLICY_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
