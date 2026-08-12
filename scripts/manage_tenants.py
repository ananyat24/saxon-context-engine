# Add, list, or remove a client's API key -- the whole point of this script is
# that onboarding a new tenant never requires touching code or hand-editing JSON.
# It reads and writes config/tenants.json directly (see app/config.py for how the
# running app loads that same file). The app must be restarted to pick up changes,
# since settings are only read once at startup.
#
# Usage:
#   python scripts/manage_tenants.py add --name "Acme Corp" --gemini-key AIza...
#   python scripts/manage_tenants.py list
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


def cmd_add(args: argparse.Namespace) -> None:
    tenants = load()
    group_id = args.group_id or slugify(args.name)

    if any(cfg["group_id"] == group_id for cfg in tenants.values()):
        print(f"A tenant with group_id '{group_id}' already exists. Use --group-id to pick a different one, "
              f"or `remove` the existing one first.", file=sys.stderr)
        sys.exit(1)

    api_key = args.api_key or secrets.token_urlsafe(32)
    tenants[api_key] = {"group_id": group_id, "gemini_api_key": args.gemini_key}
    save(tenants)

    print(f"Added tenant '{group_id}'.")
    print()
    print(f"  API key: {api_key}")
    print()
    print("Give this key to the client -- it will not be shown again by `list`.")
    print("Restart the API for this change to take effect.")


def cmd_list(args: argparse.Namespace) -> None:
    tenants = load()
    if not tenants:
        print("No tenants configured yet. Add one with `add --name ... --gemini-key ...`.")
        return
    for api_key, cfg in tenants.items():
        print(f"{cfg['group_id']:30s} key={mask(api_key)}  gemini_key={mask(cfg['gemini_api_key'])}")


def cmd_remove(args: argparse.Namespace) -> None:
    tenants = load()
    matches = [k for k, cfg in tenants.items() if cfg["group_id"] == args.group_id]
    if not matches:
        print(f"No tenant found with group_id '{args.group_id}'.", file=sys.stderr)
        sys.exit(1)
    for k in matches:
        del tenants[k]
    save(tenants)
    print(f"Removed tenant '{args.group_id}'. Restart the API for this change to take effect.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Add a new tenant and generate their API key")
    p_add.add_argument("--name", required=True, help="Human-readable client name, e.g. \"Acme Corp\"")
    p_add.add_argument("--gemini-key", required=True, help="The client's own Gemini API key")
    p_add.add_argument("--group-id", help="Override the auto-generated group_id (slug of --name)")
    p_add.add_argument("--api-key", help="Override the auto-generated API key (random by default)")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="List configured tenants (keys shown masked)")
    p_list.set_defaults(func=cmd_list)

    p_remove = sub.add_parser("remove", help="Remove a tenant by group_id")
    p_remove.add_argument("group_id")
    p_remove.set_defaults(func=cmd_remove)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
