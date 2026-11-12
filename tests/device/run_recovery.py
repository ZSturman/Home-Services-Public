"""Opt-in physical iOS acceptance; requires Xcode, XcodeGen and a client checkout.

The checkout is only a test input: committed sources are exported without its
working edits. Home Services has no build/runtime dependency on that checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", type=Path, required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--relative-file", required=True, help="Small nonsecret file relative to Workspace in the snapshot")
    parser.add_argument("--snapshot-tag", default="job-mac-workspace")
    parser.add_argument("--skip-build", action="store_true", help="Reuse the already prepared matching test build")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    os.chdir(root)
    client = args.client.resolve()
    prepared = root / "build/MobileRecoveryClient"
    revision = subprocess.check_output(["git", "-C", str(client), "rev-parse", "HEAD"], text=True).strip()
    if not prepared.exists():
        prepared.mkdir(parents=True)
        exported = subprocess.check_output(["git", "-C", str(client), "archive", "HEAD"])
        with tarfile.open(fileobj=io.BytesIO(exported)) as archive:
            archive.extractall(prepared, filter="data")
        (prepared / ".source-commit").write_text(revision)
    elif (prepared / ".source-commit").read_text().strip() != revision:
        raise SystemExit("Client HEAD changed; remove only build/MobileRecoveryClient and prepare it again.")
    if not args.skip_build:
        shutil.copy(root / "tests/device/MobileRecoveryTests.swift", prepared / "App/UITests/MobileRecoveryTests.swift")
        device_test = prepared / "App/UITests/DeviceHomeServicesTests.swift"
        # Keep acceptance bounded; no screenshot series is needed for passing checks.
        text = device_test.read_text()
        text = re.sub(r"private func capture\(_ app: XCUIApplication, _ name: String\) \{.*?\n    \}",
                      "private func capture(_ app: XCUIApplication, _ name: String) {}", text, count=1, flags=re.S)
        device_test.write_text(text)
        # The physical iOS check does not need the separate Mac UI test target.
        manifest = prepared / "project.yml"
        text = manifest.read_text().replace("        FilesBackupsMacUITests: [test]\n", "").replace("        - FilesBackupsMacUITests\n", "")
        manifest.write_text(text)
        subprocess.run(["xcodegen", "generate", "--spec", str(manifest), "--project", str(prepared)], check=True, stdout=subprocess.DEVNULL)
    token = subprocess.check_output(["security", "find-generic-password", "-a", "home-services", "-s",
                                     "home-services-token-files-backups", "-w"], text=True).strip()
    headers = {"Authorization": f"Bearer {token}", "Home-Services-Contract": "1"}
    log = root / "build/MobileRecovery.log"
    bundle = root / "build/MobileRecovery.xcresult"
    fixture = f"_acceptance/mobile-{time.strftime('%Y%m%dT%H%M%S')}-{os.urandom(3).hex()}"
    with httpx.Client(base_url=args.url.rstrip("/"), headers=headers, timeout=120) as api:
        r = api.get("/api/v1/backups/snapshots")
        r.raise_for_status()
        snapshot = next(s for s in r.json()["snapshots"] if args.snapshot_tag in s["tags"])
        path = snapshot["paths"][0].rstrip("/") + "/" + args.relative_file
        archived = api.get(f"/api/v1/backups/snapshots/{snapshot['id']}/content", params={"path": path})
        archived.raise_for_status()
        expected = hashlib.sha256(archived.content).hexdigest()
        for directory in ("_acceptance", fixture):
            r = api.post("/api/v1/storage/files/directories", json={"path": directory})
            if r.status_code not in (201, 409):
                r.raise_for_status()
        environment = {**os.environ, "TEST_RUNNER_FB_HS_TEST_URL": args.url, "TEST_RUNNER_FB_HS_TEST_TOKEN": token,
                       "TEST_RUNNER_FB_HS_TEST_ROOT": fixture, "TEST_RUNNER_FB_UITEST_FIXTURE_MB": "2",
                       "TEST_RUNNER_FB_RECOVERY_SNAPSHOT": snapshot["id"],
                       "TEST_RUNNER_FB_RECOVERY_RELATIVE_PATH": args.relative_file}
        command = ["xcodebuild", "test-without-building" if args.skip_build else "test", "-project",
                   str(prepared / "FilesBackups.xcodeproj"), "-scheme", "FilesBackups", "-destination", f"id={args.device}",
                   "-only-testing:FilesBackupsUITests/DeviceHomeServicesTests", "-only-testing:FilesBackupsUITests/MobileRecoveryTests",
                   "-allowProvisioningUpdates", "-derivedDataPath", str(root / "build/DerivedData-ios"),
                   "-clonedSourcePackagesDirPath", str(client / ".build/DerivedData-device/SourcePackages"),
                   "-parallel-testing-enabled", "NO", "-resultBundlePath", str(bundle)]
        if bundle.exists():
            shutil.rmtree(bundle)
        print(f"Testing {args.device}; current Mac snapshot {snapshot['short_id']}; fixture {fixture}", flush=True)
        try:
            with log.open("w") as output:
                result = subprocess.run(command, env=environment, stdout=output, stderr=subprocess.STDOUT)
            text = log.read_text().replace(token, "<redacted>")
            log.write_text(text)
            for line in text.splitlines():
                if re.search(r"Test Case|error:|\*\* TEST|MOBILE_RECOVERY_RESULT:", line):
                    print(line)
            if result.returncode:
                raise SystemExit(result.returncode)
            files = {
                "Day 1/renamed.txt": b"Files & Backups acceptance fixture\n",
                "Day 1/photo.bin": bytes((i * 7) & 255 for i in range(750_000)),
                "Day 1/Café ☕️/menu.txt": "crème brûlée\n".encode(),
                "Day 2/empty.dat": b"",
                "Day 2/large.bin": bytes(((i * 31) ^ (i >> 11)) & 255 for i in range(2 << 20)),
            }
            for relative, data in files.items():
                received = api.get("/api/v1/storage/files/content", params={"path": f"{fixture}/FB Acceptance/{relative}"})
                received.raise_for_status()
                if hashlib.sha256(received.content).digest() != hashlib.sha256(data).digest():
                    raise SystemExit(f"Phone-upload hash mismatch: {relative}")
            print("All 5 phone-uploaded files match their SHA-256, including the moved file and Trash-restored folder.")
            match = re.search(r"MOBILE_RECOVERY_RESULT: .*?into (Restores/[^\s]+)", text)
            if not match:
                raise SystemExit("UI test did not report its restore destination; inspect result bundle.")
            destination = match[1]
            downloaded = api.get("/api/v1/storage/files/content", params={"path": destination + path})
            downloaded.raise_for_status()
            actual = hashlib.sha256(downloaded.content).hexdigest()
            if actual != expected:
                raise SystemExit("Restored-file SHA-256 differs from archived file")
            print(f"Phone restore {destination}: SHA-256 matches archived file ({len(downloaded.content)} bytes).")
            # Keep the tiny restored copy so the owner can inspect it in the real app.
            print(f"Retained restored sample at {destination}; actual saved phone connection remains configured.")
        finally:
            cleanup = api.post("/api/v1/storage/files/trash", json={"path": fixture})
            print(f"Disposable upload fixture moved to managed Trash: {cleanup.status_code == 201}.")


if __name__ == "__main__":
    main()
