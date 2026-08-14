from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from .config import AgentConfig, ConfigError, load_config, parse_config, save_config
from .core.outbox import EventOutbox
from .logging import configure_logging
from .updates import apply_update, check_for_update, publish_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="printer-agent")
    parser.add_argument("--config", default="agent.yaml", help="Path to agent.yaml")

    # `--config` reads naturally on either side of the subcommand, so accept
    # both. SUPPRESS keeps the subparser from overwriting a value given before
    # the subcommand with its own default.
    config_parent = argparse.ArgumentParser(add_help=False)
    config_parent.add_argument(
        "--config", default=argparse.SUPPRESS, help="Path to agent.yaml"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="Run the agent service", parents=[config_parent])
    subparsers.add_parser("status", help="Show local agent status", parents=[config_parent])
    subparsers.add_parser("gui", help="Open the local configuration GUI", parents=[config_parent])
    subparsers.add_parser("install-service", help="Install the Windows service", parents=[config_parent])
    subparsers.add_parser("uninstall-service", help="Remove the Windows service", parents=[config_parent])

    update_parser = subparsers.add_parser(
        "update", help="Check or apply a software update", parents=[config_parent]
    )
    update_parser.add_argument("--feed-url", help="Override the update feed URL")
    update_parser.add_argument("--apply", action="store_true", help="Install the available update")

    publish_parser = subparsers.add_parser("publish-update", help="Write an update manifest file")
    publish_parser.add_argument("--version", required=True, help="Release version to publish")
    publish_parser.add_argument("--package-url", required=True, help="Wheel or source distribution URL")
    publish_parser.add_argument("--output", required=True, help="Path to the manifest JSON file")
    publish_parser.add_argument("--sha256", default="", help="Optional SHA-256 checksum")
    publish_parser.add_argument("--notes", default="", help="Optional release notes")
    publish_parser.add_argument("--published-at", default="", help="Optional ISO timestamp")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging()

    if args.command == "gui":
        # Dispatched before load_config on purpose: the editor exists to fix a
        # broken config, so refusing to open on one is exactly backwards. It
        # also runs under pythonw.exe, where parser.exit() would write to a
        # non-existent stderr and kill the process with no window and no message.
        from .desktop import main as desktop_main

        return desktop_main(["--config", str(args.config)])

    if args.command in {"install-service", "uninstall-service"}:
        # Registering a service does not depend on the config being runnable.
        # A fresh install writes a template with no printers yet, and refusing
        # to register it would leave the operator with no service to configure.
        return _service_command(parser, args)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.exit(status=2, message=f"configuration error: {exc}\n")

    if args.command == "status":
        outbox = EventOutbox(config.outbox.database_path)
        summary = outbox.summary()
        print(f"hub_url={config.hub_url}")
        print(f"location_key={config.location_key}")
        print(f"printers={len(config.printers)}")
        print(f"outbox_pending={summary['pending_events']}")
        print(f"command_results={summary['command_results']}")
        return 0

    if args.command == "run":
        return run_agent(config)

    if args.command == "update":
        feed_url = args.feed_url or config.updates.feed_url
        status = check_for_update(feed_url)
        print(f"current_version={status.current_version}")
        print(f"latest_version={status.latest_version}")
        print(f"update_available={status.update_available}")
        print(f"message={status.message}")
        if args.apply and status.update_available and status.manifest is not None:
            applied = apply_update(status.manifest)
            print(f"installed={applied.installed}")
            print(f"install_message={applied.message}")
            return 0 if applied.installed else 1
        return 0 if not status.update_available else 1

    if args.command == "publish-update":
        manifest = publish_manifest(
            version=args.version,
            package_url=args.package_url,
            destination=args.output,
            sha256=args.sha256,
            notes=args.notes,
            published_at=args.published_at,
        )
        print(f"wrote update manifest for {manifest.version} to {args.output}")
        return 0

    parser.exit(status=1, message="unknown command\n")


def run_agent(config: AgentConfig) -> int:
    """Run the hub session and the printer poll loop until interrupted."""
    import asyncio
    from logging import getLogger

    from .uplink.connection import HubConnection, HubRejected

    logger = getLogger(__name__)

    async def _serve() -> None:
        outbox = EventOutbox(config.outbox.database_path)
        connection = HubConnection(config, outbox)
        try:
            await connection.run()
        finally:
            connection.stop()
            outbox.close()

    logger.info(
        "printer-agent started",
        extra={"action": "startup", "hub_url": config.hub_url, "printers": str(len(config.printers))},
    )
    try:
        asyncio.run(_serve())
    except HubRejected as exc:
        logger.error("hub rejected this agent", extra={"action": "shutdown", "reason": str(exc)})
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive path
        logger.info("printer-agent stopped", extra={"action": "shutdown"})
    return 0


def _service_command(parser: argparse.ArgumentParser, args) -> int:
    if os.name != "nt":
        parser.exit(status=2, message="service management is only supported on Windows\n")

    if args.command == "uninstall-service":
        _run_windows_service_command("remove")
        print("removed Windows service")
        return 0

    from .windows_service import CONFIG_PATH

    try:
        config, errors = parse_config(args.config)
    except ConfigError as exc:
        # A malformed YAML document is a different failure: there is nothing to
        # copy to ProgramData, so this one really does stop the install.
        parser.exit(status=2, message=f"configuration error: {exc}\n")

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if Path(args.config).resolve() != CONFIG_PATH.resolve():
        save_config(config, CONFIG_PATH)
    _run_windows_service_command("install")
    print(f"installed Windows service with config {CONFIG_PATH}")
    if errors:
        # Registered but not yet runnable. Say so plainly: the service will fail
        # to start until these are fixed in the app.
        print("service is registered but the configuration is not runnable yet:")
        for error in errors:
            print(f"  - {error}")
    return 0


def _run_windows_service_command(command: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "printer_agent.windows_service", command],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or f"Windows service command failed: {command}"
        raise SystemExit(message)
