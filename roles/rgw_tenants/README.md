# rgw_tenants

Provision OIDC-federated, per-tenant S3 on Ceph RGW. Per tenant it creates:

- an RGW account (fixed id);
- an account-root user (creates buckets; never handed to end users);
- a registered OIDC provider (via the IAM API;
  squid has no working `radosgw-admin oidc-provider`);
- the per-tier web-identity IAM roles (`<tenant>-admin/writer/reader`),
  with trust conditions on the token's `org`/`tier` claims and a
  permission policy scoped to `arn:aws:s3:::<tenant>-*`;
- the tenant's buckets.

Requires `rgw_sts` first. The role asserts `rgw_s3_auth_use_sts == true`
and waits for RGW to answer before its boto3 steps. A wrong play order or an
in-flight RGW restart fails or blocks with a clear message.
Runs on `hosts: mons`. Ceph work runs on the bootstrap host via `cephadm shell`.
`meta/argument_specs.yml` and per-tenant asserts validate inputs.

## Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `rgw_tenants_list` | `[]` | Tenants: `{ name, account_id, state?, buckets?, quota?, limits?, root_user?, rotate_keys?, purge_objects?, bucket_prefix? }`. |
| `rgw_tenants_endpoint` | `""` | RGW S3/IAM/STS endpoint reachable from the bootstrap host. |
| `rgw_tenants_oidc` | `{provider_url,client_ids,thumbprints}` | The IdP realm to federate. Thumbprint is the SHA1 of the JWKS signing cert (see collection README recipe). |
| `rgw_tenants_tiers` | admin/writer/reader | Tier to S3 actions. |
| `rgw_tenants_rotate_keys` | `false` | Global gate: roll every tenant's account-root keys to the vaulted value. |
| `rgw_tenants_prune_roles` | `false` | Global gate: delete `<name>-<tier>` roles whose tier left `rgw_tenants_tiers`. |
| `rgw_tenants_service_name` | `rgw.rgw` | cephadm RGW service to restart on OIDC change. |
| `rgw_tenants_bootstrap_host` | `groups['mons'][0]` | Host that runs the ceph commands. |
| `rgw_tenants_fsid` | `""` | Empty discovers it via `ceph fsid`. |

The role converges on a schedule (e.g. hourly).
Every task is idempotent and reports `changed` only on real drift.
The two destructive operations, key rotation and role pruning, are gated off.
A routine run never rotates a live key or deletes a role.

Vault, per tenant: `vault_rgw_tenants_<name>_access_key` / `_secret_key`
(asserted defined before provisioning).

## Per-tenant item schema

```yaml
- name: acme                          # slug: account-root uid + default bucket prefix; must be [a-z0-9]+
  account_id: "RGW00000000000000001"  # "RGW" + 17 digits, unique and stable
  state: present                      # optional; "absent" decommissions (see below)
  buckets: ["acme-data", "acme-logs"] # optional; each must start with the prefix
  quota:                              # optional storage cap; omit for unlimited
    enabled: true                     #   enforce it (false = tracked but not enforced)
    max_size: "{{ 100 * 1024 ** 3 }}" #   BYTES or -1; let kayobe do the GiB math
    max_objects: -1                   #   count or -1
  limits:                             # optional entity cap; omit for RGW defaults
    max_users: 10                     #   or -1 = unlimited
    max_buckets: 50                   #   or -1 = unlimited
  root_user: acmeadmin                # optional; default "<name>root" (also [a-z0-9]+)
  rotate_keys: false                  # optional; roll this tenant's keys (gated, see below)
  purge_objects: false                # optional; with state:absent, force-delete non-empty buckets
  bucket_prefix: "acme-"              # optional; default "<name>-" (the tier ARN scope)
```

## Day-to-day operations

| Task | How |
|------|-----|
| **Add** a tenant / bucket / tier | append to `rgw_tenants_list` / `item.buckets` / `rgw_tenants_tiers`, re-run |
| **Storage** increase/decrease/limit | edit `item.quota` (see the quota table below) |
| **Entity** limit (users/buckets) | set `item.limits.max_users` / `max_buckets` (`-1` = unlimited) |
| **Change** tier permissions / trust | edit `rgw_tenants_tiers[].actions`; the permission policy is always re-put |
| **Rotate** account-root keys | change the vaulted keys, set `rgw_tenants_rotate_keys: true` (or `item.rotate_keys`), run, set back to false |
| **Remove** a tier's orphan roles | set `rgw_tenants_prune_roles: true` after dropping the tier, run |
| **Decommission** a tenant | set `item.state: absent`, run with `-e rgw_tenants_allow_teardown=true` (add `item.purge_objects` + `-e rgw_tenants_allow_purge=true` to destroy data) |

### Rotate account-root keys (gated)

The provisioning step never touches an existing user's keys.
A scheduled converge run never rotates a live credential.
To roll a key: update `vault_rgw_tenants_<name>_access_key` / `_secret_key`,
set `rgw_tenants_rotate_keys: true` (or per-tenant `rotate_keys: true`),
run once, then set it back to false. The task reconciles the vaulted key onto
the account-root user and prunes any older access key.
The tenant ends with exactly the vaulted key. It is idempotent while on and
rotates only on drift. Leaving it on is safe but keeps the gate open.

### Decommission a tenant (state: absent)

