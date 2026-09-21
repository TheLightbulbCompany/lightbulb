#!/usr/bin/env python3
"""Resolve our loopback metadata without agent-browser's remote-host rewrite."""
import json
import os
import re
import sys
import urllib.request
from urllib.parse import urlparse

DISCOVERY = 'http://127.0.0.1:8080'
UPSTREAM = '/usr/local/bin/agent-browser'


def main(args=None):
    args = sys.argv[1:] if args is None else args
    metadata = any(flag in args for flag in ('--version', '-V', '--help', '-h'))
    try:
        if os.environ.get('AGENT_BROWSER_CDP') == DISCOVERY and not metadata:
            with urllib.request.urlopen(DISCOVERY + '/json/version', timeout=5) as response:
                body = response.read(32769)
            if len(body) > 32768:
                raise ValueError('oversized discovery')
            endpoint = json.loads(body)['webSocketDebuggerUrl']
            parsed = urlparse(endpoint)
            if (parsed.scheme != 'wss' or parsed.port not in (None, 443) or parsed.username or parsed.password
                    or parsed.fragment or not re.fullmatch(r'bedrock-agentcore\.[a-z0-9-]+\.amazonaws\.com', parsed.hostname or '')
                    or not re.fullmatch(r'/browser-streams/aws\.browser\.v1/sessions/[A-Za-z0-9]+/automation', parsed.path)):
                raise ValueError('invalid automation endpoint')
            # Only this child receives the signed capability; the bridge process and other
            # tool children keep the nonsecret loopback address. Preserve signature bytes.
            os.environ['AGENT_BROWSER_CDP'] = endpoint
        os.execv(UPSTREAM, [UPSTREAM, *args])
    except Exception:
        print(json.dumps({'success': False, 'error': 'Browser connection unavailable. Obtain a fresh browser snapshot and retry.'}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
