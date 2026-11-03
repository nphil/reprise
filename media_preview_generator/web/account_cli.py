"""Get back in when the password is forgotten (no email involved).

Run it inside the container, where the config folder is mounted::

    docker exec -it <container> python3 -m media_preview_generator.web.account_cli reset-password
    docker exec -it <container> python3 -m media_preview_generator.web.account_cli reset-account

``reset-password`` asks for a new password for the existing account and signs every browser out.
``reset-account`` removes the account, so the next visit shows the first-run page to make a new one.
The webhook secret is not touched by either, so Sonarr, Radarr and Plex keep working.
Deleting ``auth.json`` from the config folder does the same as ``reset-account``.
"""

import argparse
import getpass
import os
import sys

from . import auth


def _hand_file_back_to_the_app() -> None:
    """``docker exec`` runs as root; the app runs as the container's PUID user and couldn't read a root-owned file."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0 or not os.path.exists(auth.AUTH_FILE):
        return
    owner = os.stat(os.path.dirname(auth.AUTH_FILE) or ".")
    os.chown(auth.AUTH_FILE, owner.st_uid, owner.st_gid)


def _ask_new_password() -> str:
    password = getpass.getpass("New password: ")
    if password != getpass.getpass("New password again: "):
        raise auth.AccountError("The two passwords don't match.")
    return password


def main(argv: list[str] | None = None) -> int:
    """Run the recovery command; returns the process exit code."""
    parser = argparse.ArgumentParser(prog="account_cli", description="Recover access to Reprise.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("reset-password", help="set a new password for the existing account")
    commands.add_parser("reset-account", help="remove the account so the first-run page makes a new one")
    args = parser.parse_args(argv)

    try:
        if args.command == "reset-password":
            username = auth.reset_password(_ask_new_password())
            _hand_file_back_to_the_app()
            print(f"Done. '{username}' has a new password and every browser is signed out.")
        else:
            had_account = auth.reset_account()
            _hand_file_back_to_the_app()
            print(
                "Done. Open Reprise in your browser to create a new account."
                if had_account
                else "There was no account. Open Reprise in your browser to create one."
            )
    except (auth.AccountError, OSError) as exc:
        print(f"Could not do that: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
