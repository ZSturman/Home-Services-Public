# Home Services

Python/FastAPI services for Linux and Raspberry Pi: guarded storage, transfers, durable jobs, backup recovery and cameras.

**Status:** Working backend with optional integrations. Live-host and physical storage/camera acceptance require separate opt-in checks. Never run deployment scripts against an existing host without reviewing configuration.

[Project portfolio](https://zacharysturman.com/projects/home-services)

## Quick start

```sh
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/home-services --help
```

## Usage

```sh
.venv/bin/home-services --config config/development.toml dev-fixtures
.venv/bin/home-services --config config/development.toml serve
```

## How it works

The FastAPI gateway handles authenticated requests; separately supervised workers own jobs. SQLite stores service state. Volume identity checks guard storage operations. Camera and backup clients remain independent.

## Testing

```sh
.venv/bin/python -m pytest tests/test_config_network_auth.py tests/test_volumes.py tests/test_connections.py -q
```

See [validation and next steps](docs/NEXT_STEPS.md) for the checks performed on this snapshot. Automated fixture tests do not establish physical-device compatibility.

## Configuration and privacy

This is a curated source snapshot, published November 12, 2026. Configure your own endpoints, credentials and signing identities. Private development history, local environment files, personal recordings, runtime databases, signing materials and deployment automation are excluded. Deployment and device installation are explicit operator actions.

## Further reading

- [Api](docs/API.md)
- [Integrations](docs/INTEGRATIONS.md)
- [Install](docs/INSTALL.md)
- [Pairing](docs/PAIRING.md)
- [Camera Gate](docs/CAMERA_GATE.md)

## License and attribution

Existing licenses and third-party notices apply to the files they cover. Publication of this snapshot does not grant a new license to original material; no additional license is granted where none is supplied.
