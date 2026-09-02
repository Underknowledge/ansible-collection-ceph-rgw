#!/usr/bin/env python3
"""Idempotently register an OIDC realm as an RGW OIDC provider in one account.

Ceph squid has no working `radosgw-admin oidc-provider` command.
OIDC providers are IAM resources created via
CreateOpenIDConnectProvider. The provider is scoped to the calling
credentials' account, so this runs once per tenant with that
tenant's account-root keys. The resulting ARN
(arn:aws:iam::<account>:oidc-provider/<host>/realms/<realm>)
is what each tenant's role trust policy federates.

Config comes from the environment to keep secrets off the command line:
  IAM_ENDPOINT       RGW endpoint (e.g. http://192.168.70.10:8080)
  OIDC_PROVIDER_URL  realm issuer incl. https:// (the token `iss`)
  OIDC_CLIENT_IDS    comma-separated accepted audiences (e.g. ceph-s3)
  OIDC_THUMBPRINTS   comma-separated SHA1 hex fingerprints of the JWKS signing cert
  AWS_REGION         SigV4 signing region (default us-east-1). RGW ignores it.
  AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   the account-root keys

Exit 0 whether it created the provider or found it already present.
An empty or malformed thumbprint set is rejected before any change.
A failed recreate on drift rolls back to the previous provider,
so a bad thumbprint cannot strand a tenant with no provider.

Set STATE=absent to delete the provider (matched by URL)
for tenant decommission. Delete is idempotent: prints DELETED
if it removed one, ABSENT if there was none.
Thumbprints and client ids are ignored in that mode.
"""
import os
import re
import sys

import boto3
from botocore.exceptions import ClientError

# On teardown the account-root creds may already be gone (account removed on a prior converge). An account-scoped
# provider cannot outlive its account, so an auth failure in delete mode means the provider is gone too. Treat as
# success.
_ACCOUNT_GONE = ("InvalidAccessKeyId", "SignatureDoesNotMatch", "AccessDenied", "AccessDeniedException")

ENDPOINT = os.environ["IAM_ENDPOINT"]
URL = os.environ["OIDC_PROVIDER_URL"]
STATE = os.environ.get("STATE", "present")
CLIENT_IDS = [c.strip() for c in os.environ.get("OIDC_CLIENT_IDS", "").split(",") if c.strip()]
THUMBPRINTS = [t.strip().lower() for t in os.environ.get("OIDC_THUMBPRINTS", "").split(",") if t.strip()]
REGION = os.environ.get("AWS_REGION", "us-east-1")

_SHA1_HEX = re.compile(r"^[0-9a-f]{40}$")


def _require_valid_thumbprints():
    """A thumbprint-less or malformed provider is unusable. Refuse before any change."""
    if not THUMBPRINTS:
        sys.exit("OIDC_THUMBPRINTS is empty; refusing to register a thumbprint-less provider.")
    bad = [t for t in THUMBPRINTS if not _SHA1_HEX.match(t)]
    if bad:
        sys.exit(f"OIDC_THUMBPRINTS malformed (need 40-hex SHA1): {bad}. "
                 "Compute from <issuer>/protocol/openid-connect/certs; see the role README.")


def _find_arn(iam):
    """Return the provider ARN matching URL (host+path), or None."""
    suffix = URL.split("://", 1)[-1]
    for p in iam.list_open_id_connect_providers().get("OpenIDConnectProviderList", []):
        if p["Arn"].split(":oidc-provider/", 1)[-1] == suffix:
            return p["Arn"]
    return None


def _create(iam, client_ids, thumbprints):
    arn = iam.create_open_id_connect_provider(
        Url=URL, ClientIDList=client_ids, ThumbprintList=thumbprints
    )["OpenIDConnectProviderArn"]
    return arn


def main():
    if STATE != "absent":
        _require_valid_thumbprints()
    iam = boto3.client(
        "iam",
        endpoint_url=ENDPOINT,
        region_name=REGION,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    )
    if STATE == "absent":
        try:
            arn = _find_arn(iam)
            if arn is None:
                print(f"ABSENT (no provider for {URL})")
                return 0
            iam.delete_open_id_connect_provider(OpenIDConnectProviderArn=arn)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _ACCOUNT_GONE:
                print(f"ABSENT (account for {URL} already gone: {code})")
                return 0
            raise
        print(f"DELETED {arn}")
        return 0

    arn = _find_arn(iam)
    if arn is None:
        new = _create(iam, CLIENT_IDS, THUMBPRINTS)
        print(f"CREATED {new} thumbprints={THUMBPRINTS}")
        return 0

    # RGW has no UpdateOpenIDConnectProviderThumbprint. Changing the thumbprint or client list means delete + recreate.
    # Compare current vs desired (thumbprints case-insensitive) and replace only on drift.
    cur = iam.get_open_id_connect_provider(OpenIDConnectProviderArn=arn)
    prev_tp = cur.get("ThumbprintList", [])
    prev_cid = cur.get("ClientIDList", [])
    if sorted(t.lower() for t in prev_tp) == sorted(THUMBPRINTS) and sorted(prev_cid) == sorted(CLIENT_IDS):
        print(f"EXISTS (current) {arn}")
        return 0

    iam.delete_open_id_connect_provider(OpenIDConnectProviderArn=arn)
    try:
        new = _create(iam, CLIENT_IDS, THUMBPRINTS)
    except Exception as exc:  # noqa: BLE001 - recreate failed; restore the known-good provider
        try:
            _create(iam, prev_cid, prev_tp)
        except Exception as restore_exc:  # noqa: BLE001 - restore failed too; provider now missing
            sys.exit(f"recreate failed ({exc}) and rollback failed ({restore_exc}); "
                     "manual intervention required: tenant left without an OIDC provider.")
        sys.exit(f"recreate failed ({exc}); rolled back to previous provider thumbprints={prev_tp}")
    print(f"REPLACED {new} thumbprints={THUMBPRINTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
