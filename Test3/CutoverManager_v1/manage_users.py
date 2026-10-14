"""
Manage who can log in to the SAP BTP-IS Cutover Automation & Management
Platform. This is the ONLY place users are added, changed, or removed --
deliberately not exposed anywhere in the running app itself, since an
"add a user" screen behind the same login it protects would defeat the
point of having a login screen.

Usage:
    python manage_users.py add <username>       Add a new user, or change
                                                 an existing one's password
                                                 (prompts for the password
                                                 twice, to catch typos --
                                                 never shown on screen,
                                                 never passed as a CLI
                                                 argument, which would
                                                 otherwise land in shell
                                                 history)
    python manage_users.py remove <username>    Remove a user
    python manage_users.py list                 List current usernames
                                                 (never passwords or hashes)

Run this directly on the machine the app itself runs on.
"""

from __future__ import annotations

import argparse
import getpass
import sys

import auth

MIN_PASSWORD_LENGTH = 8


def _prompt_new_password() -> str | None:
    """Prompts twice to catch typos (passwords aren't echoed to the
    screen, so there's no other way to visually confirm what was typed).
    Returns None (with a message already printed) if the two entries
    don't match or the password is too short, rather than raising --
    the caller treats None as "try again" territory, not a crash."""
    password = getpass.getpass("New password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
        return None
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords didn't match -- nothing was changed.", file=sys.stderr)
        return None
    return password


def cmd_add(username: str) -> int:
    password = _prompt_new_password()
    if password is None:
        return 1
    existing = auth.load_users()
    is_update = username in existing
    auth.add_user(username, password)
    print(f"{'Updated password for' if is_update else 'Added'} user {username!r}.")
    return 0


def cmd_remove(username: str) -> int:
    if auth.remove_user(username):
        print(f"Removed user {username!r}.")
        return 0
    print(f"No user named {username!r} was found -- nothing to remove.", file=sys.stderr)
    return 1


def cmd_list() -> int:
    users = auth.load_users()
    if not users:
        print("No users configured yet. Add one with: python manage_users.py add <username>")
        return 0
    print(f"{len(users)} user(s) configured:")
    for username in sorted(users):
        print(f"  {username}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Manage login users for this app.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Add a user, or change an existing user's password")
    add_parser.add_argument("username")

    remove_parser = subparsers.add_parser("remove", help="Remove a user")
    remove_parser.add_argument("username")

    subparsers.add_parser("list", help="List current usernames")

    args = parser.parse_args(argv)

    if args.command == "add":
        return cmd_add(args.username)
    elif args.command == "remove":
        return cmd_remove(args.username)
    else:  # "list"
        return cmd_list()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
