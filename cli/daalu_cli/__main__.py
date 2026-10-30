"""`daalu` entry point — argparse dispatch.

Subcommands:

    daalu login [--host URL] [--code CODE] [--no-browser]
    daalu logout
    daalu status            (alias: whoami)
    daalu models
    daalu chat              (also the default when no subcommand is given)
    daalu --version

Running bare ``daalu`` in a project directory starts the agent.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from daalu_cli import __version__
from daalu_cli.config import DEFAULT_HOST, load_credentials


def _cmd_login(args: argparse.Namespace) -> int:
    from daalu_cli import auth

    host = args.host or DEFAULT_HOST
    return auth.login(host, code=args.code, open_browser=not args.no_browser)


def _cmd_logout(_args: argparse.Namespace) -> int:
    from daalu_cli import auth

    return auth.logout()


def _cmd_status(_args: argparse.Namespace) -> int:
    from daalu_cli import auth

    return auth.status()


def _require_login():
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run `daalu login` first.", file=sys.stderr)
        return None
    return creds


def _cmd_models(_args: argparse.Namespace) -> int:
    creds = _require_login()
    if creds is None:
        return 1
    from daalu_cli.client import ApiError, DaaluClient

    try:
        cfg = DaaluClient(creds.host, creds.token).coding_config()
    except ApiError as e:
        print(f"could not load models: {e}", file=sys.stderr)
        return 1
    default = cfg.get("default_model")
    print("Available coding models:\n")
    for m in cfg.get("models", []):
        mark = "*" if m["served_model_name"] == default else " "
        state = "" if m["available"] else "  (not deployed yet)"
        print(f"  {mark} {m['label']:<24} {m['served_model_name']}{state}")
    print("\n  * = default")
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    creds = _require_login()
    if creds is None:
        return 1
    from daalu_cli import agent
    from daalu_cli.client import DaaluClient

    model = args.model or creds.model
    if not model:
        # Resolve a default from the server if creds didn't carry one.
        from daalu_cli.client import ApiError

        try:
            cfg = DaaluClient(creds.host, creds.token).coding_config()
            model = cfg.get("default_model")
        except ApiError as e:
            print(f"could not resolve a model: {e}", file=sys.stderr)
            return 1
    client = DaaluClient(creds.host, creds.token)
    return agent.run(root=Path(args.directory or "."), client=client, model=model)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="daalu",
        description="Daalu coding agent for your terminal.",
    )
    p.add_argument("--version", action="version", version=f"daalu {__version__}")
    sub = p.add_subparsers(dest="command")

    login = sub.add_parser("login", help="authenticate this machine")
    login.add_argument("--host", help=f"Daalu instance URL (default {DEFAULT_HOST})")
    login.add_argument("--code", help="redeem a one-time login code (express flow)")
    login.add_argument(
        "--no-browser", action="store_true", help="don't auto-open the browser"
    )
    login.set_defaults(func=_cmd_login)

    logout = sub.add_parser("logout", help="remove stored credentials")
    logout.set_defaults(func=_cmd_logout)

    for name in ("status", "whoami"):
        st = sub.add_parser(name, help="show the logged-in session")
        st.set_defaults(func=_cmd_status)

    models = sub.add_parser("models", help="list available coding models")
    models.set_defaults(func=_cmd_models)

    chat = sub.add_parser("chat", help="start the coding agent (default)")
    chat.add_argument(
        "-C", "--directory", help="project directory (default: current)"
    )
    chat.add_argument("--model", help="override the coding model (served name)")
    chat.set_defaults(func=_cmd_chat)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        # Bare `daalu` → start the agent in the current directory.
        args = parser.parse_args(["chat", *(argv or [])])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
