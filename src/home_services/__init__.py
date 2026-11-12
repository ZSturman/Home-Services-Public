"""Home Services: optional private backend for storage, backups, and cameras."""

__version__ = "0.4.10"

# Contract versions this server implements. Clients send their major version
# in the `Home-Services-Contract` header; anything else is rejected with an
# actionable error instead of being guessed at.
CONTRACT_VERSION = "1.6"
SUPPORTED_CONTRACT_MAJORS = (1,)
