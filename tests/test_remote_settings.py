"""Удалённая настройка агента через хаб.

Опасность здесь не в форме сообщения, а в двух правилах, которые ломаются молча.

Первое: секрет, который агент показал хабу как `__redacted__`, не должен вернуться
обратно как строка `__redacted__`. Хаб рисует форму из того, что мы прислали, и
сохраняет её целиком — если маркер записать буквально, рабочий код доступа будет
затёрт, а принтер отвалится не в момент сохранения, а при следующей печати.

Второе: конфиг, который агент отказывается загрузить, не должен попасть на диск.
Хаб — единственный канал, которым площадку можно чинить удалённо; форма, способная
сделать файл нечитаемым, снимает площадку с эфира одним нажатием.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from printer_agent.config import AgentConfig, PrinterConfig, load_config_file, save_config
from printer_agent.contracts import HUB_TO_AGENT_TYPES, REDACTED, MessageType
from printer_agent.settings_bundle import (
    REMOTE_BLOCKED_KEYS,
    apply_remote_settings,
    readonly_settings,
    redacted_settings,
)


def make_config(**overrides: Any) -> AgentConfig:
    config = AgentConfig(
        hub_url="https://hub.example.com/api/printers/agent",
        agent_token="secret-token",
        location_key="loc-001",
        printers=[
            PrinterConfig(key="k2plus", brand="moonraker", host="10.13.0.130", port=7125),
            PrinterConfig(
                key="a1-mini",
                brand="bambu",
                host="10.13.0.126",
                credentials={"access_code": "12345678", "serial": "0309ABCDEF"},
            ),
        ],
    )
    for name, value in overrides.items():
        setattr(config, name, value)
    return config


# ── Что видит хаб ───────────────────────────────────────────────────────────


def test_no_secret_ever_travels_upward() -> None:
    """Агент не отдаёт коды доступа наверх, о чём бы хаб ни спросил."""
    settings = redacted_settings(make_config())

    bambu = next(p for p in settings["printers"] if p["key"] == "a1-mini")
    assert bambu["credentials"]["access_code"] == REDACTED
    assert "12345678" not in repr(settings)


def test_a_serial_is_not_a_secret_and_stays_readable() -> None:
    """Серийник опознаёт принтер, а не даёт к нему доступ.

    Спрятав его, мы получили бы карточку принтера, по которой нельзя понять,
    какая это машина.
    """
    settings = redacted_settings(make_config())

    bambu = next(p for p in settings["printers"] if p["key"] == "a1-mini")
    assert bambu["credentials"]["serial"] == "0309ABCDEF"


def test_a_set_secret_and_an_unset_one_look_different() -> None:
    """Иначе форма на хабе сохранит пустоту поверх рабочего кода доступа."""
    config = make_config()
    config.printers[1].credentials = {"serial": "0309ABCDEF"}

    settings = redacted_settings(config)
    bambu = next(p for p in settings["printers"] if p["key"] == "a1-mini")

    assert "access_code" not in bambu["credentials"]


def test_the_token_is_not_echoed_back_to_the_hub() -> None:
    """Хаб этим токеном и аутентифицировал сессию — возвращать его незачем."""
    config = make_config()

    assert "agent_token" not in redacted_settings(config)
    assert "agent_token" not in readonly_settings(config)
    assert readonly_settings(config)["hub_url"] == config.hub_url


# ── Что хаб может изменить ──────────────────────────────────────────────────


def test_an_omitted_key_keeps_what_the_agent_has() -> None:
    """Правка, а не зеркало: файл остаётся правдой по всему, чего хаб не назвал."""
    current = make_config(telemetry_interval_s=5, heartbeat_interval_s=15)

    merged, report = apply_remote_settings({"telemetry_interval_s": 30}, current)

    assert merged.telemetry_interval_s == 30
    assert merged.heartbeat_interval_s == 15
    assert "telemetry_interval_s" in report.applied
    assert [p.key for p in merged.printers] == ["k2plus", "a1-mini"]
    assert "printers" in report.kept_local


def test_the_printer_list_replaces_wholesale_because_removal_needs_it() -> None:
    """Другого способа сказать «этого принтера больше нет» на проводе нет."""
    current = make_config()

    merged, _ = apply_remote_settings(
        {"printers": [{"key": "k2plus", "brand": "moonraker", "host": "10.13.0.130"}]}, current
    )

    assert [p.key for p in merged.printers] == ["k2plus"]


def test_a_printer_can_be_added_with_its_access_code() -> None:
    """Ради этого всё и делается: завести Bambu, не выезжая на площадку."""
    current = make_config()
    incoming = [
        {"key": "k2plus", "brand": "moonraker", "host": "10.13.0.130"},
        {"key": "a1-mini", "brand": "bambu", "host": "10.13.0.126"},
        {
            "key": "jekson-p1s",
            "brand": "bambu",
            "host": "10.13.0.140",
            "credentials": {"access_code": "87654321", "serial": "0309XYZ"},
        },
    ]

    merged, report = apply_remote_settings({"printers": incoming}, current)

    added = next(p for p in merged.printers if p.key == "jekson-p1s")
    assert added.credentials["access_code"] == "87654321"
    assert not report.missing


# ── Секреты в обратную сторону ──────────────────────────────────────────────


def test_the_redaction_marker_means_keep_what_you_have() -> None:
    """Хаб рисует форму из нашего же ответа и сохраняет её целиком.

    Если записать маркер буквально, принтер отвалится не при сохранении, а при
    следующей печати — и причину будет не найти.
    """
    current = make_config()
    posted_back = redacted_settings(current)

    merged, report = apply_remote_settings(posted_back, current)

    bambu = next(p for p in merged.printers if p.key == "a1-mini")
    assert bambu.credentials["access_code"] == "12345678"
    assert "printers.a1-mini.credentials.access_code" in report.kept_local


def test_an_omitted_secret_also_keeps_what_the_agent_has() -> None:
    """Хаб, который секретов не хранит, всё равно должен уметь править хост."""
    current = make_config()

    merged, _ = apply_remote_settings(
        {"printers": [{"key": "a1-mini", "brand": "bambu", "host": "10.13.0.200"}]}, current
    )

    bambu = merged.printers[0]
    assert bambu.host == "10.13.0.200"
    assert bambu.credentials["access_code"] == "12345678"


def test_only_an_explicit_null_takes_a_credential_away() -> None:
    """Отозванный код доступа должен уходить, иначе его не отозвать удалённо."""
    current = make_config()

    merged, report = apply_remote_settings(
        {
            "printers": [
                {
                    "key": "a1-mini",
                    "brand": "bambu",
                    "host": "10.13.0.126",
                    "credentials": {"access_code": None, "serial": "0309ABCDEF"},
                }
            ]
        },
        current,
    )

    assert "access_code" not in merged.printers[0].credentials
    assert any("cleared" in line for line in report.applied)


# ── Чего хаб не может ───────────────────────────────────────────────────────


@pytest.mark.parametrize("field", REMOTE_BLOCKED_KEYS)
def test_the_session_identity_is_refused_not_dropped(field: str) -> None:
    """Молча проглоченная правка читается оператором как сработавшая.

    Ошибка в этих полях к тому же необратима удалённо: чинить агента станет
    нечем, кроме выезда.
    """
    current = make_config()

    merged, report = apply_remote_settings(
        {field: "https://evil.example.com", "telemetry_interval_s": 20}, current
    )

    assert field in report.rejected
    assert getattr(merged, field) == getattr(current, field)
    assert merged.telemetry_interval_s == 20


def test_the_outbox_path_is_refused_but_its_size_is_not() -> None:
    """Путь к открытой базе менять нельзя; сколько в ней событий — можно."""
    current = make_config()

    merged, report = apply_remote_settings(
        {"outbox": {"database_path": "D:/elsewhere.sqlite3", "max_events": 9000}}, current
    )

    assert "database_path" in report.rejected
    assert merged.outbox.database_path == current.outbox.database_path
    assert merged.outbox.max_events == 9000


# ── Форма на проводе ────────────────────────────────────────────────────────


def test_both_new_hub_messages_are_accepted_types() -> None:
    """Иначе агент отвечал бы на них строкой «ignored message»."""
    assert MessageType.settings_request.value in HUB_TO_AGENT_TYPES
    assert MessageType.settings_update.value in HUB_TO_AGENT_TYPES


def test_a_saved_config_round_trips_through_the_file(tmp_path: Path) -> None:
    """То, что записано после правки, агент обязан суметь прочитать обратно."""
    current = make_config()
    merged, _ = apply_remote_settings({"telemetry_interval_s": 42}, current)
    path = tmp_path / "agent.yaml"

    save_config(merged, path)
    reread = load_config_file(path)

    assert reread.telemetry_interval_s == 42
    assert reread.printers[1].credentials["access_code"] == "12345678"


# ── На проводе, целиком ─────────────────────────────────────────────────────


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    def payloads_of(self, message_type: str) -> list[dict[str, Any]]:
        return [e["payload"] for e in self.sent if e.get("type") == message_type]


@pytest.fixture
def hub(tmp_path: Path):
    from printer_agent.config import OutboxConfig, PrintFilesConfig
    from printer_agent.core.outbox import EventOutbox
    from printer_agent.uplink.connection import HubConnection

    config = make_config()
    config.outbox = OutboxConfig(database_path=tmp_path / "outbox.sqlite3")
    config.print_files = PrintFilesConfig(directory=tmp_path / "print-files")
    config.source_path = tmp_path / "agent.yaml"
    save_config(config, config.source_path)

    outbox = EventOutbox(config.outbox.database_path)
    connection = HubConnection(config, outbox)
    try:
        yield connection, outbox
    finally:
        outbox.close()


def update(**settings: Any) -> dict[str, Any]:
    from printer_agent.contracts import build_envelope

    return build_envelope("settings_update", {"command_id": "cmd-9", "settings": settings})


@pytest.mark.asyncio
async def test_an_accepted_update_reaches_the_file_and_is_answered(hub) -> None:
    """Ответ говорит «принято»; подействует оно на следующем цикле опроса."""
    connection, _outbox = hub
    ws = FakeWebSocket()

    await connection._handle_message(ws, update(telemetry_interval_s=30))

    result = ws.payloads_of("command_result")[0]
    assert result["command_id"] == "cmd-9"
    assert result["status"] == "done"
    # Про агента, а не про принтер — поле пустое, но присутствует.
    assert result["printer_key"] == ""
    assert "telemetry_interval_s" in result["response"]["applied"]
    assert load_config_file(connection.config.source_path).telemetry_interval_s == 30


@pytest.mark.asyncio
async def test_an_unloadable_config_never_reaches_the_disk(hub) -> None:
    """Форма на хабе не должна уметь снять площадку с эфира.

    Хаб — единственный канал удалённого ремонта; агент, чей файл не читается,
    чинится только выездом.
    """
    connection, _outbox = hub
    before = (connection.config.source_path).read_text(encoding="utf-8")
    ws = FakeWebSocket()

    await connection._handle_message(
        ws, update(printers=[{"key": "nope", "brand": "not-a-brand", "host": "10.0.0.1"}])
    )

    result = ws.payloads_of("command_result")[0]
    assert result["status"] == "failed"
    assert "not-a-brand" in result["error_text"]
    assert (connection.config.source_path).read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_a_change_set_of_only_refused_fields_fails_loudly(hub) -> None:
    """«Принято» на правку, которой не было, — худший из возможных ответов."""
    connection, _outbox = hub
    ws = FakeWebSocket()

    await connection._handle_message(ws, update(hub_url="https://elsewhere.example.com"))

    result = ws.payloads_of("command_result")[0]
    assert result["status"] == "failed"
    assert "hub_url" in result["error_text"]
    assert load_config_file(connection.config.source_path).hub_url == make_config().hub_url


@pytest.mark.asyncio
async def test_a_redelivered_update_is_not_applied_twice(hub) -> None:
    """Сессия могла оборваться, пока ответ был в пути."""
    connection, _outbox = hub
    ws = FakeWebSocket()

    await connection._handle_message(ws, update(telemetry_interval_s=30))
    await connection._handle_message(ws, update(telemetry_interval_s=99))

    first, replay = ws.payloads_of("command_result")
    assert replay["status"] == first["status"] == "done"
    assert replay["response"] == first["response"]
    # Второй раз ничего не применялось: ответ пришёл из сохранённого результата.
    assert load_config_file(connection.config.source_path).telemetry_interval_s == 30


@pytest.mark.asyncio
async def test_a_settings_request_is_answered_without_secrets(hub) -> None:
    from printer_agent.contracts import build_envelope

    connection, _outbox = hub
    ws = FakeWebSocket()

    await connection._handle_message(ws, build_envelope("settings_request", {}))

    payload = ws.payloads_of("settings")[0]
    assert payload["request_msg_id"]
    assert payload["readonly"]["hub_url"] == connection.config.hub_url
    assert "12345678" not in repr(payload)
    assert "secret-token" not in repr(payload)
