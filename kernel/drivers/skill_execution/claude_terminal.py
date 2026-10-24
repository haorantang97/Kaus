"""Pass per-session MCP to the unmodified Claude CLI through an anonymous file.

The environment arrives through Kaus' same-user, single-use relay. Credentials
are removed before exec, never appear in argv, and no named config is retained.
The CLI keeps its native history and still loads its ordinary global config.
"""
import json
import os
import sys
import tempfile

MCP_ENV = 'KAUS_CLAUDE_TERMINAL_MCP'


def main(argv=None):
    command = list(sys.argv[1:] if argv is None else argv)
    if not command:
        raise ValueError('Missing native terminal command')
    env = dict(os.environ)
    payload = json.loads(env.pop(MCP_ENV))
    if not isinstance(payload, dict) or not isinstance(payload.get('mcpServers'), dict):
        raise ValueError('Invalid session MCP configuration')
    # TemporaryFile is unlinked immediately. /dev/fd remains readable across
    # exec and disappears automatically when the native process releases it.
    with tempfile.TemporaryFile(mode='w+b') as stream:
        stream.write(json.dumps(payload).encode())
        stream.flush()
        stream.seek(0)
        os.set_inheritable(stream.fileno(), True)
        os.execvpe(command[0], [*command, '--mcp-config', f'/dev/fd/{stream.fileno()}'], env)


if __name__ == '__main__':
    main()
