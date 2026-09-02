# rgw_sts

Enable native Ceph RGW STS on a cephadm-deployed cluster.
Sets the session-token signing key (`rgw_sts_key`),
turns on STS auth for the S3 endpoint (`rgw_s3_auth_use_sts`),
and creates standalone IAM roles for `AssumeRole` / `GetSessionToken`.
RGW restarts when the startup-read config changes.

OIDC web-identity federation is handled by the `rgw_tenants` role,
which registers providers via the IAM API.
Squid's `radosgw-admin oidc-provider` does not work for this.

Runs on `hosts: mons`.
All `radosgw-admin`/`ceph` work runs on the bootstrap host
(`rgw_sts_bootstrap_host`, default `groups['mons'][0]`) via `cephadm shell`.
`meta/argument_specs.yml` validates inputs.

## Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `rgw_sts_key` | `""` | STS signing key (from vault, `vault_rgw_sts_key`). Empty skips it and leaves Ceph's insecure default. |
| `rgw_sts_roles` | `[]` | Standalone IAM roles: `{ name, account_id?, trust_policy, policies: [{name, document}] }`. |
| `rgw_sts_service_name` | `rgw.rgw` | cephadm RGW service to restart on change. |
| `rgw_sts_bootstrap_host` | `groups['mons'][0]` | Host that runs the ceph commands. |
| `rgw_sts_fsid` | `""` | Empty discovers it via `ceph fsid`. |

## Notes

- RGW reads `rgw_sts_key` and `rgw_s3_auth_use_sts` at startup.
  The role restarts RGW when either changes.
  The restart is async and settles in a few seconds.
- Create tasks report `changed` on first run only. They echo `CREATED`.

Run this as its own play (see the collection README) before `rgw_tenants`.
