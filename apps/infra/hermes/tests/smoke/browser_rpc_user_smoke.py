"""Run the RPC helper socket contract as AgentCore's unprivileged user."""
import os
import subprocess
import sys

subprocess.run(
    [sys.executable, '-c', '''
import os
from pathlib import Path
from bridge.browser_state import _operation_env
assert os.geteuid() == 1000
assert os.environ['HOME'] == '/'
with _operation_env('wss://unused.invalid') as env:
    directory = Path(env['AGENT_BROWSER_SOCKET_DIR'])
    assert directory.stat().st_mode & 0o777 == 0o700
    (directory / 'write-probe').write_text('ok')
assert not directory.exists()
print('Browser RPC sockets are writable as UID 1000 with HOME=/ and removed afterward')
'''],
    user=1000,
    env={**os.environ, 'HOME': '/', 'PYTHONPATH': '/opt/bridge'},
    check=True,
)
