"""Guard against source files that exist locally but never reach the repo.

The CCXT root .gitignore ignores names like `config.py`; a module with such a
name passes every local test and then fails with ModuleNotFoundError on a
fresh clone. This test fails if any file under src/ or tests/ is ignored by git.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _in_git_checkout() -> bool:
    if shutil.which('git') is None:
        return False
    probe = subprocess.run(['git', 'rev-parse', '--is-inside-work-tree'],
                           cwd=ROOT, capture_output=True, text=True)
    return probe.returncode == 0 and probe.stdout.strip() == 'true'


@pytest.mark.skipif(not _in_git_checkout(), reason='not a git checkout (e.g. ZIP download)')
def test_no_source_file_is_gitignored():
    sources = [str(p.relative_to(ROOT)) for d in ('src', 'tests') for p in (ROOT / d).rglob('*')
               if p.is_file() and '__pycache__' not in p.parts and '.egg-info' not in str(p)]
    result = subprocess.run(['git', 'check-ignore', '--no-index', *sources],
                            cwd=ROOT, capture_output=True, text=True)
    ignored = result.stdout.split()
    assert not ignored, f'these source files are gitignored and would be missing from a clone: {ignored}'
