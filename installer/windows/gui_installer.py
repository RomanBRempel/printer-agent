from __future__ import annotations

import ctypes
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk


STEP_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("bootstrap", "Подготовка окружения"),
    ("package", "Установка пакета"),
    ("config", "Запись конфигурации"),
    ("service", "Регистрация и запуск сервиса"),
    ("shortcuts", "Создание ярлыков"),
    ("finalize", "Финальная проверка"),
)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> None:
    args = " ".join(f'"{arg}"' for arg in sys.argv[1:])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args, None, 1)


class InstallerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("printer-agent Installer")
        self.root.geometry("840x560")
        self.root.minsize(780, 520)

        self._output_queue: queue.Queue[str] = queue.Queue()
        self._worker_thread: threading.Thread | None = None

        self.install_root_var = tk.StringVar(value=r"C:\Program Files\printer-agent")
        self.package_spec_var = tk.StringVar(value="")
        self.update_feed_var = tk.StringVar(
            value="https://github.com/RomanBRempel/printer-agent/releases/latest/download/printer-agent-update.json"
        )
        self.auto_update_var = tk.BooleanVar(value=True)
        self.step_states: dict[str, tk.StringVar] = {
            key: tk.StringVar(value="○") for key, _ in STEP_DEFINITIONS
        }
        self._current_step_index = -1

        self._build_ui()
        self._apply_window_icon()
        self.root.after(100, self._drain_output)

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=12)
        container.pack(fill=tk.BOTH, expand=True)

        title = ttk.Label(container, text="Установка printer-agent", font=("Segoe UI", 14, "bold"))
        title.pack(anchor="w")

        subtitle = ttk.Label(
            container,
            text="Установит Windows-сервис, создаст иконки и предложит запустить GUI настройки.",
        )
        subtitle.pack(anchor="w", pady=(2, 12))

        steps_frame = ttk.LabelFrame(container, text="Этапы установки", padding=10)
        steps_frame.pack(fill=tk.X, pady=(0, 10))
        for row, (key, caption) in enumerate(STEP_DEFINITIONS):
            ttk.Label(steps_frame, textvariable=self.step_states[key], width=2).grid(row=row, column=0, sticky="w")
            ttk.Label(steps_frame, text=caption).grid(row=row, column=1, sticky="w", padx=(6, 0))

        form = ttk.Frame(container)
        form.pack(fill=tk.X)

        self._add_row(form, 0, "Папка установки", self.install_root_var)
        self._add_row(form, 1, "PackageSpec (URL/path, optional)", self.package_spec_var)
        self._add_row(form, 2, "Update feed URL", self.update_feed_var)

        checks = ttk.Frame(container)
        checks.pack(fill=tk.X, pady=(8, 8))
        ttk.Checkbutton(checks, text="Включить автообновление", variable=self.auto_update_var).pack(anchor="w")

        button_row = ttk.Frame(container)
        button_row.pack(fill=tk.X, pady=(6, 8))

        self.install_button = ttk.Button(button_row, text="Установить", command=self.start_install)
        self.install_button.pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Готово к установке")
        ttk.Label(button_row, textvariable=self.status_var).pack(side=tk.LEFT, padx=(12, 0))

        self.progress = ttk.Progressbar(container, mode="determinate", maximum=len(STEP_DEFINITIONS), value=0)
        self.progress.pack(fill=tk.X, pady=(6, 8))

        self.log = tk.Text(container, height=20, wrap="word")
        self.log.pack(fill=tk.BOTH, expand=True)
        self.log.configure(state=tk.DISABLED)

    def _apply_window_icon(self) -> None:
        icon_path = self._resource_dir() / "printer-agent.ico"
        if icon_path.exists():
            try:
                self.root.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass

    @staticmethod
    def _resource_dir() -> Path:
        if getattr(sys, "frozen", False):
            return Path(getattr(sys, "_MEIPASS"))
        return Path(__file__).resolve().parent

    @staticmethod
    def _add_row(parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label, width=32).grid(row=row, column=0, sticky="w", pady=4)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        parent.columnconfigure(1, weight=1)

    def append_log(self, line: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, line)
        if not line.endswith("\n"):
            self.log.insert(tk.END, "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def start_install(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            return

        self.install_button.configure(state=tk.DISABLED)
        self.progress.configure(value=0)
        self.status_var.set("Установка выполняется...")
        self._reset_steps()
        self._set_step_state("bootstrap", "active")

        install_root = self.install_root_var.get().strip()
        package_spec = self.package_spec_var.get().strip()
        update_feed = self.update_feed_var.get().strip()
        auto_update = self.auto_update_var.get()

        self._worker_thread = threading.Thread(
            target=self._run_install,
            args=(install_root, package_spec, update_feed, auto_update),
            daemon=True,
        )
        self._worker_thread.start()

    def _run_install(self, install_root: str, package_spec: str, update_feed: str, auto_update: bool) -> None:
        script_path = self._resource_dir() / "install.ps1"
        if not script_path.exists():
            self._output_queue.put("ERROR: install.ps1 was not found next to gui installer.")
            self._output_queue.put("__DONE__:2")
            return

        command: list[str] = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            "-InstallRoot",
            install_root,
            "-UpdateFeedUrl",
            update_feed,
            "-AutoUpdate",
            "True" if auto_update else "False",
        ]

        if package_spec:
            command.extend(["-PackageSpec", package_spec])

        self._output_queue.put("Running: " + " ".join(command))

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as exc:
            self._output_queue.put(f"ERROR: failed to start installer: {exc}")
            self._output_queue.put("__DONE__:2")
            return

        assert process.stdout is not None
        for line in process.stdout:
            self._output_queue.put(line.rstrip("\n"))

        return_code = process.wait()
        self._output_queue.put(f"Installer exit code: {return_code}")
        self._output_queue.put(f"__DONE__:{return_code}")

    def _drain_output(self) -> None:
        try:
            while True:
                item = self._output_queue.get_nowait()
                if item.startswith("__DONE__:"):
                    code = int(item.split(":", 1)[1])
                    self._finish_install(code)
                elif item.startswith("[STEP] "):
                    self._apply_step_marker(item)
                    self.append_log(item)
                else:
                    self.append_log(item)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._drain_output)

    def _finish_install(self, return_code: int) -> None:
        self.install_button.configure(state=tk.NORMAL)

        if return_code == 0:
            self._set_step_state("finalize", "done")
            self.status_var.set("Установка завершена успешно")
            launch_now = messagebox.askyesno(
                "Установка завершена",
                "Сервис и иконки созданы. Запустить GUI для настройки сервиса сейчас?",
            )
            if launch_now:
                self._launch_config_gui()
        else:
            if self._current_step_index >= 0:
                key = STEP_DEFINITIONS[self._current_step_index][0]
                self._set_step_state(key, "failed")
            self.status_var.set("Установка завершена с ошибкой")
            messagebox.showerror(
                "Ошибка установки",
                "Установка завершилась с ошибкой. Подробности смотрите в журнале ниже.",
            )

    def _reset_steps(self) -> None:
        self._current_step_index = -1
        for key, _ in STEP_DEFINITIONS:
            self.step_states[key].set("○")

    def _apply_step_marker(self, line: str) -> None:
        marker = line.replace("[STEP]", "", 1).strip().lower()
        keys = [key for key, _ in STEP_DEFINITIONS]
        if marker not in keys:
            return

        target_index = keys.index(marker)
        for index in range(0, target_index):
            self._set_step_state(keys[index], "done")

        if self._current_step_index >= 0 and self._current_step_index < target_index:
            self._set_step_state(keys[self._current_step_index], "done")

        self._set_step_state(marker, "active")
        self._current_step_index = target_index

    def _set_step_state(self, step_key: str, state: str) -> None:
        if state == "done":
            symbol = "✓"
        elif state == "active":
            symbol = "▶"
        elif state == "failed":
            symbol = "✗"
        else:
            symbol = "○"
        self.step_states[step_key].set(symbol)
        completed = sum(1 for key, _ in STEP_DEFINITIONS if self.step_states[key].get() == "✓")
        active = any(self.step_states[key].get() == "▶" for key, _ in STEP_DEFINITIONS)
        self.progress.configure(value=completed + (1 if active else 0))

    def _launch_config_gui(self) -> None:
        install_root = Path(self.install_root_var.get().strip())
        pythonw = install_root / ".venv" / "Scripts" / "pythonw.exe"
        config_path = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "printer-agent" / "agent.yaml"

        if not pythonw.exists():
            messagebox.showwarning("Не найден GUI", f"Не найден Python GUI launcher: {pythonw}")
            return

        try:
            subprocess.Popen(
                [str(pythonw), "-m", "printer_agent", "gui", "--config", str(config_path)],
                cwd=str(install_root),
            )
        except Exception as exc:
            messagebox.showerror("Не удалось запустить GUI", str(exc))


def main() -> int:
    if os.name != "nt":
        print("This installer UI is Windows-only.")
        return 2

    if not is_admin():
        relaunch_as_admin()
        return 0

    root = tk.Tk()
    root.option_add("*Font", "Segoe UI 10")
    app = InstallerApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
