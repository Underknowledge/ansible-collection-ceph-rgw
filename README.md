# underknowledge.ceph_rgw

Native Ceph RGW STS plus OIDC-federated, per-tenant S3 for
kayobe / cephadm deployments. End users hold no long-lived S3 keys.
They log in to an OIDC IdP and get short-lived, tenant- and
tier-scoped S3 credentials via `AssumeRoleWithWebIdentity`.

Two roles:

| Role | What it does |
|------|--------------|
| `rgw_sts` | Enables native RGW STS. Sets `rgw_sts_key` and `rgw_s3_auth_use_sts`. Creates standalone IAM roles (`AssumeRole` / `GetSessionToken`). Restarts RGW on change. |
| `rgw_tenants` | Per tenant: an RGW account, an account-root user (creates buckets, never handed out), a registered OIDC provider, per-tier web-identity IAM roles (`<tenant>-admin/writer/reader`), and buckets. Runs after `rgw_sts`. |

## How a login turns into S3 access

```
  user logs in to the OIDC IdP (realm, client "ceph-s3")
      access token carries: iss, aud=ceph-s3, org=<tenant>, tier=<tier>

  boto3 sts.assume_role_with_web_identity(RoleArn=<tenant>-<tier>, WebIdentityToken=token)

  RGW validates the token against the account's OIDC provider and matches the role
  trust policy (Federated provider, <provider>:org == tenant, <provider>:tier == tier),
  then returns temporary credentials scoped by the permission policy to
  arn:aws:s3:::<tenant>-*

  boto3 s3 with the temporary creds: only this tenant's buckets, only this tier's actions
```

Each tenant is a separate RGW account. A role in one account
cannot reach another account's buckets. The per-tier permission policy
narrows access within the account to a bucket-name prefix
(`<tenant>-*` by default).

## Install (kayobe)

Kayobe pulls this collection from git with `ansible-galaxy`,
the same as the cephadm collection. Add it to
`etc/kayobe/ansible/requirements.yml` under `collections:`
(see [`examples/kayobe-config/ansible/requirements.yml.snippet`](examples/kayobe-config/ansible/requirements.yml.snippet)):

```yaml
collections:
  - name: https://github.com/Underknowledge/ansible-collection-ceph-rgw.git
    type: git
    version: main        # pin to a tag or commit for production
```

Then `kayobe control host bootstrap` installs it.

## Wire into the cephadm chain

1. Copy the two chain plays into `etc/kayobe/ansible/ceph/`:
   [`cephadm-sts.yml`](examples/kayobe-config/ansible/ceph/cephadm-sts.yml),
   [`cephadm-tenants.yml`](examples/kayobe-config/ansible/ceph/cephadm-tenants.yml).
2. Append the two `import_playbook` lines to `etc/kayobe/ansible/ceph/cephadm.yml`
   after `cephadm-commands-post.yml`
   (see [`cephadm.yml.snippet`](examples/kayobe-config/ansible/ceph/cephadm.yml.snippet)).
   Order matters: STS after commands-post, tenants last.
   `rgw_tenants` asserts at runtime that `rgw_s3_auth_use_sts` is already `true`.
   A wrong order fails with a clear message before it breaks the cluster.
3. Add the config to your environment's `cephadm.yml` and vault
   (see [`examples/kayobe-config/environments/example/`](examples/kayobe-config/environments/example/)).

Run this part alone with `kayobe playbook run .../ansible/ceph/cephadm.yml --tags cephadm-sts`
(then `--tags cephadm-tenants`). Each `cephadm shell` call takes roughly 12s.
A full tenant set is around 30 calls. Allow a timeout of ten minutes or more.

## Configuration

Everything is variable-driven (defaults plus `meta/argument_specs.yml` in each role).
Config is role-name-prefixed: `rgw_sts_*` for the STS role,
`rgw_tenants_*` for the tenants role. Vaulted secrets follow the same
prefix (`vault_rgw_sts_*` / `vault_rgw_tenants_*`):

- `rgw_sts_key` from `vault_rgw_sts_key`: STS signing key.
- `rgw_tenants_endpoint`: RGW S3/IAM/STS endpoint the bootstrap host can reach.
- `rgw_tenants_oidc`: `{ provider_url, client_ids, thumbprints }` for the IdP.
- `rgw_tenants_list`: list of `{ name, account_id, buckets?, quota?, limits?, root_user?,
  root_users?, bucket_prefix?, state? }`.
- `rgw_tenants_tiers`: tier to actions (defaults admin/writer/reader).
- `rgw_tenants_default_quota`: fallback `{ enabled, max_size, max_objects }`
  applied to any tenant that omits its own `quota`. Default `{}` (no fallback).
- `rgw_sts_service_name` / `rgw_tenants_service_name`: RGW cephadm service to
  restart (per-role; both default to `rgw.rgw`).
- Vault, per tenant: `vault_rgw_tenants_<name>_access_key` / `_secret_key`
  (the primary account-root), plus
  `vault_rgw_tenants_<name>_root_<slug>_access_key` / `_secret_key` for each extra root user.

