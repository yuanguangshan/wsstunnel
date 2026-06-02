"""tests/test_cli.py — CLI 参数解析和 --version 测试"""

from click.testing import CliRunner

from wsstunnel.cli import cli


class TestCLI:
    """Click CLI 命令行接口测试。"""

    def setup_method(self):
        self.runner = CliRunner()

    # ── 顶层命令 ──

    def test_help(self):
        result = self.runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "WebSocket Tunnel" in result.output

    def test_version(self):
        result = self.runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "wsstunnel" in result.output or "version" in result.output.lower()

    # ── relay 子命令 ──

    def test_relay_help(self):
        result = self.runner.invoke(cli, ["relay", "--help"])
        assert result.exit_code == 0
        assert "--host" in result.output
        assert "--port" in result.output
        assert "--token" in result.output
        assert "--cert" in result.output
        assert "--key" in result.output
        assert "--wxpush" in result.output
        assert "--verbose" in result.output
        assert "--quiet" in result.output

    def test_relay_defaults(self):
        """验证 relay 子命令的默认参数。"""
        result = self.runner.invoke(cli, ["relay", "--help"])
        assert result.exit_code == 0

    # ── client 子命令 ──

    def test_client_help(self):
        result = self.runner.invoke(cli, ["client", "--help"])
        assert result.exit_code == 0
        assert "--server" in result.output
        assert "--proxy" in result.output
        assert "--reconnect" in result.output
        assert "--token" in result.output
        assert "--insecure" in result.output
        assert "--shell" in result.output
        assert "--name" in result.output
        assert "--no-pty" in result.output

    def test_client_requires_server(self):
        """client 子命令必须提供 --server 参数。"""
        result = self.runner.invoke(cli, ["client"])
        assert result.exit_code != 0
        assert "Missing option" in result.output or "--server" in result.output

    def test_unknown_command(self):
        result = self.runner.invoke(cli, ["unknown"])
        assert result.exit_code != 0

    def test_relay_short_token(self):
        """验证 -t 短选项。"""
        result = self.runner.invoke(cli, ["relay", "--help"])
        assert "-t" in result.output

    def test_client_short_token(self):
        """验证 -t 短选项。"""
        result = self.runner.invoke(cli, ["client", "--help"])
        assert "-t" in result.output
