from clippy import control, shell


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
