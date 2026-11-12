"""Pair a local Hue Bridge and read its light inventory without changing lights."""
from __future__ import annotations

import argparse
import ipaddress
import json
import ssl
import urllib.request
from pathlib import Path

from .energy import private_write


def request(ip: str, path: str, context: ssl.SSLContext, payload: dict | None = None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"https://{ip}{path}", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=context, timeout=8) as response:
        return json.load(response)


def pair(ip: str, expected_id: str, directory: Path) -> dict:
    if not ipaddress.ip_address(ip).is_private:
        raise ValueError("Expected a private LAN bridge address")
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / "hue-bridge.pem"
    config_path = directory / "hue.json"
    if config_path.exists() and not cert_path.exists():
        raise ValueError("The paired bridge's pinned certificate is missing")
    # Trust on first use on the owner's LAN; retain the exact certificate.
    # Subsequent connections must validate against this stored certificate.
    cert = cert_path.read_text() if cert_path.exists() else ssl.get_server_certificate((ip, 443), timeout=8)
    context = ssl.create_default_context(cadata=cert)
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    # Hue's certificate names the bridge, not its DHCP address.
    context.check_hostname = False
    info = request(ip, "/api/config", context)
    if info.get("bridgeid", "").lower() != expected_id.lower():
        raise ValueError("Bridge identity did not match discovery")
    if config_path.exists():
        config = json.loads(config_path.read_text())
        if config["bridge_id"].lower() != expected_id.lower():
            raise ValueError("A different bridge is already paired")
    else:
        response = request(ip, "/api", context, {"devicetype": "home_services_energy#mac"})
        if not isinstance(response, list) or len(response) != 1:
            raise ValueError("Unexpected bridge pairing response")
        result = response[0]
        if result.get("error", {}).get("type") == 101:
            return {"paired": False, "action": "Press the bridge's large link button once, then retry"}
        username = result.get("success", {}).get("username")
        if not username:
            raise ValueError("Bridge did not authorize pairing")
        config = {"ip": ip, "bridge_id": info["bridgeid"], "username": username,
                  "certificate_file": str(cert_path.resolve())}
        private_write(cert_path, cert)
        private_write(config_path, json.dumps(config, indent=2) + "\n")
    lights = request(ip, f'/api/{config["username"]}/lights', context)
    if not isinstance(lights, dict):
        raise ValueError("Bridge inventory unavailable; existing pairing was preserved")
    private_write(directory / "hue-inventory.json", json.dumps(lights, indent=2) + "\n")
    return {"paired": True, "bridge": info.get("name"), "light_count": len(lights),
            "lights": [{"id": key, "name": item.get("name"), "model": item.get("modelid")}
                       for key, item in lights.items()]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-ip", required=True)
    parser.add_argument("--bridge-id", required=True)
    parser.add_argument("--directory", type=Path, default=Path(".dev/energy"))
    args = parser.parse_args()
    try:
        result = pair(args.bridge_ip, args.bridge_id, args.directory)
    except Exception as exc:
        # Exceptions can contain authenticated URLs. Never print their text.
        print(json.dumps({"paired": False, "error": type(exc).__name__,
                          "action": "Check the bridge/LAN and retry; any existing pairing is preserved"}))
        return 1
    print(json.dumps(result))
    return 0 if result["paired"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
