#!/usr/bin/env python3
"""Vendor versioned pairing clients into an app; never create sibling dependencies."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent

def vendor(source, destination, check=False):
    files = [p for p in source.rglob('*') if p.is_file() and not any(x in p.parts for x in ('.build', '.swiftpm', '__pycache__'))]
    # Independent adapters vendor into an existing service directory. Keep that
    # service's README and other vendor manifests untouched.
    backend = source.name == "PiConnectionBackend"
    if backend:
        files = [p for p in files if p.suffix == ".py"]
    metadata = "PAIRING_VENDORED.json" if backend else "VENDORED.json"
    hashes = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    if check:
        missing = [name for name, digest in hashes.items() if not (destination/name).is_file() or hashlib.sha256((destination/name).read_bytes()).hexdigest() != digest]
        if missing:
            raise SystemExit('Outdated vendored pairing files: ' + ', '.join(missing))
    else:
        for path in files:
            output = destination/path.relative_to(source); output.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(path, output)
        (destination/metadata).write_text(json.dumps({'version': '1.0.0', 'files': hashes}, indent=2) + '\n')
    print(f'{"Verified" if check else "Vendored"} {len(files)} files: {destination}')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--backend', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    vendor(ROOT/('PiConnectionBackend' if args.backend else 'HomeServicesConnection'), args.destination, args.check)
