from notecli.cli import main


def test_add_and_list(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NOTECLI_HOME", str(tmp_path))
    assert main(["add", "buy milk"]) == 0
    assert main(["list"]) == 0
    assert "1. buy milk" in capsys.readouterr().out
