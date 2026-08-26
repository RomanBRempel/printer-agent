"""Unattended updates: on a schedule, and only when a restart costs nothing.

The rule under test is what "safe to restart" means. A print is not a reason to
wait — the printer runs it alone, and the outbox carries unacked events across a
restart. A file half-way to a printer is, because that work only exists in the
agent's memory.
"""

from __future__ import annotations

import asyncio

import pytest

from printer_agent.config import AgentConfig, UpdateConfig, validate_config
from printer_agent.updater import AutoUpdater
from printer_agent.updates import UpdateManifest, UpdateStatus
from printer_agent import updater as updater_module

FEED = "https://hub.example.com/printer-agent-update.json"
MANIFEST = UpdateManifest(version="9.9.9", package_url="https://hub.example.com/x.whl", sha256="ab")


def make_config(**overrides) -> AgentConfig:
    updates = UpdateConfig(
        feed_url=FEED, auto_update=True, check_on_startup=True, check_interval_h=0
    )
    for name, value in overrides.items():
        setattr(updates, name, value)
    return AgentConfig(
        hub_url="https://hub.example.com/api/printers/agent",
        agent_token="secret-token",
        location_key="loc-001",
        updates=updates,
    )


@pytest.fixture
def hub_release(monkeypatch):
    """A feed offering 9.9.9, and a recording installer."""
    calls: dict[str, int] = {"checked": 0, "applied": 0}

    def check(feed_url):
        calls["checked"] += 1
        return UpdateStatus(
            current_version="0.0.1",
            latest_version=MANIFEST.version,
            update_available=True,
            manifest=MANIFEST,
        )

    def apply(manifest):
        calls["applied"] += 1
        return UpdateStatus(
            current_version="0.0.1",
            latest_version=manifest.version,
            update_available=True,
            manifest=manifest,
            installed=True,
            message="Update installed successfully.",
        )

    monkeypatch.setattr(updater_module, "check_for_update", check)
    monkeypatch.setattr(updater_module, "apply_update", apply)
    monkeypatch.setattr(updater_module, "IDLE_POLL_S", 0.01)
    return calls


@pytest.mark.asyncio
async def test_an_idle_agent_installs_and_restarts_without_being_asked(hub_release) -> None:
    restarts: list[int] = []
    updater = AutoUpdater(make_config(), is_busy=lambda: False, restart=lambda: restarts.append(1))

    await updater.run()

    assert hub_release["applied"] == 1
    assert restarts == [1]


@pytest.mark.asyncio
async def test_a_transfer_in_flight_holds_the_update_back(hub_release) -> None:
    """Restarting mid-download loses the file the printer is waiting for."""
    busy = [True]
    restarts: list[int] = []
    updater = AutoUpdater(
        make_config(), is_busy=lambda: busy[0], restart=lambda: restarts.append(1)
    )

    task = asyncio.create_task(updater.run())
    await asyncio.sleep(0.05)
    installed_while_busy = hub_release["applied"]
    busy[0] = False
    await asyncio.wait_for(task, timeout=2)

    assert installed_while_busy == 0
    assert hub_release["applied"] == 1
    assert restarts == [1]


@pytest.mark.asyncio
async def test_a_busy_agent_gives_up_rather_than_waiting_forever(hub_release, monkeypatch) -> None:
    """A camera left open all day must not hold a release back indefinitely."""
    monkeypatch.setattr(updater_module, "IDLE_WAIT_LIMIT_S", 0.03)
    restarts: list[int] = []
    updater = AutoUpdater(make_config(), is_busy=lambda: True, restart=lambda: restarts.append(1))

    await asyncio.wait_for(updater.run(), timeout=2)

    assert hub_release["applied"] == 0
    assert restarts == []


@pytest.mark.asyncio
async def test_without_auto_update_the_release_is_only_announced(hub_release) -> None:
    restarts: list[int] = []
    updater = AutoUpdater(
        make_config(auto_update=False), is_busy=lambda: False, restart=lambda: restarts.append(1)
    )

    await updater.run()

    assert hub_release["checked"] == 1
    assert hub_release["applied"] == 0
    assert restarts == []


@pytest.mark.asyncio
async def test_a_failed_install_is_not_retried_in_a_loop(monkeypatch) -> None:
    attempts: list[str] = []

    def check(feed_url):
        return UpdateStatus(
            current_version="0.0.1",
            latest_version=MANIFEST.version,
            update_available=True,
            manifest=MANIFEST,
        )

    def apply(manifest):
        attempts.append(manifest.version)
        return UpdateStatus(
            current_version="0.0.1",
            latest_version=manifest.version,
            update_available=True,
            manifest=manifest,
            installed=False,
            message="pip refused the package",
        )

    monkeypatch.setattr(updater_module, "check_for_update", check)
    monkeypatch.setattr(updater_module, "apply_update", apply)
    restarts: list[int] = []
    updater = AutoUpdater(make_config(), is_busy=lambda: False, restart=lambda: restarts.append(1))

    await updater._cycle()
    await updater._cycle()

    assert attempts == [MANIFEST.version]
    assert restarts == []


@pytest.mark.asyncio
async def test_an_unreachable_feed_does_not_stop_the_agent(monkeypatch) -> None:
    def check(feed_url):
        raise OSError("the shop link is down")

    monkeypatch.setattr(updater_module, "check_for_update", check)
    updater = AutoUpdater(make_config(), is_busy=lambda: False, restart=lambda: None)

    await updater.run()  # no exception escapes


