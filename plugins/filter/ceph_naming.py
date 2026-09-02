# Naming filters for ceph_rgw.
#
# Watch out: the user ARN is <account_id>:user/<display-name> and trust-policy Principals are matched against it, so uid and
# display-name have to be the identical Ceph-safe string. Get that wrong and STS AssumeRole silently stops matching. These
# filters are the single place that string gets built, so the role and playbooks stay in sync.
#
#   account name  <org>_<purpose>[_<qualifier>]   lowercase snake_case, ASCII   initrode_data_prod
#   root user     iam-<account>-root[-<descriptor>]                             iam-initrode_data_prod-root-erika_schroeder
#   group         <account>-<role>                 hyphen-separated, no iam-     initrode_data_prod-readers
import re
import unicodedata

# German umlauts go to digraphs (ö->oe), not bare vowels. Map them before the NFKD strip below, which would
# otherwise drop the diaeresis and leave a plain "o".
_GERMAN = {"ö": "oe", "ä": "ae", "ü": "ue", "ß": "ss"}

_ACCOUNT_RE = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)+$")
_DESCRIPTOR_RE = re.compile(r"^[a-z0-9_]+$")


def ceph_descriptor(name):
    """Slug a real name/label down to [a-z0-9_]+.

    Handles umlauts (ö->oe), strips accents (José->jose),
    folds spaces and hyphens to '_'. Hyphens have to go: '-' is
    the field separator in iam-<account>-root-<descriptor>,
    so a hyphen in the descriptor would split the uid.
    Raises if nothing usable survives.
    """
    text = str(name).strip().lower()
    for src, dst in _GERMAN.items():
        text = text.replace(src, dst)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("-", "_").replace(" ", "_")
    text = re.sub(r"[^a-z0-9_]", "", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        raise ValueError("ceph_descriptor: %r sanitizes to an empty string" % (name,))
    return text


def ceph_root_uid(account, descriptor=None):
    """Build the account-root uid iam-<account>-root[-<descriptor>].

    descriptor must already be [a-z0-9_]+ (run person names
    through ceph_descriptor first). Feed the result to both
    --uid and --display-name; they have to match (see file header).
    """
    if not ceph_account_valid(account):
        raise ValueError("ceph_root_uid: %r is not a valid account name" % (account,))
    base = "iam-%s-root" % account
    if descriptor in (None, ""):
        return base
    if not _DESCRIPTOR_RE.match(str(descriptor)):
        raise ValueError("ceph_root_uid: descriptor %r is not [a-z0-9_]+" % (descriptor,))
    return "%s-%s" % (base, descriptor)


def ceph_account_valid(account):
    """True for a lowercase snake_case name with at least two segments (initrode_data_prod)."""
    return bool(_ACCOUNT_RE.match(str(account)))


def ceph_group(account, role):
    """Build a group name <account>-<role>. No iam- prefix
    (groups aren't users). role is lowercased with spaces/underscores
    turned into hyphens (read only users -> ...)."""
    if not ceph_account_valid(account):
        raise ValueError("ceph_group: %r is not a valid account name" % (account,))
    role_slug = re.sub(r"[^a-z0-9-]", "", re.sub(r"[ _]+", "-", str(role).strip().lower()))
    role_slug = re.sub(r"-+", "-", role_slug).strip("-")
    if not role_slug:
        raise ValueError("ceph_group: role %r sanitizes to empty" % (role,))
    return "%s-%s" % (account, role_slug)


class FilterModule:
    """Ceph RGW naming-convention filters."""

    def filters(self):
        return {
            "ceph_descriptor": ceph_descriptor,
            "ceph_root_uid": ceph_root_uid,
            "ceph_account_valid": ceph_account_valid,
            "ceph_group": ceph_group,
        }
