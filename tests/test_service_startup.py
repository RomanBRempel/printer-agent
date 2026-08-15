"""Two ways the Windows service died before it could say anything useful."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from printer_agent import windows_service
from printer_agent.config import parse_config

CONFIG_WITH_RELATIVE_OUTBOX = """
hub_url: ws://hub.example.com/api/printers/agent
agent_token: t
location_key: loc-1
outbox:
  database_path: data/outbox.sqlite3
  max_events: 5000
printers:
  - key: p1
    brand: moonraker
    host: 192.168.1.5
"""


@pytest.fixture()
def config_file(tmp_path):
    path = tmp_path / "conf" / "agent.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(CONFIG_WITH_RELATIVE_OUTBOX, encoding="utf-8")
    return path


def test_relative_outbox_anchors_to_the_config_not_the_cwd(config_file):
    """The service starts in System32; `data/` there is access-denied."""
    config, errors = parse_config(config_file)

    assert errors == []
    assert config.outbox.database_path == (config_file.parent / "data" / "outbox.sqlite3").resolve()
    assert config.outbox.database_path.is_absolute()


def test_an_absolute_outbox_path_is_left_alone(tmp_path):
    path = tmp_path / "agent.yaml"
    absolute = tmp_path / "queue" / "outbox.sqlite3"
    path.write_text(
        CONFIG_WITH_RELATIVE_OUTBOX.replace("data/outbox.sqlite3", absolute.as_posix()),
        encoding="utf-8",
    )

    config, _ = parse_config(path)

    assert config.outbox.database_path == absolute


def test_the_resolved_path_is_writable(config_file):
    """Anchoring is only useful if the directory can actually be created."""
    from printer_agent.core.outbox import EventOutbox

    config, _ = parse_config(config_file)
    outbox = EventOutbox(config.outbox.database_path)
    try:
        assert outbox.summary() == {"pending_events": 0, "command_results": 0}
    finally:
        outbox.close()


def test_the_service_reports_running_before_doing_any_work():
    """Without this the SCM kills the start after 30s with error 1053.

    The config read, the update check and the hub dial all happen after the
    handshake and can each exceed the timeout on their own. Read the module
    source rather than the class: the class only exists where pywin32 does,
    and this invariant has to hold everywhere it is edited.
    """
    source = Path(inspect.getfile(windows_service)).read_text(encoding="utf-8")
    body = source[source.index("def SvcDoRun") : source.index("async def _run")]

    assert "SERVICE_RUNNING" in body
    assert body.index("ReportServiceStatus") < body.index("run_async(")


def test_service_environment_covers_the_dll_search_path():
    """pythonservice.exe links against python3XX.dll, which a venv does not copy.

    Missing it, the process dies with 0xC0000135 before reaching any of our
    code, and the SCM reports only "did not respond in a timely fashion".
    """
    import sys
    import sysconfig

    paths = [str(item) for item in windows_service.service_environment_paths()]

    assert sys.base_prefix in paths
    assert str(Path(sysconfig.get_paths()["purelib"]) / "pywin32_system32") in paths


def test_service_environment_block_keeps_the_existing_path(monkeypatch):
    monkeypatch.setenv("PATH", r"C:\Windows\System32")

    block = windows_service.build_service_environment()
    path_entry = next(item for item in block if item.startswith("PATH="))

    # Replacing PATH outright would strip System32 from the service process.
    assert path_entry.endswith(r"C:\Windows\System32")
    assert any(item.startswith("PYTHONPATH=") for item in block)


def test_service_environment_sets_pythonpath_to_site_packages():
    import sysconfig

    block = windows_service.build_service_environment()
    entry = next(item for item in block if item.startswith("PYTHONPATH="))

    assert entry == f"PYTHONPATH={sysconfig.get_paths()['purelib']}"


def test_the_service_runs_the_venv_python_not_pythonservice_exe():
    """pythonservice.exe cannot find python3XX.dll from inside a venv.

    It sits in the venv root and links against the base interpreter's DLL,
    which a venv does not copy, so it exited 0xC0000135 before running any of
    our code. A venv's own python.exe resolves that through pyvenv.cfg.
    """
    source = Path(inspect.getfile(windows_service)).read_text(encoding="utf-8")

    assert "_exe_name_ = sys.executable" in source
    assert "-u -m " in source


def test_the_module_hosts_the_dispatcher_when_launched_bare():
    """With _exe_args_ the SCM starts `python.exe -u windows_service.py`.

    Without the dispatcher branch that process would parse no arguments, do
    nothing, and the SCM would time it out.
    """
    source = Path(inspect.getfile(windows_service)).read_text(encoding="utf-8")
    body = source[source.index("def main()") :]

    assert "StartServiceCtrlDispatcher" in body
    assert "PrepareToHostSingle" in body
    assert body.index("len(sys.argv) == 1") < body.index("HandleCommandLine")


def test_the_package_never_shadows_a_stdlib_module():
    """Running the service as a file put the package dir first on sys.path.

    `printer_agent/logging.py` then answered `import logging` for the whole
    interpreter, and asyncio failed to import before any of our code ran.
    """
    import sys

    package = Path(inspect.getfile(windows_service)).parent
    shadowed = {
        path.stem
        for path in package.glob("*.py")
        if path.stem in sys.stdlib_module_names
    }

    assert shadowed == set()
