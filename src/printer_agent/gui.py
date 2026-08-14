from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

try:
    import yaml
except Exception:  # pragma: no cover - tkinter GUI can still run without yaml only if config already exists
    yaml = None

from . import __version__
from .config import (
    AgentConfig,
    BackoffConfig,
    ConfigError,
    OutboxConfig,
    PrinterConfig,
    UpdateConfig,
    config_from_dict,
    load_config,
    save_config,
    validate_config,
)
from .updates import apply_update, check_for_update


BRANDS = ("moonraker", "bambu")


def default_config_path() -> Path:
    program_data = os.environ.get("PROGRAMDATA")
    base_path = Path(program_data) if program_data else Path.home()
    return base_path / "printer-agent" / "agent.yaml"


class PrinterEditorDialog(simpledialog.Dialog):
    def __init__(self, parent: tk.Misc, printer: PrinterConfig | None = None):
        self._initial_printer = printer
        self.result: PrinterConfig | None = None
        super().__init__(parent, title="Printer")

    def body(self, master: tk.Misc) -> tk.Widget | None:
        initial = self._initial_printer or PrinterConfig(key="", brand="moonraker", host="")
        credentials = dict(initial.credentials)

        self.key_var = tk.StringVar(value=initial.key)
        self.brand_var = tk.StringVar(value=initial.brand or "moonraker")
        self.host_var = tk.StringVar(value=initial.host)
        self.port_var = tk.StringVar(value="" if initial.port is None else str(initial.port))
        self.access_code_var = tk.StringVar(value=str(credentials.pop("access_code", "")))
        self.serial_var = tk.StringVar(value=str(credentials.pop("serial", "")))
        self.extra_credentials_var = tk.StringVar(value=json.dumps(credentials, ensure_ascii=False, sort_keys=True) if credentials else "{}")

        fields = [
            ("Key", self.key_var),
            ("Brand", self.brand_var),
            ("Host", self.host_var),
            ("Port", self.port_var),
            ("Access code", self.access_code_var),
            ("Serial", self.serial_var),
            ("Extra credentials JSON", self.extra_credentials_var),
        ]

        for row, (label, variable) in enumerate(fields):
            ttk.Label(master, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
            if label == "Brand":
                widget = ttk.Combobox(master, textvariable=variable, values=BRANDS, state="readonly")
            else:
                widget = ttk.Entry(master, textvariable=variable, width=42)
            widget.grid(row=row, column=1, sticky="ew", pady=4)

        master.columnconfigure(1, weight=1)
        return None

    def validate(self) -> bool:
        key = self.key_var.get().strip()
        brand = self.brand_var.get().strip().lower()
        host = self.host_var.get().strip()
        port_text = self.port_var.get().strip()
        access_code = self.access_code_var.get().strip()
        serial = self.serial_var.get().strip()
        extra_text = self.extra_credentials_var.get().strip()

        if not key:
            messagebox.showerror("Invalid printer", "Printer key is required.")
            return False
        if brand not in BRANDS:
            messagebox.showerror("Invalid printer", f"Brand must be one of: {', '.join(BRANDS)}.")
            return False
        if port_text:
            try:
                port = int(port_text)
            except ValueError:
                messagebox.showerror("Invalid printer", "Port must be a number.")
                return False
            if port <= 0:
                messagebox.showerror("Invalid printer", "Port must be positive.")
                return False
        credentials: dict[str, object] = {}
        if extra_text:
            try:
                extra_credentials = json.loads(extra_text)
            except json.JSONDecodeError as exc:
                messagebox.showerror("Invalid printer", f"Extra credentials JSON is invalid: {exc}")
                return False
            if not isinstance(extra_credentials, dict):
                messagebox.showerror("Invalid printer", "Extra credentials JSON must decode to an object.")
                return False
            credentials.update(extra_credentials)
        if access_code:
            credentials["access_code"] = access_code
        if serial:
            credentials["serial"] = serial

        self.result = PrinterConfig(
            key=key,
            brand=brand,
            host=host,
            port=int(port_text) if port_text else None,
            credentials=credentials,
        )
        return True


class PrinterAgentConfigApp:
    def __init__(self, root: tk.Tk, config_path: Path):
        self.root = root
        self.config_path = config_path
        self.root.title("printer-agent configuration")
        self.root.geometry("920x680")
        self.root.minsize(860, 620)

        self.current_config = self._load_editor_config()
        self._build_ui()
        self._populate_form(self.current_config)

    def run(self) -> None:
        self.root.mainloop()

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=16)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(1, weight=1)
        container.rowconfigure(2, weight=0)
        container.rowconfigure(3, weight=0)

        header = ttk.Frame(container)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="printer-agent", font=("Segoe UI", 18, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text=str(self.config_path), foreground="#666666").grid(row=1, column=0, sticky="w", pady=(4, 0))

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(header, textvariable=self.status_var, anchor="e").grid(row=0, column=1, rowspan=2, sticky="e")

        general = ttk.LabelFrame(container, text="General", padding=12)
        general.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(16, 0))
        general.columnconfigure(1, weight=1)

        self.hub_url_var = tk.StringVar()
        self.agent_token_var = tk.StringVar()
        self.location_key_var = tk.StringVar()
        self.telemetry_interval_var = tk.StringVar()
        self.heartbeat_interval_var = tk.StringVar()
        self.outbox_path_var = tk.StringVar()
        self.outbox_max_events_var = tk.StringVar()
        self.backoff_min_var = tk.StringVar()
        self.backoff_max_var = tk.StringVar()
        self.update_feed_var = tk.StringVar()
        self.auto_update_var = tk.BooleanVar(value=False)
        self.check_on_startup_var = tk.BooleanVar(value=True)
        self.update_status_var = tk.StringVar(value=f"Current version: {__version__}")

        general_fields = [
            ("Hub URL", self.hub_url_var),
            ("Agent token", self.agent_token_var),
            ("Location key", self.location_key_var),
            ("Telemetry interval (s)", self.telemetry_interval_var),
            ("Heartbeat interval (s)", self.heartbeat_interval_var),
            ("Outbox path", self.outbox_path_var),
            ("Outbox max events", self.outbox_max_events_var),
            ("Reconnect backoff min (s)", self.backoff_min_var),
            ("Reconnect backoff max (s)", self.backoff_max_var),
        ]

        for row, (label, variable) in enumerate(general_fields):
            ttk.Label(general, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 8))
            ttk.Entry(general, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=4)

        updates_frame = ttk.LabelFrame(container, text="Updates", padding=12)
        updates_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        updates_frame.columnconfigure(1, weight=1)

        ttk.Label(updates_frame, text="Feed URL").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(updates_frame, textvariable=self.update_feed_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Checkbutton(updates_frame, text="Auto-update service on startup", variable=self.auto_update_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(updates_frame, text="Check for updates on startup", variable=self.check_on_startup_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(updates_frame, textvariable=self.update_status_var).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(updates_frame, text="Check now", command=self.check_for_updates).grid(row=4, column=0, sticky="w", pady=(12, 0))
        ttk.Button(updates_frame, text="Apply update", command=self.apply_update).grid(row=4, column=1, sticky="e", pady=(12, 0))

        printers_frame = ttk.LabelFrame(container, text="Printers", padding=12)
        printers_frame.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(16, 0))
        printers_frame.columnconfigure(0, weight=1)
        printers_frame.rowconfigure(0, weight=1)

        self.printer_tree = ttk.Treeview(printers_frame, columns=("brand", "host", "port"), show="headings", selectmode="browse", height=15)
        for column, title in (("brand", "Brand"), ("host", "Host"), ("port", "Port")):
            self.printer_tree.heading(column, text=title)
            self.printer_tree.column(column, width=120 if column != "host" else 220, anchor="w")
        self.printer_tree.grid(row=0, column=0, columnspan=3, sticky="nsew")
        self.printer_tree.bind("<Double-1>", lambda _: self.edit_selected_printer())

        ttk.Button(printers_frame, text="Add", command=self.add_printer).grid(row=1, column=0, sticky="ew", pady=(12, 0), padx=(0, 8))
        ttk.Button(printers_frame, text="Edit", command=self.edit_selected_printer).grid(row=1, column=1, sticky="ew", pady=(12, 0), padx=8)
        ttk.Button(printers_frame, text="Remove", command=self.remove_selected_printer).grid(row=1, column=2, sticky="ew", pady=(12, 0), padx=(8, 0))

        footer = ttk.Frame(container)
        footer.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Button(footer, text="Reload", command=self.reload_from_disk).grid(row=0, column=0, sticky="w")
        ttk.Button(footer, text="Save", command=self.save_to_disk).grid(row=0, column=1, sticky="e")

    def _populate_form(self, config: AgentConfig) -> None:
        self.hub_url_var.set(config.hub_url)
        self.agent_token_var.set(config.agent_token)
        self.location_key_var.set(config.location_key)
        self.telemetry_interval_var.set(str(config.telemetry_interval_s))
        self.heartbeat_interval_var.set(str(config.heartbeat_interval_s))
        self.outbox_path_var.set(str(config.outbox.database_path))
        self.outbox_max_events_var.set(str(config.outbox.max_events))
        self.backoff_min_var.set(str(config.command_reconnect_backoff_s.min_s))
        self.backoff_max_var.set(str(config.command_reconnect_backoff_s.max_s))
        self.update_feed_var.set(config.updates.feed_url)
        self.auto_update_var.set(config.updates.auto_update)
        self.check_on_startup_var.set(config.updates.check_on_startup)
        self.update_status_var.set(f"Current version: {__version__}")
        self._reload_printer_rows(config.printers)

    def _reload_printer_rows(self, printers: list[PrinterConfig]) -> None:
        self.printer_tree.delete(*self.printer_tree.get_children())
        self._printers = list(printers)
        for index, printer in enumerate(self._printers):
            self.printer_tree.insert("", "end", iid=str(index), values=(printer.brand, printer.host, "" if printer.port is None else printer.port))

    def _current_printer_index(self) -> int | None:
        selection = self.printer_tree.selection()
        if not selection:
            return None
        return int(selection[0])

    def add_printer(self) -> None:
        dialog = PrinterEditorDialog(self.root)
        if dialog.result is not None:
            self._printers.append(dialog.result)
            self._reload_printer_rows(self._printers)

    def edit_selected_printer(self) -> None:
        index = self._current_printer_index()
        if index is None:
            return
        dialog = PrinterEditorDialog(self.root, self._printers[index])
        if dialog.result is not None:
            self._printers[index] = dialog.result
            self._reload_printer_rows(self._printers)

    def remove_selected_printer(self) -> None:
        index = self._current_printer_index()
        if index is None:
            return
        del self._printers[index]
        self._reload_printer_rows(self._printers)

    def reload_from_disk(self) -> None:
        self.current_config = self._load_editor_config()
        self._populate_form(self.current_config)
        self.status_var.set("Configuration reloaded")

    def save_to_disk(self) -> None:
        try:
            config = self._build_config_from_form()
        except ValueError as exc:
            messagebox.showerror("Invalid configuration", str(exc))
            return
        errors = validate_config(config)
        if errors:
            messagebox.showerror("Invalid configuration", "\n".join(errors))
            return
        save_config(config, self.config_path)
        self.current_config = config
        self.status_var.set("Configuration saved")
        messagebox.showinfo("printer-agent", f"Saved to {self.config_path}")

    def check_for_updates(self) -> None:
        try:
            status = check_for_update(self.update_feed_var.get().strip())
        except Exception as exc:
            messagebox.showerror("Update check failed", str(exc))
            return
        self.update_status_var.set(
            f"Current version: {status.current_version} | Latest: {status.latest_version} | Available: {status.update_available}"
        )
        messagebox.showinfo("printer-agent", status.message)

    def apply_update(self) -> None:
        feed_url = self.update_feed_var.get().strip()
        if not feed_url:
            messagebox.showerror("Update failed", "Update feed URL is required.")
            return
        try:
            status = check_for_update(feed_url)
            if not status.update_available or status.manifest is None:
                self.update_status_var.set(f"Current version: {status.current_version} | Already up to date")
                messagebox.showinfo("printer-agent", status.message)
                return
            applied = apply_update(status.manifest)
        except Exception as exc:
            messagebox.showerror("Update failed", str(exc))
            return
        self.update_status_var.set(
            f"Current version: {applied.current_version} | Latest: {applied.latest_version} | Installed: {applied.installed}"
        )
        if applied.installed:
            messagebox.showinfo("printer-agent", "Update installed. Restart the GUI and service to pick up the new version.")
        else:
            messagebox.showerror("Update failed", applied.message)

    def _build_config_from_form(self) -> AgentConfig:
        try:
            telemetry_interval = int(self.telemetry_interval_var.get().strip())
            heartbeat_interval = int(self.heartbeat_interval_var.get().strip())
            outbox_max_events = int(self.outbox_max_events_var.get().strip())
            backoff_min = int(self.backoff_min_var.get().strip())
            backoff_max = int(self.backoff_max_var.get().strip())
        except ValueError as exc:
            raise ValueError("Interval and backoff fields must be integers.") from exc

        return AgentConfig(
            hub_url=self.hub_url_var.get().strip(),
            agent_token=self.agent_token_var.get().strip(),
            location_key=self.location_key_var.get().strip(),
            telemetry_interval_s=telemetry_interval,
            heartbeat_interval_s=heartbeat_interval,
            command_reconnect_backoff_s=BackoffConfig(min_s=backoff_min, max_s=backoff_max),
            outbox=OutboxConfig(database_path=Path(self.outbox_path_var.get().strip()), max_events=outbox_max_events),
            updates=UpdateConfig(
                feed_url=self.update_feed_var.get().strip(),
                auto_update=self.auto_update_var.get(),
                check_on_startup=self.check_on_startup_var.get(),
            ),
            printers=list(self._printers),
        )

    def _load_editor_config(self) -> AgentConfig:
        if self.config_path.exists():
            try:
                return load_config(self.config_path)
            except ConfigError:
                if yaml is None:
                    raise
                raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
                if not isinstance(raw, dict):
                    raw = {}
                return config_from_dict(raw)
        return AgentConfig(
            hub_url="",
            agent_token="",
            location_key="",
            telemetry_interval_s=5,
            heartbeat_interval_s=15,
            command_reconnect_backoff_s=BackoffConfig(),
            outbox=OutboxConfig(database_path=default_config_path().parent / "outbox.sqlite3", max_events=5000),
            updates=UpdateConfig(),
            printers=[],
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="printer-agent gui")
    parser.add_argument("--config", default=str(default_config_path()), help="Path to agent.yaml")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = tk.Tk()
    app = PrinterAgentConfigApp(root, Path(args.config))
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
