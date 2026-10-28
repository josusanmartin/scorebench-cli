import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from unittest import mock

from scorebench_cli import cli


def test_cli_entrypoint_lists_commands():
    result = subprocess.run(
        [sys.executable, "-c", "from scorebench_cli import cli; import sys; sys.argv=['harness']; cli.main()"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "submit" in result.stderr and "leaderboard" in result.stderr


def test_run_help_lists_progress_command():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from scorebench_cli import cli; import sys; sys.argv=['scorebench','run','--help']; cli.main()",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "progress" in result.stdout
    assert "canonical submitted active time" in result.stdout


def test_run_progress_calls_scoped_endpoint():
    context = cli.HarnessCliContext(
        url="https://scorebench.example/",
        token="hrun_test",
    )
    client = mock.Mock()
    client.get.return_value = {
        "scope": {"kind": "run_token", "run_id": "run-one"},
        "progress": {"run_id": "run-one", "active_seconds": 10},
    }
    output = io.StringIO()

    with (
        mock.patch.object(cli, "load_cli_context", return_value=context),
        mock.patch.object(cli, "HarnessClient", return_value=client) as client_type,
        redirect_stdout(output),
    ):
        assert cli._run(["run", "progress"]) == 0

    client_type.assert_called_once_with(
        "https://scorebench.example/", arm_token="hrun_test"
    )
    client.get.assert_called_once_with("/run/progress")
    assert json.loads(output.getvalue())["progress"]["active_seconds"] == 10


def test_browser_safe_url_only_admits_https_or_loopback_http():
    assert cli._browser_safe_url("https://scorebench.dev/admin/cli-login")
    assert cli._browser_safe_url("http://127.0.0.1:8000/admin/cli-login")
    for url in ("http://scorebench.dev/login", "javascript:alert(1)", "file:///etc/passwd", "https://[bad"):
        assert not cli._browser_safe_url(url)
