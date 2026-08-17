"""Bambu print job: FTPS upload and the MQTT start command.

Neither half can be proven against a live printer here, and both fail *quietly*
on a real one — MQTT does not acknowledge a publish, so a wrong command reads as
"the print simply never started", and a mis-negotiated FTPS transfer reads as a
network fault. What can be pinned without hardware is the shape of both: which
command the printer is asked to run, where it is told to find the file, and that
the slot mapping the hub worked out is actually passed on.

The FTPS quirks (implicit TLS on 990, data channel reusing the control session)
are not decoration either: without them the login succeeds and the transfer dies,
which is the confusing half of this protocol.
"""

from __future__ import annotations

import ftplib
import ssl
from pathlib import Path
from typing import Any

import pytest

from printer_agent.adapters import bambu
from printer_agent.adapters.bambu import BambuAdapter
from printer_agent.adapters.base import UnsupportedCommandError
from printer_agent.config import PrinterConfig


def make_adapter(**credentials: Any) -> BambuAdapter:
    creds = {"serial": "0309DA4B0803132", "access_code": "12345678"}
    creds.update(credentials)
    return BambuAdapter(
        PrinterConfig(key="h2d", brand="bambu", host="10.0.0.5", credentials=creds)
    )


class RecordingPublish:
    """Captures the MQTT payload instead of sending it."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


@pytest.fixture
def published(monkeypatch) -> RecordingPublish:
    recorder = RecordingPublish()
    monkeypatch.setattr(BambuAdapter, "_publish_json", lambda self, message: recorder(message))
    return recorder


# ── Что именно просим напечатать ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sliced_project_is_started_as_a_project_file(published) -> None:
    """`.3mf` — архив, и принтеру отдельно называют плиту внутри него."""
    adapter = make_adapter()

    result = await adapter.start_print("f00d", "BWB-20-D-001-R1.gcode.3mf")

    payload = published.messages[0]["print"]
    assert payload["command"] == "project_file"
    assert payload["param"] == bambu.BAMBU_PROJECT_PLATE
    assert payload["url"] == "file:///sdcard/BWB-20-D-001-R1.gcode.3mf"
    assert result["url"] == payload["url"]


@pytest.mark.asyncio
async def test_plain_gcode_is_started_as_a_gcode_file(published) -> None:
    """У голого `.gcode` плиты нет — путь и есть параметр команды."""
    adapter = make_adapter()

    await adapter.start_print("f00d", "part.gcode")

    payload = published.messages[0]["print"]
    assert payload["command"] == "gcode_file"
    assert payload["param"] == "/sdcard/part.gcode"


@pytest.mark.asyncio
async def test_url_prefix_is_configuration_not_a_guess(published) -> None:
    """P- и A-серия адресуют корень, X-серия — `/sdcard`, и модель не определяется.

    Ошибка здесь не даёт ни исключения, ни ответа: печать просто не начинается.
    Поэтому префикс — настройка принтера, а не эвристика по серийному номеру.
    """
    adapter = make_adapter(print_url_prefix="file:///")

    await adapter.start_print("f00d", "part.gcode.3mf")

    assert published.messages[0]["print"]["url"] == "file:///part.gcode.3mf"


@pytest.mark.asyncio
async def test_missing_trailing_slash_in_the_prefix_is_forgiven(published) -> None:
    """Печать не должна срываться из-за символа, забытого в YAML."""
    adapter = make_adapter(print_url_prefix="file:///sdcard")

    await adapter.start_print("f00d", "part.gcode.3mf")

    assert published.messages[0]["print"]["url"] == "file:///sdcard/part.gcode.3mf"


@pytest.mark.asyncio
async def test_start_print_without_a_name_is_refused(published) -> None:
    """Безымянная печать — это печать неизвестно чего."""
    adapter = make_adapter()

    with pytest.raises(RuntimeError):
        await adapter.start_print("", None)
    assert published.messages == []


# ── Материал ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_slot_mapping_from_the_hub_reaches_the_printer(published) -> None:
    """Хаб сверил программу со слотами — его ответ и должен доехать.

    Потерять сопоставление значит отдать выбор слота принтеру, который знает о
    программе меньше, чем хаб: печать выйдет не тем пластиком, а это брак.
    """
    adapter = make_adapter()

    await adapter.start_print("f00d", "part.gcode.3mf", ams_mapping=[2, 0])

    payload = published.messages[0]["print"]
    assert payload["ams_mapping"] == [2, 0]
    assert payload["use_ams"] is True


@pytest.mark.asyncio
async def test_without_a_mapping_the_feeder_stays_out_of_it(published) -> None:
    """Нет сопоставления — принтер не выбирает слот сам."""
    adapter = make_adapter()

    await adapter.start_print("f00d", "part.gcode.3mf")

    payload = published.messages[0]["print"]
    assert payload["use_ams"] is False
    assert "ams_mapping" not in payload


@pytest.mark.asyncio
async def test_calibrations_do_not_run_on_every_job(published) -> None:
    """Калибровки добавляют минуты к каждой печати; нивелирование стола — нет."""
    adapter = make_adapter()

    await adapter.start_print("f00d", "part.gcode.3mf")

    payload = published.messages[0]["print"]
    assert payload["bed_leveling"] is True
    assert payload["flow_cali"] is False
    assert payload["vibration_cali"] is False


# ── Загрузка файла ──────────────────────────────────────────────────────────


class FakeFTPS:
    """Записывает вызовы вместо разговора с принтером."""

    instances: list["FakeFTPS"] = []

    def __init__(self, *, context: ssl.SSLContext | None = None, **_: Any) -> None:
        self.context = context
        self.calls: list[tuple[str, Any]] = []
        self.stored: tuple[str, bytes] | None = None
        FakeFTPS.instances.append(self)

    def connect(self, host: str, port: int, timeout: int | None = None) -> None:
        self.calls.append(("connect", (host, port)))

    def login(self, user: str, passwd: str) -> None:
        self.calls.append(("login", (user, passwd)))

    def prot_p(self) -> None:
        self.calls.append(("prot_p", None))

    def cwd(self, path: str) -> None:
        self.calls.append(("cwd", path))

    def storbinary(self, command: str, handle: Any) -> None:
        self.stored = (command, handle.read())
        self.calls.append(("storbinary", command))

    def quit(self) -> None:
        self.calls.append(("quit", None))

    def close(self) -> None:
        self.calls.append(("close", None))


@pytest.fixture
def ftps(monkeypatch) -> type[FakeFTPS]:
    FakeFTPS.instances = []
    monkeypatch.setattr(bambu, "_ImplicitFTPS", FakeFTPS)
    return FakeFTPS


@pytest.mark.asyncio
async def test_upload_goes_to_the_implicit_ftps_port_with_the_access_code(ftps, tmp_path) -> None:
    """Порт 990, пользователь `bblp`, пароль — код доступа принтера."""
    source = tmp_path / "part.gcode.3mf"
    source.write_bytes(b"payload")
    adapter = make_adapter()

    result = await adapter.upload_file(source, "BWB-20-D-001-R1.gcode.3mf")

    client = ftps.instances[0]
    assert ("connect", ("10.0.0.5", bambu.BAMBU_FTPS_PORT)) in client.calls
    assert ("login", (bambu.BAMBU_FTPS_USER, "12345678")) in client.calls
    assert client.stored == ("STOR BWB-20-D-001-R1.gcode.3mf", b"payload")
    assert result["remote_name"] == "BWB-20-D-001-R1.gcode.3mf"
    assert result["size_bytes"] == len(b"payload")


@pytest.mark.asyncio
async def test_data_channel_is_encrypted_before_the_transfer(ftps, tmp_path) -> None:
    """Без `prot_p()` принтер принимает логин и отказывает в передаче.

    Именно это и путает: аутентификация выглядит успешной.
    """
    source = tmp_path / "part.gcode"
    source.write_bytes(b"x")
    adapter = make_adapter()

    await adapter.upload_file(source, "part.gcode")

    order = [name for name, _ in ftps.instances[0].calls]
    assert order.index("prot_p") < order.index("storbinary")


@pytest.mark.asyncio
async def test_control_connection_is_always_released(ftps, tmp_path) -> None:
    """Принтер держит немного управляющих соединений — утёкшее стоит следующей загрузки."""
    source = tmp_path / "part.gcode"
    source.write_bytes(b"x")
    adapter = make_adapter()

    def explode(self: FakeFTPS, command: str, handle: Any) -> None:
        raise ftplib.error_perm("550 no space")

    FakeFTPS.storbinary = explode  # type: ignore[assignment]
    try:
        with pytest.raises(ftplib.error_perm):
            await adapter.upload_file(source, "part.gcode")
    finally:
        del FakeFTPS.storbinary

    order = [name for name, _ in ftps.instances[0].calls]
    assert "quit" in order or "close" in order


@pytest.mark.asyncio
async def test_upload_without_an_access_code_is_refused_before_connecting(ftps, tmp_path) -> None:
    """Кода доступа нет — FTPS невозможен, и говорим об этом, а не пробуем."""
    source = tmp_path / "part.gcode"
    source.write_bytes(b"x")
    adapter = make_adapter(access_code="")

    with pytest.raises(UnsupportedCommandError):
        await adapter.upload_file(source, "part.gcode")
    assert ftps.instances == []


@pytest.mark.asyncio
async def test_missing_file_is_named_rather_than_uploaded_empty(ftps, tmp_path) -> None:
    """Пустая печать хуже отказа: принтер прогреется и сделает ничего."""
    adapter = make_adapter()

    with pytest.raises(RuntimeError):
        await adapter.upload_file(tmp_path / "nope.gcode", "nope.gcode")
    assert ftps.instances == []


# ── Флаг возможности ────────────────────────────────────────────────────────


def test_upload_capability_follows_the_access_code() -> None:
    """Флаг отражает то, что адаптер реально может, а не намерение.

    Хаб превращает его в кнопку: поднятый без основания, он даёт кнопку,
    способную ответить только отказом.
    """
    assert make_adapter().capabilities().upload is True
    assert make_adapter(access_code="").capabilities().upload is False