@pytest.mark.asyncio
async def test_a_periodic_check_keeps_running_after_the_first_one(hub_release) -> None:
    """The old code checked once at service start; a box up for weeks never saw a release."""
    config = make_config(check_on_startup=False, check_interval_h=1)
    updater = AutoUpdater(config, is_busy=lambda: True, restart=lambda: None)
    # An hour of sleeping is a wait on the stop event, so stopping cuts it short.
    task = asyncio.create_task(updater.run())
    await asyncio.sleep(0.02)
    updater.stop()
    await asyncio.wait_for(task, timeout=2)

    assert hub_release["checked"] == 0  # nothing due yet, and the stop was honoured


@pytest.mark.asyncio
async def test_auto_update_without_a_feed_is_inert_rather_than_fatal(hub_release) -> None:
    """An agent already running that combination must not refuse to start.

    The setting is pointless — there is nothing to check — but turning it into a
    config error would take a working install down at the next update, which is
    a worse outcome than a useless flag.
    """
    config = make_config(feed_url="", auto_update=True)

    assert [error for error in validate_config(config) if "update" in error] == []

    await AutoUpdater(config, is_busy=lambda: False, restart=lambda: None).run()
    assert hub_release["checked"] == 0


def test_the_service_restarts_itself_through_a_detached_child() -> None:
    """A service cannot restart itself: the process issuing the start has to
    outlive the stop, and every thread of this one is gone by then."""
    from printer_agent.windows_service import SERVICE_NAME, recovery_command, restart_command

    command = restart_command()

    assert command[:2] == ["cmd.exe", "/c"]
    script = command[2]
    assert f"sc stop {SERVICE_NAME}" in script
    assert f"sc start {SERVICE_NAME}" in script
    # The start has to wait out the stop, or it fails with "service is stopping".
    assert script.index("sc stop") < script.index("ping") < script.index("sc start")

    # Recovery covers a crash, not a deliberate stop: no failureflag.
    recovery = recovery_command()
    assert recovery[:3] == ["sc.exe", "failure", SERVICE_NAME]
    assert "restart/5000" in recovery[-1]
    assert not any("failureflag" in part for part in recovery)


# ─── Обновление по команде хаба ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nothing_is_claimed_about_a_feed_never_checked() -> None:
    """«Не проверяли» и «обновлений нет» — разные утверждения.

    Хаб показывает это рядом с агентом. Отдай мы здесь «актуален», интерфейс
    объявил бы агента свежим, ничего об этом не зная, — то же молчание, что и у
    ошибки принтера без отметки времени.
    """
    updater = AutoUpdater(make_config(), is_busy=lambda: False, restart=lambda: None)

    assert updater.known_status() is None


@pytest.mark.asyncio
async def test_a_check_leaves_a_dated_answer(hub_release) -> None:
    """У снимка обязана быть отметка времени: без неё он не стареет на вид."""
    updater = AutoUpdater(
        make_config(auto_update=False), is_busy=lambda: False, restart=lambda: None
    )

    await updater.run()
    status = updater.known_status()

    assert status is not None
    assert status["update_available"] is True
    assert status["latest_version"] == MANIFEST.version
    assert status["checked_at"], "снимок без времени читается как свежий всегда"
    # Копия, а не внутреннее состояние: снимок уезжает в сообщение.
    status["latest_version"] = "подменили"
    assert updater.known_status()["latest_version"] == MANIFEST.version


@pytest.mark.asyncio
async def test_the_hub_can_install_even_when_the_schedule_is_off(hub_release) -> None:
    """`auto_update` тут не спрашивается: это оператор ставит обновление сам."""
    restarts: list[int] = []
    updater = AutoUpdater(
        make_config(auto_update=False, check_on_startup=False),
        is_busy=lambda: False,
        restart=lambda: restarts.append(1),
        restarts_itself=True,
    )

    answer = await updater.update_now()

    assert answer["scheduled"] is True
    assert answer["latest_version"] == MANIFEST.version
    assert answer["waiting_for_idle"] is False
    assert answer["restarts_itself"] is True
    await asyncio.sleep(0.05)
    assert hub_release["applied"] == 1


@pytest.mark.asyncio
async def test_being_up_to_date_is_an_answer_not_a_failure(monkeypatch) -> None:
    """Кнопку жмут, не зная наверняка, — «уже последняя» это ответ."""
    monkeypatch.setattr(
        updater_module,
        "check_for_update",
        lambda feed_url: UpdateStatus(
            current_version="9.9.9", latest_version="9.9.9", update_available=False
        ),
    )
    updater = AutoUpdater(make_config(), is_busy=lambda: False, restart=lambda: None)

    answer = await updater.update_now()

    assert answer["scheduled"] is False
    assert answer["reason"] == "already_latest"


@pytest.mark.asyncio
async def test_a_version_nobody_asked_for_is_not_installed(hub_release) -> None:
    """Оператор ждёт одно конкретное число; «примерно то же» здесь неверно."""
    updater = AutoUpdater(make_config(), is_busy=lambda: False, restart=lambda: None)

    with pytest.raises(updater_module.UpdateUnavailable):
        await updater.update_now(target_version="1.2.3")
    assert hub_release["applied"] == 0


@pytest.mark.asyncio
async def test_without_a_feed_the_refusal_names_the_reason() -> None:
    """Иначе в ленте команд это читается как поломка агента."""
    config = make_config()
    config.updates.feed_url = ""
    updater = AutoUpdater(config, is_busy=lambda: False, restart=lambda: None)

    with pytest.raises(updater_module.UpdateUnavailable):
        await updater.update_now()