`name` must be lowercase `[a-z0-9_]+` (snake_case account name, see *Naming conventions*).
`account_id` must be `RGW` plus 17 digits. Every bucket must start
with the tenant's prefix (`<name>-` by default). The role asserts all of this
before touching the cluster. A typo fails early with the tenant name, before any provisioning.

### Naming conventions

The `rgw_new_tenant` and `rgw_add_root_user` playbooks build every identifier
through the `ceph_naming` filters, so names stay consistent and the STS
trust-policy ARNs keep matching (the user ARN is `<account_id>:user/<display-name>`,
so uid equals display-name):

| Thing | Format | Example |
|-------|--------|---------|
| Account name | `<org>_<purpose>[_<qualifier>]`, lowercase snake_case | `initrode_data_prod`, `globex_data` |
| Primary root | `iam-<account>-root` | `iam-initrode_data_prod-root` |
| Extra root (service/person) | `iam-<account>-root-<descriptor>` | `iam-globex_data-root-backup`, `iam-initrode_data_prod-root-erika_schroeder` |
| Group | `<account>-<role>` (no `iam-` prefix) | `globex_data-uploaders` |

Person names are sanitized to the descriptor slug (German umlauts transliterated
`ö→oe`/`ä→ae`/`ü→ue`/`ß→ss`, other diacritics stripped,
spaces/hyphens folded to `_`), so `"Erika Schröder"` becomes `erika_schroeder`.
Underscore joins words inside one semantic unit; hyphen separates the structural segments of a uid.

Account names get the same sanitizing pass, then a validity check.
`"Acme Data Prod"` and `Acme-Data-Prod` both canonicalize to `acme_data_prod`.
The play then asserts the result is a valid `<org>_<purpose>`, so a single-word
`acme` is rejected (a tenant needs at least two segments). An account name is a
permanent identity: it drives the uid, vault key names, bucket prefix, and role ARNs.
The play warns whenever sanitizing changed your input.
Re-run with the exact name if the canonical form is not what you meant.

## Add a new tenant

The role treats the vault as authoritative (it never generates account-root keys
or writes anything back), so a new tenant's identity and keys must exist in
config *before* it runs. The `rgw_new_tenant` playbook mints that once and stops,
changing nothing on any cluster. Run it bare and it prompts for the account name;
or pass it with `-e` (which skips the prompt):

```sh
# Uses your existing vault password (ANSIBLE_VAULT_PASSWORD_FILE, or -e
# rgw_new_tenant_vault_password_file=<path>, e.g. kayobe's vault helper).
ansible-playbook playbooks/rgw_new_tenant.yml -e rgw_new_tenant=initrode_web
```

It allocates an `account_id` (a Unix-time id: `RGW` + `date +%s%N` truncated to 17 digits,
sortable, collision-free, unpadded; or pass `-e rgw_new_tenant_account_id=RGW<17 digits>`),
mints an `IAM`-prefixed 20-char access key and a 40-char secret key
from `random.SystemRandom`, and writes one self-contained drop-in file
`<tenant>.yml` into `rgw_new_tenant_output_dir` (default `./rgw_tenants.d`).
It always emits the primary root `iam-<tenant>-root`:

```yaml
# rgw_tenants.d/initrode_web.yml: config and vaulted keys, together
rgw_tenant_initrode_web:
  name: initrode_web
  account_id: "RGW17887187248433597"
  root_user: iam-initrode_web-root
  buckets: [initrode_web-data, initrode_web-logs]
vault_rgw_tenants_initrode_web_access_key: !vault |   # encrypted with your vault password
  $ANSIBLE_VAULT;1.1;AES256;...
vault_rgw_tenants_initrode_web_secret_key: !vault |
  $ANSIBLE_VAULT;1.1;AES256;...
```

The `IAM` prefix makes the access key recognizable in a request log.
The remaining 17 chars keep the entropy.
Add `-e '{"rgw_new_tenant_quota": {...}}'`, `-e '{"rgw_new_tenant_buckets":
[...]}'`, or `-e '{"rgw_new_tenant_root_users": ["backup", "Erika Schröder"]}'`
to seed those keys (all optional; a bare run gives an account plus its primary root).
Point the role's `rgw_tenants_dir` at that directory and it loads every file,
appending each to `rgw_tenants_list`. Dropping a file in adds a tenant.
Deleting it removes one. Review, commit, then run `rgw_tenants` (`cephadm-tenants.yml`).
Re-running for an existing tenant is refused unless `-e rgw_new_tenant_force=true`
(which mints *new* keys).

## Root users (day-2)

A tenant's primary `iam-<tenant>-root` is created by `rgw_new_tenant`.
Beyond it, each account can carry additional **account-root users**:
personalised keys handed to a tenant operator to self-serve buckets for their
own tenant, added and removed over the tenant's life. They live in the same
drop-in, as a `root_users` list plus their own vaulted keys.
Buckets are account-owned (not owned by the creating uid), so a root user can
be added or removed without touching any tenant data.

**Add one.** The `rgw_add_root_user` playbook mints the key and merges it into
the drop-in, leaving every existing key and `!vault` block byte-for-byte intact.
Run it bare and it prompts for the tenant and the user (type the person's real
name at the prompt, spaces are fine, no quoting):

