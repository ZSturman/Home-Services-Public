"""Compile the real Swift core and check server-issued invitations, without UI,
Keychain access, a simulator, XCTest overlays or SwiftPM macro processes.

Run with .venv/bin/python tests/check_swift_pairing.py on a Mac with Xcode.
All generated binaries, module caches and service fixtures stay local/temporary.
"""
import base64
import json
import subprocess
import tempfile
from pathlib import Path

from home_services.config import load_config
from home_services.connections import Connections
from home_services.contract import _fixture_config
from home_services.devfixtures import create_dev_fixtures
from home_services.services import build_services


def encode(payload):
    return "homeservices://pair#" + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def main():
    repo = Path(__file__).resolve().parents[1]
    build = repo / "build/ConnectionInterop"
    build.mkdir(parents=True, exist_ok=True)
    binary = build / "pairing-probe"
    subprocess.run(["swiftc", "-swift-version", "5", "-module-cache-path", str(build / "ModuleCache"),
                    str(repo / "clients/HomeServicesConnection/Sources/HomeServicesConnection/Pairing.swift"),
                    str(repo / "tests/swift/PairingProbe.swift"), "-o", str(binary)], check=True)
    with tempfile.TemporaryDirectory(prefix="hs-swift-pairing-") as folder:
        root = Path(folder)
        create_dev_fixtures(root)
        s = build_services(load_config(_fixture_config(root)))
        try:
            invite = Connections(s).invite(preset="home-page", name="Swift fixture", url="https://host.example.net:8443")
            payload = invite["invitation"]
            cases = [(invite["pairing_uri"], True), (" \n" + invite["pairing_uri"] + "\n", True)]
            cases.append((encode(payload | {"expires_at": "2099-01-01T00:00:00.123456Z"}), True))
            for change in [{"preset": "files-backups"}, {"protocol": 2}, {"provider": "other"},
                           {"expires_at": "2000-01-01T00:00:00Z"}, {"secret": "short"}]:
                cases.append((encode(payload | change), False))
            for url in ["http://home.example", "https://user:pass@home.example", "https://home.example/path",
                        "https://home.example?q=secret", "https://home.example#secret"]:
                cases.append((encode(payload | {"base_url": url}), False))
            cases += [(code, False) for code in ["123456", "https://pair#abcd", "homeservices://pair?token=abc#abcd", "a" * 9000]]
            for i, (code, expected) in enumerate(cases):
                result = subprocess.run([str(binary)], input=code, text=True, capture_output=True)
                assert (result.returncode == 0) == expected, f"Swift invitation case {i} failed"
                if expected:
                    assert result.stdout.strip() == s.server_id
            print(f"{len(cases)} Swift/server invitation interoperability checks passed")
        finally:
            s.db.close()


if __name__ == "__main__":
    main()
