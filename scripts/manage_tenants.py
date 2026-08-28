# Add, list, or remove a client's API key, and manage which knowledge bases
# (datasets) they can query -- the whole point of this script is that
# onboarding a new tenant, or giving an existing one another dataset, never
# requires touching code or hand-editing JSON. It reads and writes
# config/tenants.json directly (see app/config.py for how the running app
# loads that same file). The app must be restarted to pick up changes, since
# settings are only read once at startup.
#
# Usage:
#   python scripts/manage_tenants.py add --name "Acme Corp" --gemini-key AIza...
#   python scripts/manage_tenants.py add-knowledge-base acme_corp --id northwind --label "Northwind"
#   python scripts/manage_tenants.py list
#   python scripts/manage_tenants.py rotate acme_corp
#   python scripts/manage_tenants.py remove acme_corp
import argparse
import json
import re
import secrets
import sys
from pathlib import Path

CONFIG_PATH = Path("config/tenants.json")


def load() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save(tenants: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(tenants, indent=2) + "\n", encoding="utf-8")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "tenant"


def mask(api_key: str) -> str:
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:4]}...{api_key[-4:]}"


def find_by_tenant_id(tenants: dict, tenant_id: str):
    for api_key, cfg in tenants.items():
        if cfg["tenant_id"] == tenant_id:
            return api_key, cfg
    return None


def cmd_add(args: argparse.Namespace) -> None:
    tenants = load()
    tenant_id = args.tenant_id or slugify(args.name)

    if find_by_tenant_id(tenants, tenant_id):
        print(f"A tenant with tenant_id '{tenant_id}' already exists. Use --tenant-id to pick a different one, "
              f"or `remove` the existing one first.", file=sys.stderr)
        sys.exit(1)

    api_key = args.api_key or secrets.token_urlsafe(32)
    tenants[api_key] = {
        "tenant_id": tenant_id,
        "gemini_api_key": args.gemini_key,
        "knowledge_bases": [{"id": tenant_id, "label": args.name}],
    }
    save(tenants)

    print(f"Added tenant '{tenant_id}' with knowledge base '{tenant_id}' ({args.name}).")
    print()
    print(f"  API key: {api_key}")
    print()
    print("Give this key to the client -- it will not be shown again by `list`.")
    print("Restart the API for this change to take effect.")


def cmd_add_knowledge_base(args: argparse.Namespace) -> None:
    tenants = load()
    match = find_by_tenant_id(tenants, args.tenant_id)
    if not match:
        print(f"No tenant found with tenant_id '{args.tenant_id}'.", file=sys.stderr)
        sys.exit(1)
    _api_key, cfg = match

    if any(kb["id"] == args.id for kb in cfg["knowledge_bases"]):
        print(f"Tenant '{args.tenant_id}' already has a knowledge base '{args.id}'.", file=sys.stderr)
        sys.exit(1)

    cfg["knowledge_bases"].append({"id": args.id, "label": args.label})
    save(tenants)
    print(f"Added knowledge base '{args.id}' ({args.label}) to tenant '{args.tenant_id}'.")
    print("Restart the API for this change to take effect.")


def cmd_list(args: argparse.Namespace) -> None:
    tenants = load()
    if not tenants:
        print("No tenants configured yet. Add one with `add --name ... --gemini-key ...`.")
        return
    for api_key, cfg in tenants.items():
        kb_desc = ", ".join(f"{kb['id']} ({kb['label']})" for kb in cfg["knowledge_bases"])
        print(f"{cfg['tenant_id']:20s} key={mask(api_key)}  gemini_key={mask(cfg['gemini_api_key'])}")
        print(f"{'':20s} knowledge bases: {kb_desc}")


def cmd_rotate(args: argparse.Namespace) -> None:
    """Replaces a tenant's API key in place, keeping every other field
    (tenant_id, gemini_api_key, knowledge_bases) exactly as-is -- the tenant
    config is stored keyed BY its api key (see cmd_add), so rotating means
    moving the same config dict to a new key and dropping the old one, not
    editing any of the config itself. The old key stops authenticating the
    moment this is saved; there's no overlap window, so line up the client
    getting the new key with this if that matters."""
    tenants = load()
    match = find_by_tenant_id(tenants, args.tenant_id)
    if not match:
        print(f"No tenant found with tenant_id '{args.tenant_id}'.", file=sys.stderr)
        sys.exit(1)
    old_key, cfg = match
    new_key = args.api_key or secrets.token_urlsafe(32)
    if new_key == old_key:
        print("New API key is identical to the current one -- nothing to do.", file=sys.stderr)
        sys.exit(1)
    del tenants[old_key]
    tenants[new_key] = cfg
    save(tenants)

    print(f"Rotated the API key for tenant '{args.tenant_id}'. The old key no longer authenticates.")
    print()
    print(f"  New API key: {new_key}")
    print()
    print("Give this key to the client -- it will not be shown again by `list`.")
    print("For production: re-run the deploy script (it re-exports TENANT_API_KEYS from this same")
    print("file), not just a local restart -- Azure won't see this change otherwise.")


def cmd_remove(args: argparse.Namespace) -> None:
    tenants = load()
    match = find_by_tenant_id(tenants, args.tenant_id)
    if not match:
        print(f"No tenant found with tenant_id '{args.tenant_id}'.", file=sys.stderr)
        sys.exit(1)
    api_key, _cfg = match
    del tenants[api_key]
    save(tenants)
    print(f"Removed tenant '{args.tenant_id}'. Restart the API for this change to take effect.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Add a new tenant and generate their API key")
    p_add.add_argument("--name", required=True, help="Human-readable client name, e.g. \"Acme Corp\"")
    p_add.add_argument("--gemini-key", required=True, help="The client's own Gemini API key")
    p_add.add_argument("--tenant-id", help="Override the auto-generated tenant_id (slug of --name)")
    p_add.add_argument("--api-key", help="Override the auto-generated API key (random by default)")
    p_add.set_defaults(func=cmd_add)

    p_add_kb = sub.add_parser("add-knowledge-base", help="Give an existing tenant another dataset to query")
    p_add_kb.add_argument("tenant_id")
    p_add_kb.add_argument("--id", required=True, help="group_id for this dataset, e.g. \"northwind\"")
    p_add_kb.add_argument("--label", required=True, help="Human-readable name shown in a picker, e.g. \"Northwind\"")
    p_add_kb.set_defaults(func=cmd_add_knowledge_base)

    p_list = sub.add_parser("list", help="List configured tenants and their knowledge bases (keys shown masked)")
    p_list.set_defaults(func=cmd_list)

    p_rotate = sub.add_parser("rotate", help="Replace a tenant's API key, keeping their config as-is")
    p_rotate.add_argument("tenant_id")
    p_rotate.add_argument("--api-key", help="Override the auto-generated replacement key (random by default)")
    p_rotate.set_defaults(func=cmd_rotate)

    p_remove = sub.add_parser("remove", help="Remove a tenant by tenant_id")
    p_remove.add_argument("tenant_id")
    p_remove.set_defaults(func=cmd_remove)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