```sh
ansible-playbook playbooks/rgw_add_root_user.yml
#  Existing RGW tenant account: initrode_web
#  New root user, person name or service label: Erika Schröder
```

Or drive it non-interactively. A name with spaces then needs JSON
(Ansible's `-e k=v` form splits on spaces):

```sh
ansible-playbook playbooks/rgw_add_root_user.yml \
  -e rgw_add_root_user_tenant=initrode_web -e '{"rgw_add_root_user": "Erika Schröder"}'
```

`rgw_add_root_user` is any person name or service label; it is sanitized to the
descriptor slug, so the uid becomes `iam-initrode_web-root-erika_schroeder`.
Re-running with the same user is an idempotent no-op. This appends to the drop-in:

```yaml
rgw_tenant_initrode_web:
  # ...
  root_users:
    - name: erika_schroeder
vault_rgw_tenants_initrode_web_root_erika_schroeder_access_key: !vault | ...
vault_rgw_tenants_initrode_web_root_erika_schroeder_secret_key: !vault | ...
```

Review, commit, run `rgw_tenants`. The role creates the account-root user with those keys.

**Remove or suspend one.** No playbook, a desired-state edit the role reconciles:

- *Remove:* set the entry's `state: absent` (**keep it in the list**),
  run `rgw_tenants` once so it issues `radosgw-admin user rm`
  (gated by `rgw_tenants_allow_root_user_removal`, default `true`),
  *then* delete the entry and its two `vault_..._root_<slug>_*` blocks.
  Deleting the entry first orphans the cluster user, which the role can no longer remove.
- *Suspend:* set `suspended: true` (or back to `false`).
  The role reconciles suspend/enable each converge, keeping the keys but blocking access.

## Verify it worked

Two layers. Run either or both:

- Read-only, no IdP needed. Confirms accounts, per-tier roles, and buckets exist:
  ```sh
  kayobe playbook run .../ansible/ceph/cephadm-tenants-verify.yml
  ```
  (the `rgw_tenants` role's `verify` task set; see the example play).
- Full federation path. Login, assume, scoped S3, with negative checks.
  Run `roles/rgw_tenants/files/verify_federation.py` per user
  (needs `boto3` and the user's IdP password; env-driven, see its docstring).
  It checks the tier's allowed op and the reader deny.
  When `CROSS_BUCKET` names another tenant's bucket, it checks the account
  isolation boundary holds: that bucket must be unreachable.

## Compute the OIDC thumbprint

This value is a common source of misconfiguration. It is the SHA1 of the
JWKS signing cert (the `use=sig` key's `x5c`), not the IdP TLS chain.
The example ships a `CHANGEME` sentinel that the role refuses to register.
Compute the real one with:

```sh
curl -s https://idp.example.com/realms/myrealm/protocol/openid-connect/certs \
  | python3 -c 'import sys,json,base64,hashlib; \
      k=[k for k in json.load(sys.stdin)["keys"] if k.get("use")=="sig"][0]; \
      der=base64.b64decode(k["x5c"][0]); print(hashlib.sha1(der).hexdigest())'
```

Recompute and re-run whenever the realm signing key rotates.
On drift the OIDC helper deletes and recreates the provider
(RGW has no thumbprint-update operation) and the role restarts RGW.

## Decommission a tenant

Set `state: absent` on the tenant entry and re-run `cephadm-tenants`
(or wait for the next scheduled converge). The role tears it down idempotently
in this order: buckets, OIDC provider, tier roles, account-root user, account.
It reports `OK` once everything is gone:

```yaml
rgw_tenants_list:
  - name: acme
    account_id: "RGW00000000000000001"
    state: absent
    # purge_objects: true   # only if buckets still hold data (destructive)
    buckets: ["acme-data"]  # keep the bucket list so they get removed
```

This removes what the collection provisioned. If the tenant self-served its own
buckets/users/roles, plain `account rm` refuses on the non-empty account and the
run fails without deleting anything. Set `purge_objects: true` to cascade
(`account rm --purge-data` also purges bucket data). A non-empty bucket in the
list behaves the same way. Keep the entry (with `state: absent`) and its
`vault_rgw_tenants_*` keys until the run reports all `OK`, then delete both.
Provider and bucket removal use the tenant's own account-root keys.
Re-runs after the account is gone still report `OK`/`ABSENT`.

## Operational notes

- The OIDC thumbprint is the SHA1 of the JWKS signing cert (see above), not the TLS chain.
- RGW caches the OIDC provider in memory.
  The role restarts RGW after any provider change. `rgw_tenants` waits for RGW to
  answer before its boto3 steps (the STS role's restart is async).
  A fresh deploy does not race the bounce.
- Account-scoped `radosgw-admin` operations need `--account-id` on every call, or they 404.
- There is no `radosgw-admin` role trust-policy update.
  `rgw_tenants` provisions roles via the IAM API (`manage_role.py`),
  which uses `UpdateAssumeRolePolicy`, so an edited trust policy takes effect.
  A `get || create` shell path cannot update trust.

## License

Apache-2.0.
