import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def cli():
    path = Path(__file__).parents[1] / 'browser_cli.py'
    spec = importlib.util.spec_from_file_location('browser_cli', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_discovery_keeps_aws_host_port_and_signature(cli, monkeypatch):
    signed = 'wss://bedrock-agentcore.us-east-1.amazonaws.com/browser-streams/aws.browser.v1/sessions/abc/automation?X-Amz-Signature=exact%2Fbytes'
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, _limit): return json.dumps({'webSocketDebuggerUrl': signed}).encode()
    monkeypatch.setenv('AGENT_BROWSER_CDP', cli.DISCOVERY)
    monkeypatch.setattr(cli.urllib.request, 'urlopen', lambda *_args, **_kwargs: Response())
    seen = []
    monkeypatch.setattr(cli.os, 'execv', lambda path, argv: seen.append((path, argv, cli.os.environ['AGENT_BROWSER_CDP'])))
    cli.main(['--session', 'task', '--json', 'snapshot'])
    assert seen == [(cli.UPSTREAM, [cli.UPSTREAM, '--session', 'task', '--json', 'snapshot'], signed)]


def test_version_check_does_not_require_an_active_lease(cli, monkeypatch):
    monkeypatch.setenv('AGENT_BROWSER_CDP', cli.DISCOVERY)
    def refused(*args, **kwargs):
        raise AssertionError('cleanup must not fetch discovery')
    monkeypatch.setattr(cli.urllib.request, 'urlopen', refused)
    seen = []
    monkeypatch.setattr(cli.os, 'execv', lambda path, argv: seen.append(argv))
    cli.main(['--version'])
    assert seen


def test_failed_discovery_does_not_launch_and_redacts_error(cli, monkeypatch, capsys):
    monkeypatch.setenv('AGENT_BROWSER_CDP', cli.DISCOVERY)
    def failed(*args, **kwargs):
        raise ValueError('signed-secret-must-not-leak')
    monkeypatch.setattr(cli.urllib.request, 'urlopen', failed)
    seen = []
    monkeypatch.setattr(cli.os, 'execv', lambda *args: seen.append(args))
    assert cli.main(['--json', 'snapshot']) == 1
    assert not seen
    assert 'signed-secret' not in capsys.readouterr().out


@pytest.mark.parametrize('endpoint', ['ws://bedrock-agentcore.us-east-1.amazonaws.com/browser-streams/aws.browser.v1/sessions/abc/automation', 'wss://evil.example/automation', 'wss://user@bedrock-agentcore.us-east-1.amazonaws.com/browser-streams/aws.browser.v1/sessions/abc/automation'])
def test_untrusted_discovery_endpoint_is_refused(cli, monkeypatch, endpoint):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, _limit): return json.dumps({'webSocketDebuggerUrl': endpoint}).encode()
    monkeypatch.setenv('AGENT_BROWSER_CDP', cli.DISCOVERY)
    monkeypatch.setattr(cli.urllib.request, 'urlopen', lambda *_args, **_kwargs: Response())
    seen = []
    monkeypatch.setattr(cli.os, 'execv', lambda *args: seen.append(args))
    assert cli.main(['--json', 'snapshot']) == 1
    assert not seen
