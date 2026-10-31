#!/usr/bin/env python3
"""Fetch exact standalone sources and apply the existing human-authored SideSign PR."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent


def run(*args):
    subprocess.run(args, check=True)


def prepare(directory):
    pins = json.loads((HERE / 'sources.json').read_text())
    patch = HERE / 'sidesign-pr4.patch'
    if hashlib.sha256(patch.read_bytes()).hexdigest() != pins['sidesign_patch_sha256']:
        raise ValueError('SideSign PR patch hash mismatch')
    test_patch = HERE / 'sidesign-test-foundation.patch'
    if hashlib.sha256(test_patch.read_bytes()).hexdigest() != pins['sidesign_test_patch_sha256']:
        raise ValueError('SideSign test compatibility patch hash mismatch')
    source = directory / 'SideStore'
    source.mkdir(parents=True, exist_ok=False)
    run('git', 'init', str(source))
    run('git', '-C', str(source), 'remote', 'add', 'origin', pins['sidestore_repository'])
    run('git', '-C', str(source), 'fetch', '--depth', '1', 'origin', pins['sidestore_revision'])
    run('git', '-C', str(source), '-c', 'advice.detachedHead=false', 'checkout', '--detach', 'FETCH_HEAD')
    observed = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if observed != pins['sidestore_revision']:
        raise ValueError('Standalone SideStore revision mismatch')
    run('git', '-C', str(source), 'submodule', 'update', '--init', '--recursive')
    dependency = source / 'Dependencies/SideSign'
    observed = subprocess.check_output(['git', '-C', str(dependency), 'rev-parse', 'HEAD'], text=True).strip()
    if observed != pins['sidesign_base_revision']:
        raise ValueError('SideSign submodule does not match the reviewed patch base')
    run('git', '-C', str(dependency), 'apply', '--check', str(patch))
    run('git', '-C', str(dependency), 'apply', str(patch))
    run('git', '-C', str(dependency), 'apply', '--reverse', '--check', str(patch))
    run('git', '-C', str(dependency), 'apply', '--check', str(test_patch))
    run('git', '-C', str(dependency), 'apply', str(test_patch))
    # SideStore application sources stay unchanged. The production PR is verbatim;
    # the separate compatibility patch only adds Foundation to an existing test.
    tool = directory / 'dylibify'
    with urlopen(pins['dylibify_url'], timeout=90) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != pins['dylibify_sha256']:
        raise ValueError('Pinned dylibify release hash mismatch')
    tool.write_bytes(data)
    tool.chmod(0o755)
    (directory / 'SOURCE-MANIFEST.json').write_text(json.dumps(pins, indent=2) + '\n')
    print('Prepared official standalone SideStore at ' + pins['sidestore_revision'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    prepare(args.directory)
