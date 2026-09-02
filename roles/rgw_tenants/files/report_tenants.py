#!/usr/bin/env python3
"""Read-only usage-vs-quota report for the configured tenants. Runs inside `cephadm shell`.

Reads input from `/var/run/ceph/report_input.json` = `{"tenants":
[{"name", "account_id"}]}`. A file, not env: `cephadm shell`
does not carry the host env into the container. For each account,
reads the storage quota (`account get`) and aggregate usage
(`account stats`) and reports used size/objects against the cap,
with a percentage when the cap is finite.
Every call is a read-only `radosgw-admin ... get/stats`.
Account stats are used as cached, with no `--sync-stats` recompute,
so figures are as fresh as the last stats sync.

Emits `{"reports": [{name, account_id, used_size, max_size, size_pct, used_objects,
max_objects, objects_pct, enabled}]}`. `*_pct` is null when the cap is unlimited.
"""
import json
import subprocess

_INPUT = "/var/run/ceph/report_input.json"


def rgw(*args):
    r = subprocess.run(["radosgw-admin", *args], capture_output=True, text=True)
    try:
        return json.loads(r.stdout) if r.stdout.strip() else None
    except json.JSONDecodeError:
        return None


def _pct(used, cap):
    """Percent of a finite, positive cap. None for unlimited (-1/0) or a missing value."""
    try:
        used, cap = int(used), int(cap)
    except (TypeError, ValueError):
        return None
    if cap <= 0:
        return None
    return round(used * 100.0 / cap, 1)


def main():
    with open(_INPUT) as fh:
        tenants = json.load(fh).get("tenants", [])
    reports = []
    for t in tenants:
        aid = t["account_id"]
        acct = rgw("account", "get", "--account-id", aid) or {}
        stats = (rgw("account", "stats", "--account-id", aid) or {}).get("stats", {})
        q = acct.get("quota", {})
        used_size, used_objs = stats.get("size", 0), stats.get("num_objects", 0)
        max_size, max_objs = q.get("max_size", -1), q.get("max_objects", -1)
        reports.append({
            "name": t["name"],
            "account_id": aid,
            "present": bool(acct),
            "enabled": bool(q.get("enabled", False)),
            "used_size": used_size,
            "max_size": max_size,
            "size_pct": _pct(used_size, max_size),
            "used_objects": used_objs,
            "max_objects": max_objs,
            "objects_pct": _pct(used_objs, max_objs),
        })
    print(json.dumps({"reports": reports}))


if __name__ == "__main__":
    main()
