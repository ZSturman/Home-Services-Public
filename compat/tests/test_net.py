import workspace_util.net as net_mod
from workspace_util.net import resolve_bind_host


def test_resolve_bind_host_uses_explicit_value_unchanged():
    assert resolve_bind_host("127.0.0.1") == "127.0.0.1"
    assert resolve_bind_host("100.64.0.5") == "100.64.0.5"


def test_resolve_bind_host_falls_back_to_loopback_when_tailscale_unavailable(monkeypatch):
    monkeypatch.setattr(net_mod, "tailscale_ipv4", lambda: None)
    assert resolve_bind_host("") == "127.0.0.1"


def test_resolve_bind_host_uses_tailscale_ip_when_available(monkeypatch):
    monkeypatch.setattr(net_mod, "tailscale_ipv4", lambda: "100.90.61.70")
    assert resolve_bind_host("") == "100.90.61.70"


def test_tailscale_binary_falls_back_to_known_paths_when_not_on_path(monkeypatch, tmp_path):
    """Regression: a real LaunchAgent deployment had `tailscale` installed at
    /usr/local/bin but launchd's minimal PATH didn't include it, so
    subprocess.run(["tailscale", ...]) raised FileNotFoundError and the
    service silently bound to loopback instead of its Tailscale IP."""
    fake_tailscale = tmp_path / "tailscale"
    fake_tailscale.write_text("#!/bin/sh\necho fake\n")
    fake_tailscale.chmod(0o755)

    monkeypatch.setattr(net_mod.shutil, "which", lambda name: None if name == "tailscale" else (
        str(fake_tailscale) if name == str(fake_tailscale) else None
    ))
    monkeypatch.setattr(net_mod, "_TAILSCALE_FALLBACK_PATHS", [str(fake_tailscale)])

    assert net_mod._tailscale_binary() == str(fake_tailscale)


def test_tailscale_binary_falls_back_to_bare_name_when_nothing_found(monkeypatch):
    monkeypatch.setattr(net_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(net_mod, "_TAILSCALE_FALLBACK_PATHS", ["/nonexistent/tailscale"])
    assert net_mod._tailscale_binary() == "tailscale"
