from unittest.mock import AsyncMock

import pytest

from app.jobs import telegram_daily_balance


class _SessionContext:
    def __init__(self, session: object) -> None:
        self.session = session

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, *_args) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivery_result", "expected_exit_code", "expected_output"),
    [
        ((3, 0), 0, "telegram daily balance: sent=3 failed=0\n"),
        ((2, 1), 1, "telegram daily balance: sent=2 failed=1\n"),
    ],
)
async def test_run_reports_delivery_counts_and_exit_status(
    monkeypatch,
    capsys,
    delivery_result,
    expected_exit_code,
    expected_output,
):
    settings = object()
    session = object()
    send_due = AsyncMock(return_value=delivery_result)
    monkeypatch.setattr(telegram_daily_balance, "get_settings", lambda: settings)
    monkeypatch.setattr(
        telegram_daily_balance,
        "SessionLocal",
        lambda: _SessionContext(session),
    )
    monkeypatch.setattr(
        telegram_daily_balance,
        "send_due_daily_balances",
        send_due,
    )

    exit_code = await telegram_daily_balance._run()

    assert exit_code == expected_exit_code
    send_due.assert_awaited_once_with(session, settings)
    captured = capsys.readouterr()
    assert captured.out == expected_output
    assert captured.err == ""


def test_main_exits_with_run_result(monkeypatch):
    run = AsyncMock(return_value=1)
    monkeypatch.setattr(telegram_daily_balance, "_run", run)

    with pytest.raises(SystemExit) as exc_info:
        telegram_daily_balance.main()

    assert exc_info.value.code == 1
    run.assert_awaited_once_with()
