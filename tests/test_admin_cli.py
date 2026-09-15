import sys
from unittest.mock import AsyncMock

import pytest

from app.cli import __main__ as cli_main
from app.cli import promote_admin_cmd
from app.services.admin_promote import (
    PromoteAdminResult,
    PromoteAdminStatus,
)


class _Session:
    def __init__(self) -> None:
        self.commit = AsyncMock()
        self.rollback = AsyncMock()


class _SessionContext:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def __aenter__(self) -> _Session:
        return self.session

    async def __aexit__(self, *_args) -> None:
        return None


def _stub_cli_dependencies(monkeypatch, result: PromoteAdminResult):
    session = _Session()
    promote = AsyncMock(return_value=result)
    monkeypatch.setattr(
        promote_admin_cmd, "SessionLocal", lambda: _SessionContext(session)
    )
    monkeypatch.setattr(promote_admin_cmd, "promote_admin_by_email", promote)
    return session, promote


@pytest.mark.asyncio
async def test_promote_admin_cli_commits_success(monkeypatch, capsys):
    result = PromoteAdminResult(
        status=PromoteAdminStatus.promoted,
        email="owner@example.com",
        user_id=7,
    )
    session, promote = _stub_cli_dependencies(monkeypatch, result)

    exit_code = await promote_admin_cmd.run_promote_admin_cli("  Owner@Example.COM  ")

    assert exit_code == 0
    promote.assert_awaited_once_with(session, "  Owner@Example.COM  ")
    session.commit.assert_awaited_once_with()
    session.rollback.assert_not_awaited()
    captured = capsys.readouterr()
    assert captured.out == "promoted owner@example.com (user_id=7)\n"
    assert captured.err == ""


@pytest.mark.asyncio
async def test_promote_admin_cli_rolls_back_missing_user(monkeypatch, capsys):
    result = PromoteAdminResult(
        status=PromoteAdminStatus.not_found,
        email="missing@example.com",
    )
    session, promote = _stub_cli_dependencies(monkeypatch, result)

    exit_code = await promote_admin_cmd.run_promote_admin_cli("missing@example.com")

    assert exit_code == 1
    promote.assert_awaited_once_with(session, "missing@example.com")
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once_with()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "user not found: missing@example.com\n"


@pytest.mark.asyncio
async def test_promote_admin_cli_rolls_back_existing_admin(monkeypatch, capsys):
    result = PromoteAdminResult(
        status=PromoteAdminStatus.already_admin,
        email="admin@example.com",
        user_id=9,
    )
    session, promote = _stub_cli_dependencies(monkeypatch, result)

    exit_code = await promote_admin_cmd.run_promote_admin_cli("admin@example.com")

    assert exit_code == 2
    promote.assert_awaited_once_with(session, "admin@example.com")
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once_with()
    captured = capsys.readouterr()
    assert captured.out == "already admin: admin@example.com (user_id=9)\n"
    assert captured.err == ""


def test_cli_main_runs_promote_admin_and_exits(monkeypatch):
    promote = AsyncMock(return_value=2)
    monkeypatch.setattr(cli_main, "run_promote_admin_cli", promote)
    monkeypatch.setattr(
        sys,
        "argv",
        ["python -m app.cli", "promote-admin", "admin@example.com"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main()

    assert exc_info.value.code == 2
    promote.assert_awaited_once_with("admin@example.com")
