"""Lifecycle checks use fake processes; never start a real worker or tunnel."""

from unittest.mock import MagicMock

import pytest

from scripts import local_services as services


def test_reused_pid_is_not_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(services, "STATE", tmp_path)
    monkeypatch.setattr(services, "stamp", lambda pid: "different process")
    kill = MagicMock()
    monkeypatch.setattr(services.os, "kill", kill)
    data = {"runner": {"pid": 123, "stamp": "original process"}}
    assert services.stop_one("runner", data)
    kill.assert_not_called()
    assert data == {}


def test_busy_runner_is_not_force_killed(tmp_path, monkeypatch):
    monkeypatch.setattr(services, "STATE", tmp_path)
    monkeypatch.setattr(services, "stamp", lambda pid: "runner")
    monkeypatch.setattr(services.time, "sleep", lambda seconds: None)
    kill = MagicMock()
    monkeypatch.setattr(services.os, "kill", kill)
    data = {"runner": {"pid": 123, "stamp": "runner"}}
    assert not services.stop_one("runner", data)
    kill.assert_called_once_with(123, services.signal.SIGTERM)
    assert "runner" in data


def test_start_skips_all_managed_processes(monkeypatch):
    monkeypatch.setattr(services, "commands", lambda: ({}, {}))
    monkeypatch.setattr(services, "stamp", lambda pid: "same")
    monkeypatch.setattr(services.subprocess, "check_output", lambda *a, **kw: "")
    popen = MagicMock()
    monkeypatch.setattr(services.subprocess, "Popen", popen)
    services.start({name: {"pid": 123, "stamp": "same"} for name in services.NAMES})
    popen.assert_not_called()


def test_external_runner_blocks_start(monkeypatch):
    import socket

    monkeypatch.setattr(services, "commands", lambda: ({}, {}))
    monkeypatch.setattr(socket, "socket", MagicMock())
    monkeypatch.setattr(
        services.subprocess, "check_output", lambda *a, **kw: "123 python -m app.runner"
    )
    popen = MagicMock()
    monkeypatch.setattr(services.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match="脚本以外的 runner"):
        services.start({})
    popen.assert_not_called()


def test_port_check_rejects_live_listener():
    import socket

    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with pytest.raises(RuntimeError, match="暂不可用"):
            services.check_port("web", server.getsockname()[1])


def test_port_check_accepts_recently_closed_connection():
    import socket

    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.listen(1)
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            connection, _ = server.accept()
            connection.close()  # Server closes first, leaving the accepted connection in TIME_WAIT.
            assert client.recv(1) == b""
    services.check_port("web", port)
