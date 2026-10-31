from typer.testing import CliRunner

from trustmerge import __version__
from trustmerge.cli import app

runner = CliRunner()


def test_version_command():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout
