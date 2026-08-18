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
async def test_control_connection_is_always_released(ftps, tmp_path, monkeypatch) -> None:
    """Принтер держит немного управляющих соединений — утёкшее стоит следующей загрузки."""
    source = tmp_path / "part.gcode"
    source.write_bytes(b"x")
    adapter = make_adapter()

    def explode(self: FakeFTPS, command: str, handle: Any) -> None:
        raise ftplib.error_perm("550 no space")

    # monkeypatch, а не присваивание с `del`: удаление снимает и подменыш, и
    # настоящий метод класса, и следующий тест, которому нужна загрузка, падает
    # на пустом месте.
    monkeypatch.setattr(FakeFTPS, "storbinary", explode)
    # Отказ приходит обёрнутым: оператор читает эту строку целиком, и без шага
    # и адреса «550 no space» не говорит, на чём именно всё встало.
    with pytest.raises(RuntimeError, match="550 no space"):
        await adapter.upload_file(source, "part.gcode")

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


# ── Завершение передачи ─────────────────────────────────────────────────────


class _SilentDataConnection:
    """Сокет данных, который не отвечает на TLS `close_notify`.

    Ровно поведение прошивки Bambu: она закрывает соединение на уровне TCP и
    ничего больше не присылает.
    """

    def __init__(self) -> None:
        self.sent = b""
        self.timeouts: list[Any] = []
        self.closed = False

    def __enter__(self) -> "_SilentDataConnection":
        return self

    def __exit__(self, *_: Any) -> None:
        self.closed = True

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, value: Any) -> None:
        self.timeouts.append(value)

    def unwrap(self) -> None:
        raise TimeoutError("The read operation timed out")


class _RecordingClient(bambu._ImplicitFTPS):
    """`_ImplicitFTPS` без сети: подменены только команды управляющего канала."""

    def __init__(self) -> None:  # noqa: D107 - ftplib.__init__ здесь только мешает
        self.commands: list[str] = []
        self.conn = _SilentDataConnection()

    def voidcmd(self, cmd: str) -> str:
        self.commands.append(cmd)
        return "200 ok"

    def transfercmd(self, cmd: str, rest: Any = None) -> Any:
        self.commands.append(cmd)
        return self.conn

    def voidresp(self) -> str:
        self.commands.append("<voidresp>")
        return "226 Transfer complete"


def test_a_printer_that_never_answers_close_notify_still_completes_the_upload(tmp_path) -> None:
    """Файл уже на принтере — таймаут TLS-прощания не делает передачу неудачной.

    Штатный `storbinary` ждёт ответного `close_notify` до таймаута сокета и
    падает с `The read operation timed out` — про файл, который принтер принял
    целиком. Оператор при этом видит несостоявшуюся передачу.
    """
    source = tmp_path / "part.gcode.3mf"
    source.write_bytes(b"payload")
    client = _RecordingClient()

    with source.open("rb") as handle:
        answer = client.storbinary("STOR part.gcode.3mf", handle)

    assert answer == "226 Transfer complete"
    assert client.conn.sent == b"payload"
    assert client.conn.closed is True


def test_the_tls_goodbye_gets_its_own_short_deadline(tmp_path) -> None:
    """Не таймаут соединения: файл уже передан, ждать его столько незачем."""
    source = tmp_path / "part.gcode"
    source.write_bytes(b"x")
    client = _RecordingClient()

    with source.open("rb") as handle:
        client.storbinary("STOR part.gcode", handle)

    assert client.conn.timeouts == [bambu.FTPS_TLS_SHUTDOWN_TIMEOUT_S]
    assert bambu.FTPS_TLS_SHUTDOWN_TIMEOUT_S < bambu.FTPS_TIMEOUT_S


@pytest.mark.asyncio
async def test_a_failed_upload_names_the_step_it_died_in(ftps, tmp_path, monkeypatch) -> None:
    """Хаб показывает оператору эту строку целиком — она должна что-то значить.

    «The read operation timed out» не различает отказ в соединении, отказ в
    логине и оборванную передачу, а лечатся они по-разному.
    """
    source = tmp_path / "part.gcode"
    source.write_bytes(b"x")

    def refuse(self: Any, user: str, passwd: str) -> None:
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(FakeFTPS, "login", refuse, raising=False)
    adapter = make_adapter()

    with pytest.raises(TimeoutError) as raised:
        await adapter.upload_file(source, "part.gcode")

    message = str(raised.value)
    assert "login" in message
    assert "10.0.0.5:990" in message


# ─── FTPS timeout is a link property, not a model property ──────────────────
#
# 30 s bounded a single block of the transfer, and on shop-floor Wi-Fi a printer
# routinely stops reading for longer in the middle of a multi-megabyte file. The
# failure surfaces as "the read operation timed out", which reads like a broken
# printer rather than a slow link — so the default is generous and the value is
# overridable per printer.


