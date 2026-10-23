"""Start installed Hermes with a required process-local skill policy."""
import os
import sys
from pathlib import Path


def main():
    home = os.environ.get("HERMES_HOME")
    if not home:
        raise RuntimeError("Kaus managed Hermes launch requires HERMES_HOME")
    flags = {'--kaus-skill-scope': 'KAUS_HERMES_SKILL_SCOPE', '--kaus-native-source': 'KAUS_HERMES_NATIVE_SKILL_SOURCE',
             '--kaus-native-output': 'KAUS_HERMES_NATIVE_SKILL_OUTPUT'}
    while len(sys.argv) > 2 and sys.argv[1] in flags:
        os.environ[flags[sys.argv[1]]] = sys.argv[2]
        del sys.argv[1:3]
    # The interpreter is the installed Hermes environment. The adapter lives
    # alongside this file, without importing the Kaus application's dependencies.
    if os.environ.get('KAUS_HERMES_NATIVE_SKILL_SOURCE'):
        from skill_native_guard import install
        install(Path(home), Path(os.environ['KAUS_HERMES_NATIVE_SKILL_SOURCE']), Path(os.environ['KAUS_HERMES_NATIVE_SKILL_OUTPUT']))
    else:
        from skill_runtime_guard import install
        install(Path(home))
    from hermes_cli.main import main as run_hermes
    sys.argv[0] = "hermes"
    run_hermes()


if __name__ == "__main__":
    main()
