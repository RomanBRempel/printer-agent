"""Service-control error text has to be readable, not a PowerShell dump."""

from __future__ import annotations

from printer_agent.desktop.system import condense_powershell_error

# What Start-Service actually emits, minus the code-page damage.
START_SERVICE_FAILURE = """Start-Service : Не удалось запустить службу "printer-agent (printer-agent)" на этом компьютере.
строка:1 знак:1
+ Start-Service -Name 'printer-agent'
+ ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : OpenError: (System.ServiceProcess.ServiceController:ServiceController) [Start-Service],
   ServiceCommandException
    + FullyQualifiedErrorId : CouldNotStartService,Microsoft.PowerShell.Commands.StartServiceCommand
"""


def test_only_the_sentence_that_means_something_survives():
    condensed = condense_powershell_error(START_SERVICE_FAILURE)

    assert condensed == 'Не удалось запустить службу "printer-agent (printer-agent)" на этом компьютере.'


def test_the_cmdlet_prefix_and_noise_are_dropped():
    condensed = condense_powershell_error(START_SERVICE_FAILURE)

    assert "Start-Service :" not in condensed
    assert "CategoryInfo" not in condensed
    assert "FullyQualifiedErrorId" not in condensed
    assert "~~~" not in condensed


def test_a_plain_message_is_left_alone():
    assert condense_powershell_error("Отказано в доступе.") == "Отказано в доступе."


def test_empty_input_does_not_crash():
    assert condense_powershell_error("") == ""
    assert condense_powershell_error("   \n  \n") == ""


def test_a_message_without_a_cmdlet_prefix_keeps_its_colon():
    text = "Access is denied: 0x5"

    assert condense_powershell_error(text) == "Access is denied: 0x5"
