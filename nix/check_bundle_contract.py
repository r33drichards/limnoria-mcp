"""CI-only: exercise the production Nix flags on this local, locked flake.

This does not resolve remote main, publish a candidate, or activate anything.
Run from the repo root with PYTHONPATH=src and the reviewed, committed lockfile.
"""
import json
import re
import tempfile
from pathlib import Path

from limnoria_mcp.bundle_update import (
    _run, clean_environment, nix_build_argv, read_build_output,
)


def main():
    root = Path.cwd().resolve()
    if not (root / 'flake.lock').is_file():
        raise SystemExit('the reviewed flake.lock must be committed first')
    # Fixed CI architecture/output; path: uses this reviewed checkout and its
    # committed lockfile. No repository apps/host outputs run.
    installable = f'path:{root}#packages.x86_64-linux.scriptBundle'
    with tempfile.TemporaryDirectory(prefix='bundle-contract-') as home:
        result = json.loads(_run(nix_build_argv(installable), clean_environment(home)))
    if (not isinstance(result, list) or len(result) != 1
            or not isinstance(result[0], dict)
            or set(result[0].get('outputs', {})) != {'out'}):
        raise AssertionError('Nix did not produce exactly one out output')
    output = result[0]['outputs']['out']
    if not isinstance(output, str) or not re.fullmatch(r'/nix/store/[a-z0-9]{32}-[^/]+', output):
        raise AssertionError('Nix output is not an immutable store path')
    # Nix auto-optimisation may hardlink regular immutable store files. Exercise
    # that real layout too, rather than relying only on synthetic unit fixtures.
    if any(path.stat().st_nlink < 2 for path in Path(output).iterdir()):
        with tempfile.TemporaryDirectory(prefix='bundle-optimise-contract-') as home:
            _run(['nix', '--extra-experimental-features', 'nix-command',
                  'store', 'optimise'], clean_environment(home))
    assert all(path.stat().st_nlink >= 2 for path in Path(output).iterdir())
    scripts = read_build_output(output)
    expected = {path.stem: path.read_text() for path in (root / 'scripts').glob('*.tcl')}
    assert scripts == expected and expected, 'Bundle differs from repository Tcl source'
    print(f'Actual Nix CLI contract passed: {len(scripts)} Tcl script(s), fixed scriptBundle output')


if __name__ == '__main__':
    main()
