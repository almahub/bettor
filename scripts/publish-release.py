"""Publish a verified Windows setup, then retain at most three GitHub releases."""
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPOSITORY = 'almahub/bettor'


def obsolete_releases(releases, current, keep=3):
    selected = sorted(releases, key=lambda r: (r.get('published_at') or r['created_at']), reverse=True)
    retained = [current, *[r['tag_name'] for r in selected if r['tag_name'] != current]][:keep]
    return [r['tag_name'] for r in selected if r['tag_name'] not in retained]


def gh(*args):
    result = subprocess.run(['gh', *args, '--repo', REPOSITORY], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError('Comando GitHub non riuscito: pubblicazione interrotta.')
    return result.stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag', required=True)
    args = parser.parse_args()
    from better import __version__
    if args.tag != 'v' + __version__:
        raise RuntimeError('Tag e versione del software non coincidono.')
    setup = Path('dist/installer') / f'Better-Setup-{__version__}-x64.exe'
    checksum = setup.with_suffix('.exe.sha256')
    if checksum.read_text().split()[0].lower() != hashlib.sha256(setup.read_bytes()).hexdigest():
        raise RuntimeError('Checksum del setup non valido.')
    branded = setup.with_name(f'Bettor-Setup-{__version__}-x64.exe')
    branded_checksum = branded.with_suffix('.exe.sha256')
    if branded.read_bytes() != setup.read_bytes() or branded_checksum.read_text().split()[0].lower() != hashlib.sha256(branded.read_bytes()).hexdigest():
        raise RuntimeError('Setup Bettor o checksum non coerenti.')
    notes = Path('docs/releases') / f'{__version__}.md'
    # Keep the legacy asset alias for clients already installed before the rename.
    files = [setup, checksum, branded, branded_checksum]
    gh('release', 'create', args.tag, *map(str, files), '--draft', '--verify-tag', '--notes-file', str(notes), '--title', 'Bettor ' + args.tag)
    assets = json.loads(gh('release', 'view', args.tag, '--json', 'assets'))['assets']
    if {file.name for file in files} - {asset['name'] for asset in assets}:
        raise RuntimeError('Asset mancanti: la release resta in bozza.')
    with tempfile.TemporaryDirectory(prefix='bettor-release-check-') as directory:
        gh('release', 'download', args.tag, '--dir', directory)
        for file in files:
            downloaded = Path(directory) / file.name
            if hashlib.sha256(downloaded.read_bytes()).digest() != hashlib.sha256(file.read_bytes()).digest():
                raise RuntimeError('Asset remoto non verificato: la release resta in bozza.')
    gh('release', 'edit', args.tag, '--draft=false', '--latest')
    published = json.loads(gh('release', 'view', args.tag, '--json', 'isDraft,isPrerelease,tagName'))
    if published['isDraft'] or published['isPrerelease'] or published['tagName'] != args.tag:
        raise RuntimeError('Pubblicazione non verificata: le vecchie release restano conservate.')
    # Retention runs only after successful publication. Tags/source history are preserved.
    result = subprocess.run(['gh', 'api', '--paginate', '--slurp', f'repos/{REPOSITORY}/releases?per_page=100'], capture_output=True, text=True, check=True)
    releases = [r for page in json.loads(result.stdout) for r in page]
    for tag in obsolete_releases(releases, args.tag):
        gh('release', 'delete', tag, '--yes')
    print('Pubblicata ' + args.tag + '; conservate le ultime tre release, senza cancellare tag o sorgenti.')


if __name__ == '__main__':
    main()
