import subprocess
import sys


def test_cli_entrypoint_lists_commands():
    result = subprocess.run(
        [sys.executable, "-c", "from scorebench_cli import cli; import sys; sys.argv=['harness']; cli.main()"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "submit" in result.stderr and "leaderboard" in result.stderr