Decommission is **gated off the scheduled converge**.
Committing `state: absent` does nothing until an operator runs deliberately
with `-e rgw_tenants_allow_teardown=true`. A converge with a pending absent
entry only warns. Destroying data needs a **second** gate: a tenant's
`purge_objects` takes effect only with `-e rgw_tenants_allow_purge=true`.
"Delete the tenant" and "destroy its data" stay two explicit decisions.

Once allowed, `state: absent` removes the tenant idempotently in this order:
buckets, OIDC provider, tier roles, account-root user, account.
This removes what the role provisioned. If the tenant also created its own
buckets/users/roles via self-service, plain `account rm` refuses
(RGW won't drop a non-empty account) and the run fails without deleting anything.
The two purge gates together cascade (`account rm --purge-data`,
which also purges bucket data). A non-empty bucket in `item.buckets` behaves the same way.

Provider and bucket removal authenticate as the tenant's own account-root.
Keep the `vault_rgw_tenants_<name>_*` keys until the run reports all `OK`,
then delete the list entry and the vault keys. Re-runs are safe:
once the account is gone, every teardown step reports `OK`/`ABSENT`
(the OIDC step treats an already-removed account as success).

## Adjust a tenant's quota (increase / decrease / limit)

`quota` controls a tenant's storage ceiling.
Edit the tenant entry and re-run the role (or the `cephadm-tenants` play).
The quota task reads the account's current cap and re-applies only on drift.
Raising or lowering a limit is one idempotent step.

| Goal | Change |
|------|--------|
| Increase | raise `max_size` / `max_objects` |
| Decrease | lower them (data already over the new cap stays; new writes are refused) |
| Unlimited | `max_size: -1` and `max_objects: -1`, or `enabled: false` |
| Track, don't enforce | keep the numbers, set `enabled: false` |

## Preview changes (dry run)

`--check` is not useful here. The provisioning steps are `command` tasks
that skip in check mode. Use the read-only **plan** mode. Per configured tenant,
it reports what a real run would change
(create account/user/role/bucket, move a quota/limit, or decommission),
then ends the play without touching the cluster.

```sh
ansible-playbook cephadm-tenants.yml -e rgw_tenants_plan=true
```

Each tenant prints either `up to date (no changes)` or a list like
`SET quota size 21474836480->32212254720 ...`, `CREATE role acme-reader`,
`DECOMMISSION account ...`. Scope: account, account-root user, storage quota,
entity limits, per-tier roles, and buckets. The OIDC provider and policy bodies
reconcile on apply (they need the account-root keys) and are not previewed.
A tenant with only OIDC/policy drift can still read as up to date.
Run it in CI on a `rgw_tenants_list` change to review the blast radius before applying.

## Usage report (dry run)

Read-only usage-vs-quota for the configured tenants.
Used storage size/objects against each account's quota, a percentage when the
cap is finite, and a list of any tenant at or over 80%.
Needs no vault keys (only `radosgw-admin get/stats`).
Works against tenants whose secrets you don't hold.

```sh
ansible-playbook cephadm-tenants.yml -e rgw_tenants_report=true
```

Example line: `acme (RGW...001): size 12.4 GiB / 500.0 GiB (2.5%); objects 3540 / unlimited`.
Figures come from the cached account stats (no `--sync-stats` recompute).
They are as fresh as the last stats sync.

## Brownfield discovery / adoption

Point the role at a cluster that already has RGW accounts
(not created by this role). It generates ready-to-edit tenant definitions
for them without changing anything. Discovery is **read-only**:
it runs `radosgw-admin ... list/get` only, and its sole writes are stub files on the control host.

```sh
ansible-playbook cephadm-tenants.yml -e rgw_tenants_discover=true
```

For each RGW account it prints a `MANAGED` / `UNMANAGED` line
(managed = the `account_id` is already in `rgw_tenants_list`)
and writes an adoption stub
`rgw_tenants_discover_output_dir/adopt-<account-id>.yml` for every **unmanaged**
account. To adopt one: review/edit the stub, add the account-root's **existing**
S3 keys to your vault as `vault_rgw_tenants_<name>_access_key` / `_secret_key`
(the role reuses them and does not recreate the root user unless you set
`rotate_keys: true`), merge the entry into `rgw_tenants_list`, and run the role normally.
In discover mode the role ends after writing stubs
(no provisioning, no teardown, no STS/OIDC preflight).
It works before any tenant config exists.

| Variable | Default | Meaning |
|---|---|---|
| `rgw_tenants_discover` | `false` | Run read-only discovery instead of provisioning. |
| `rgw_tenants_discover_output_dir` | `{{ playbook_dir }}/rgw-tenants-discovery` | Where adoption stubs are written. |

## Idempotency notes

- IAM roles are provisioned via `files/manage_role.py`: create if absent,
  `UpdateAssumeRolePolicy` on trust drift, and always (re)put the permission policy.
  Editing a trust policy takes effect.
  A `radosgw-admin get || create` shell path cannot do this. It has no trust-policy update.
- The OIDC helper validates thumbprints (40-hex, non-empty) before any change
  and rolls back to the previous provider if a drift-driven recreate fails.
- Buckets: only `BucketAlreadyOwnedByYou` counts as success.
  `BucketAlreadyExists` (a cross-tenant name collision) surfaces as an error.
- Quota: reads the account's current cap and applies only on drift.
  Reports `changed` only when it actually moved. A blind `quota set` always shows changed.
- Verification: `tasks_from: verify` (read-only radosgw-admin checks) and
  `files/verify_federation.py` (full login, assume, scoped S3, per user).
