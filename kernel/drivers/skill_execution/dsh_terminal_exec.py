"""Deliver a DSH session patch through an anonymous descriptor."""
import json
import os
import sys
import tempfile

env = dict(os.environ)
patches = json.loads(env.pop('KAUS_DSH_TERMINAL_PATCH'))
command = sys.argv[1:]
with tempfile.TemporaryFile(mode='w+b') as stream:
    stream.write(json.dumps(patches).encode()); stream.flush(); stream.seek(0)
    os.set_inheritable(stream.fileno(), True)
    index = command.index('--resume')
    command[index:index] = ['--patch', f'/dev/fd/{stream.fileno()}']
    os.execvpe(command[0], command, env)
