#!/usr/bin/env python3
"""Read-only drift preview for the configured tenants. Runs inside `cephadm shell`.

Reads input from a JSON file in the bind-mounted fsid dir
(`/var/run/ceph/plan_input.json` = `{"desired": <rgw_tenants_list>,
"tiers": [names]}`). Not from the environment: `cephadm shell`
does not carry the host env into the container. Inspects the live
cluster and reports, per tenant, what a real `rgw_tenants` run
would change (create an account/user/role/bucket, move a quota
or limit, decommission) and touches nothing.
Every call is a read-only `radosgw-admin ... get/list/info/stats`.

Scope: account, account-root user, storage quota, entity limits,
per-tier roles, buckets. OIDC provider and role trust/permission
policy bodies are reconciled by the boto3 helpers on apply,
which need the account-root keys, and are not previewed here.
A tenant with only OIDC/policy drift can still show "up to date"
in the plan.

Emits `{"tenants": [{"name","account_id","state","present","changes":[...]}], ...}`.
"""
import json
import subprocess


def rgw(*args):
    r = subprocess.run(["radosgw-admin", *args], capture_output=True, text=True)
    try:
        return json.loads(r.stdout) if r.stdout.strip() else None
    except json.JSONDecodeError:
        return None


def _account(aid):
    return rgw("account", "get", "--account-id", aid)


def _role_name(r):
    # `role list` returns AWS-style RoleName on squid. Tolerate other spellings so a schema change yields
    # no name (skipped), not a sorted(None) crash.
    return r.get("RoleName") or r.get("role_name") or r.get("name") or ""


def _roles(aid):
    rl = rgw("role", "list", "--account-id", aid) or []
    return sorted(n for n in (_role_name(r) for r in rl) if n)


def _bucket_exists(name):
    return rgw("bucket", "stats", "--bucket", name) is not None


def _plan_present(t, tiers):
    """Changes a state: present tenant would incur."""
    aid, name = t["account_id"], t["name"]
    changes = []
    acct = _account(aid)
    if acct is None:
        # whole tenant is new: create everything
        changes.append(f"CREATE account {aid} (tenant-{name})")
        changes.append(f"CREATE account-root user {t.get('root_user') or name + 'root'}")
        for tier in tiers:
            changes.append(f"CREATE role {name}-{tier}")
        for b in t.get("buckets", []):
            changes.append(f"CREATE bucket {b}")
        if t.get("quota"):
            changes.append("SET quota")
        if t.get("limits"):
            changes.append("SET limits")
        return changes

    root = t.get("root_user") or name + "root"
    if rgw("user", "info", "--uid", root, "--account-id", aid) is None:
        changes.append(f"CREATE account-root user {root}")

    q = t.get("quota")
    if q is not None:
        cur = acct.get("quota", {})
        want = (str(q.get("max_size", -1)), str(q.get("max_objects", -1)), bool(q.get("enabled", False)))
        have = (str(cur.get("max_size", "?")), str(cur.get("max_objects", "?")), bool(cur.get("enabled", False)))
        if want != have:
            changes.append(f"SET quota size {have[0]}->{want[0]} objs {have[1]}->{want[1]} enabled {have[2]}->{want[2]}")

    lim = t.get("limits")
    if lim is not None:
        want = (str(lim.get("max_users", -1)), str(lim.get("max_buckets", -1)))
        have = (str(acct.get("max_users", "?")), str(acct.get("max_buckets", "?")))
        if want != have:
            changes.append(f"SET limits users {have[0]}->{want[0]} buckets {have[1]}->{want[1]}")

    existing_roles = set(_roles(aid))
    for tier in tiers:
        if f"{name}-{tier}" not in existing_roles:
            changes.append(f"CREATE role {name}-{tier}")

    for b in t.get("buckets", []):
        if not _bucket_exists(b):
            changes.append(f"CREATE bucket {b}")

    return changes


def _plan_absent(t):
    """What a decommission would remove (only if it exists)."""
    aid, name = t["account_id"], t["name"]
    if _account(aid) is None:
        return []  # already gone
    changes = [f"DECOMMISSION account {aid} (tenant-{name})"]
    roles = [r for r in _roles(aid) if r.startswith(f"{name}-")]
    if roles:
        changes.append("DELETE roles " + ", ".join(roles))
    present_buckets = [b for b in t.get("buckets", []) if _bucket_exists(b)]
    if present_buckets:
        verb = "PURGE" if t.get("purge_objects") else "DELETE"
        changes.append(f"{verb} buckets " + ", ".join(present_buckets))
    return changes


_INPUT = "/var/run/ceph/plan_input.json"


def main():
    with open(_INPUT) as fh:
        cfg = json.load(fh)
    desired = cfg.get("desired", [])
    tiers = cfg.get("tiers", [])
    out = []
    for t in desired:
        state = t.get("state", "present")
        changes = _plan_absent(t) if state == "absent" else _plan_present(t, tiers)
        out.append({
            "name": t["name"],
            "account_id": t["account_id"],
            "state": state,
            "present": _account(t["account_id"]) is not None,
            "changes": changes,
        })
    print(json.dumps({"tenants": out}))


if __name__ == "__main__":
    main()
