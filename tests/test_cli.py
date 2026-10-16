import yaml

from wortling.cli import main


def test_report_defaults_to_first_configured_learner(settings, tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(settings.model_dump(mode="json")))
    monkeypatch.setattr("sys.argv", ["wortling", "--config", str(config), "report"])
    seen = []

    def report(db, user):
        seen.append(user.id)
        return "ready"

    monkeypatch.setattr("wortling.report.write_report", report)
    main()
    assert seen == [settings.users[0].id]
    assert capsys.readouterr().out.strip() == "ready"


def test_setup_link_uses_private_origin_environment(settings, tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(settings.model_dump(mode="json")))
    monkeypatch.setenv("WORTLING_BASE_URL", "https://service.example.invalid")
    monkeypatch.setattr("sys.argv", ["wortling", "--config", str(config), "setup-link"])
    seen = []

    def link(db, user_id, base_url):
        seen.append((user_id, base_url))
        return "test-link"

    monkeypatch.setattr("wortling.browser_auth.issue_setup_link", link)
    main()
    assert seen == [(settings.users[0].id, "https://service.example.invalid")]
    assert capsys.readouterr().out.strip() == "test-link"
