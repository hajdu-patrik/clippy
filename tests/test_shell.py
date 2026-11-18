import os
import sys

import pytest

from clippy import control, shell
from clippy.cli import facecam_box
from clippy.tools import ClippyError


def test_every_command_and_clip_flag_is_in_both_completion_scripts():
    ps, lua = shell.powershell_script(), shell.clink_script()
    for name in shell.COMMANDS:
        assert f"'{name}'" in ps and f'"{name}" .. k_{name}' in lua
    flags = [f for f, _, _ in shell.clip_flags()]
    assert "--layout" in flags and "--cta" in flags and "--help" not in flags
    for f in flags:
        assert f"'{f}'" in ps and f'"{f}"' in lua
    assert "'clip|--layout' = @('auto', 'face', 'center', 'blur', 'stream')" in ps
    assert '"--layout" .. p():addarg({"auto", "face", "center", "blur", "stream"})' in lua


def test_banner_lists_every_command(capsys, monkeypatch):
    monkeypatch.setattr(control, "running", lambda: None)
    control.banner()
    out = capsys.readouterr().out
    assert "Welcome to Clippy" in out and "Not running" in out
    for name in shell.COMMANDS:
        assert f"  {name}" in out


def test_unknown_command_shows_help(capsys, monkeypatch):
    monkeypatch.setattr(control, "running", lambda: None)
    assert control.main(["bogus"]) == 2
    assert "Unknown command: bogus" in capsys.readouterr().out


def test_clip_subcommand_delegates_to_the_cli(monkeypatch):
    seen = {}
    import clippy.cli

    monkeypatch.setattr(clippy.cli, "main", lambda argv: seen.setdefault("argv", argv) and 0)
    control.main(["clip", "https://youtu.be/x", "-n", "2"])
    assert seen["argv"] == ["https://youtu.be/x", "-n", "2"]


def test_every_listed_command_has_exactly_one_handler():
    assert set(control.ACTIONS) == set(shell.COMMANDS)
    assert set(control.ALIASES.values()) <= set(control.ACTIONS)


class FakeProcesses:
    def __init__(self):
        self.killed, self.ollama = [], False

    def alive(self, pid):
        return pid == 41

    def name(self, pid):
        return {41: "python.exe", 77: "python3.12", 99: "nginx"}[pid]

    def listening(self, port):
        return [41, 77, 99]

    def kill_tree(self, pid):
        self.killed.append(pid)

    def kill_ollama(self):
        self.ollama = True


def test_down_stops_the_server_and_its_python_listeners_only(tmp_path, monkeypatch):
    pid_file, processes = tmp_path / "server.pid", FakeProcesses()
    pid_file.write_text("41", encoding="utf-8")
    monkeypatch.setattr(control, "PROCESSES", processes)
    monkeypatch.setattr(control, "PID_FILE", pid_file)
    monkeypatch.setattr(control, "running", lambda: None)
    monkeypatch.setattr(control, "unload_all", lambda: [])
    assert control.down(stop_ollama=True) == 0
    assert processes.killed == [41, 77] and processes.ollama and not pid_file.exists()


def test_a_missing_system_tool_reads_as_no_output():
    assert control._system(["clippy-no-such-tool-here"]) == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_posix_processes_see_a_live_process():
    assert control.PosixProcesses().alive(os.getpid())


def test_a_malformed_facecam_is_a_clear_error():
    assert facecam_box("10,20,300,200") == (10, 20, 300, 200) and facecam_box("") is None
    for bad in ("10,20,300", "a,b,c,d"):
        with pytest.raises(ClippyError) as caught:
            facecam_box(bad)
        assert caught.value.key == "error.facecam_format"