def _adapter_with(credentials: dict) -> object:
    from printer_agent.adapters.bambu import BambuAdapter
    from printer_agent.config import PrinterConfig

    return BambuAdapter(
        PrinterConfig(
            key="printer-1", brand="bambu", host="10.0.0.5", credentials=credentials
        )
    )


def test_ftps_timeout_defaults_to_the_documented_value():
    from printer_agent.adapters.bambu import FTPS_TIMEOUT_S

    assert _adapter_with({})._ftps_timeout() == float(FTPS_TIMEOUT_S)


def test_ftps_timeout_can_be_raised_per_printer():
    assert _adapter_with({"ftps_timeout_s": "300"})._ftps_timeout() == 300.0


def test_unusable_ftps_timeout_falls_back_instead_of_meaning_never():
    """A zero or a typo must not become "wait forever" or "give up at once"."""
    from printer_agent.adapters.bambu import FTPS_TIMEOUT_S

    for bad in ("0", "-5", "soon", ""):
        assert _adapter_with({"ftps_timeout_s": bad})._ftps_timeout() == float(FTPS_TIMEOUT_S)


# ─── Какую плиту печатать ───────────────────────────────────────────────────
#
# `param` указывал `Metadata/plate_1.gcode` всегда. Но Studio кладёт в архив
# gcode той плиты, которую нарезали: экспорт второй плиты двухплитного проекта
# даёт `Metadata/plate_2.gcode` и никакого `plate_1.gcode` — только его png и
# json, которые лежат там и для ненарезанных плит. Просьба напечатать плиту,
# которой в файле нет, получает `0500-4003`, «не удалось разобрать файл»: код,
# не называющий ни плиту, ни файл.


def make_project(path: Path, *plates: int, extras: tuple[str, ...] = ()) -> Path:
    import zipfile

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("3D/3dmodel.model", "<model/>")
        for plate in plates:
            archive.writestr(f"Metadata/plate_{plate}.gcode", "G1 X0\n")
            archive.writestr(f"Metadata/plate_{plate}.png", b"\x89PNG")
        for name in extras:
            archive.writestr(name, b"x")
    return path


def test_the_plate_that_was_sliced_is_the_one_printed(tmp_path) -> None:
    """Ровно файл с площадки: нарезана вторая плита, первой в архиве нет."""
    source = make_project(
        tmp_path / "part.gcode.3mf", 2, extras=("Metadata/plate_1.png", "Metadata/plate_1.json")
    )

    assert bambu.plate_in_project(source) == "Metadata/plate_2.gcode"


def test_a_thumbnail_is_not_a_plate(tmp_path) -> None:
    """`plate_1.png` есть и у плиты, которую никто не нарезал."""
    import zipfile

    source = tmp_path / "part.gcode.3mf"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("Metadata/plate_1.png", b"\x89PNG")
        archive.writestr("Metadata/plate_1.json", "{}")

    assert bambu.plate_in_project(source) is None


def test_an_unreadable_file_keeps_the_default_instead_of_inventing_one(tmp_path) -> None:
    """Пусть принтер откажет со своей причиной, а не с нашей выдуманной."""
    source = tmp_path / "part.gcode.3mf"
    source.write_bytes(b"not a zip at all")

    assert bambu.plate_in_project(source) is None


@pytest.mark.asyncio
async def test_the_upload_remembers_the_plate_and_the_start_asks_for_it(
    ftps, published, tmp_path
) -> None:
    """`start_print` знает только имя на принтере — заглянуть внутрь уже нечем.

    Поэтому плиту читают, пока локальная копия ещё здесь.
    """
    source = make_project(tmp_path / "part.gcode.3mf", 2)
    adapter = make_adapter()

    upload = await adapter.upload_file(source, "part.gcode.3mf")
    await adapter.start_print("f00d", "part.gcode.3mf")

    assert upload["plate"] == "Metadata/plate_2.gcode"
    assert published.messages[0]["print"]["param"] == "Metadata/plate_2.gcode"


@pytest.mark.asyncio
async def test_a_start_without_the_upload_falls_back_to_the_first_plate(published) -> None:
    """Перезапуск между загрузкой и стартом — прежнее поведение, не регрессия."""
    adapter = make_adapter()

    result = await adapter.start_print("f00d", "part.gcode.3mf")

    assert published.messages[0]["print"]["param"] == bambu.BAMBU_PROJECT_PLATE
    # Возвращается наверх, потому что MQTT ничего не подтверждает: печать,
    # которая не началась, диагностируется только по тому, что было послано.
    assert result["param"] == bambu.BAMBU_PROJECT_PLATE
