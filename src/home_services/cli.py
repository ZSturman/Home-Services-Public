"""`home-services` admin CLI."""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import threading
from pathlib import Path

from .config import WRITABLE_ROLES, ConfigError, load_config
from .errors import ServiceError
from .auth import PRESETS

EXIT_REBIND = 75  # systemd restarts the gateway, which re-resolves addresses


def _services(args):
    from .services import build_services
    return build_services(load_config(args.config))


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


# --- serve -----------------------------------------------------------------

def cmd_serve(args) -> int:
    import uvicorn

    from .api.app import create_app
    from .network import bind_sockets, resolve_bind_addresses, wait_for_bind_addresses

    config = load_config(args.config)
    net = config.network
    if config.environment == "production" and not (net.tls_cert_file and net.tls_key_file):
        addrs = resolve_bind_addresses(net.bind_addresses, ())
        from .network import classify
        if any(classify(a) != "loopback" for a in addrs) or net.bind_interfaces:
            print("refusing plain HTTP on a non-loopback address: configure tls_cert_file/tls_key_file, "
                  "or bind loopback behind Tailscale Serve HTTPS", file=sys.stderr)
            return 2
    addresses = wait_for_bind_addresses(net.bind_addresses, net.bind_interfaces, net.bind_wait_seconds)
    services = _services(args)
    app = create_app(services)
    socks = bind_sockets(addresses, net.port)
    logging.getLogger("home_services").info("listening on %s port %d", addresses, net.port)
    uv_config = uvicorn.Config(app, access_log=False, log_level="info", ssl_certfile=net.tls_cert_file or None,
                               ssl_keyfile=net.tls_key_file or None, proxy_headers=False, server_header=False)
    server = uvicorn.Server(uv_config)
    rebind = threading.Event()

    def watch():
        while not server.should_exit:
            if rebind.wait(net.rebind_check_seconds):
                return
            current = resolve_bind_addresses(net.bind_addresses, net.bind_interfaces)
            if current != addresses:
                logging.getLogger("home_services").warning("bind addresses changed %s -> %s; restarting to rebind", addresses, current)
                rebind.set()
                server.should_exit = True

    if net.bind_interfaces:
        threading.Thread(target=watch, daemon=True).start()
    server.run(sockets=socks)
    return EXIT_REBIND if rebind.is_set() else 0


def cmd_worker(args) -> int:
    from .worker import Stop, run_worker
    if args.queue == "camera":
        from .cameras.recorder import run_cameras
        run_cameras(_services(args), Stop())
        return 0
    run_worker(_services(args), args.queue)
    return 0


def cmd_notifier(args) -> int:
    from .worker import run_notifier
    run_notifier(_services(args))
    return 0


# --- tokens ----------------------------------------------------------------

def cmd_tokens(args) -> int:
    from .auth import PRESETS
    s = _services(args)
    if args.action == "create":
        scopes = list(PRESETS[args.preset]) if args.preset else []
        scopes += args.scope or []
        if not scopes:
            print("choose --preset or at least one --scope", file=sys.stderr)
            return 2
        token, info = s.tokens.create(args.name, sorted(set(scopes)), args.ttl_days)
        print(f"token (shown once; store it in the client's Keychain): {token}", file=sys.stderr)
        _print(info)
    elif args.action == "list":
        _print(s.tokens.list())
    elif args.action == "revoke":
        _print(s.tokens.revoke(args.token_id))
    return 0


# --- volumes ---------------------------------------------------------------

def cmd_connections(args) -> int:
    from .connections import Connections
    s = _services(args)
    try:
        connections = Connections(s)
        if args.action == "catalog":
            result = connections.catalog()
        elif args.action == "invite":
            result = connections.invite(preset=args.preset, name=args.name, url=args.base_url, scopes=args.scope)
        elif args.action == "list":
            result = connections.list()
        elif args.action == "cancel":
            result = connections.cancel(args.invitation_id)
        elif args.action == "adopt":
            result = connections.adopt(args.token_id, args.preset)
        elif args.action == "revoke":
            result = connections.revoke(args.connection_id)
        elif args.action == "update":
            preferences = None
            if args.preferences_json is not None:
                try:
                    preferences = json.loads(args.preferences_json)
                except ValueError:
                    raise ServiceError("preferences must be a JSON object") from None
                if not isinstance(preferences, dict):
                    raise ServiceError("preferences must be a JSON object")
            result = connections.update(args.connection_id, scopes=[] if args.disable_all else args.scope,
                                        preferences=preferences)
        _print(result)
        return 0
    finally:
        s.db.close()


def cmd_volumes(args) -> int:
    s = _services(args)
    if args.action == "status":
        _print([s.guard.status(v).as_dict() | {"mount": v.mount, "roots": v.roots} for v in s.config.volumes])
        return 0
    volume = s.config.volume(args.volume)
    s.guard.require(volume)  # raises unless mounted with the configured UUID
    planned = [(role, Path(volume.mount) / rel) for role, rel in volume.roots.items() if role in WRITABLE_ROLES]
    for role, path in planned:
        state = "exists" if path.is_dir() else "create"
        print(f"{role:12} {path} [{state}]")
    if not args.yes:
        print("dry run; re-run with --yes to create missing writable roots (repository and legacy roots are never touched)")
        return 0
    for _role, path in planned:
        path.mkdir(parents=True, exist_ok=True)
    return 0


# --- backups ---------------------------------------------------------------

def cmd_backups(args) -> int:
    s = _services(args)
    if args.action == "sources":
        _print(s.backups.sources())
    elif args.action == "set-scheduler":
        _print(s.backups.set_scheduler(args.source_id, args.scheduler))
    elif args.action == "register-source":
        src, _ = s.backups.register_source(host=args.host, path=args.path, tag=args.tag, scheduler=args.scheduler, principal_id="cli")
        _print(src)
    elif args.action == "retention-preview":
        return _retention_preview(s, args)
    return 0


def _retention_preview(s, args) -> int:
    from dataclasses import asdict, replace

    from .backups.retention import RetentionPolicy, preview
    policy = s.config.backup.retention
    overrides = {k: getattr(args, f"keep_{k}") for k in asdict(RetentionPolicy()) if getattr(args, f"keep_{k}") is not None}
    policy = replace(policy, **overrides)
    if policy.empty():
        print("no keep rule: set [backup.retention] keep_* in the config or pass --keep-*", file=sys.stderr)
        return 2
    result = preview(s.restic.snapshots(), policy)
    if args.json:
        _print(result)
        return 0
    print(f"policy: {result['restic_equivalent']} (preview only; nothing is forgotten)")
    for g in result["groups"]:
        print(f"{g['host']} {','.join(g['paths'])}: keep {len(g['keep'])}, remove {len(g['remove'])}")
        for r in g["remove"]:
            print(f"  remove {r['id'][:8]} {r['time']}")
    t = result["totals"]
    print(f"total: {t['snapshots']} snapshots, keep {t['keep']}, remove {t['remove']}")
    return 0


# --- doctor ----------------------------------------------------------------

def doctor_checks(config_path: str) -> list[dict]:
    from .network import BindRefused, resolve_bind_addresses
    from .volumes import VolumeGuard

    checks: list[dict] = []

    def add(name, ok, detail, level="error"):
        checks.append({"check": name, "status": "ok" if ok else level, "detail": detail})

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        add("config", False, str(exc))
        return checks
    add("config", True, f"{config.path} ({config.environment})")
    guard = VolumeGuard(config)
    ok, fstype = guard.data_dir_filesystem_ok()
    add("state_store_filesystem", ok, f"data_dir {config.data_dir} on {fstype}")
    try:
        addrs = resolve_bind_addresses(config.network.bind_addresses, config.network.bind_interfaces)
        add("bind_addresses", bool(addrs), f"{addrs or 'no private address yet'} port {config.network.port}", level="warning")
    except BindRefused as exc:
        add("bind_addresses", False, str(exc))
    for v in config.volumes:
        st = guard.status(v)
        add(f"volume:{v.id}", st.verified, f"{v.mount}: {st.state} ({st.reason})")
        if st.unmounted_mountpoint_writable:
            add(f"volume:{v.id}:mountpoint", False,
                f"{v.mount} is writable while unmounted; make it root-owned and mode 0555 so nothing can land on system storage")
        if st.verified:
            for role, rel in v.roots.items():
                p = Path(v.mount) / rel
                add(f"root:{role}", p.is_dir(), str(p), level="warning")
    if config.backup.enabled:
        add("restic_binary", shutil.which(config.backup.restic_binary) is not None, config.backup.restic_binary)
    from .services import build_services
    try:
        s = build_services(config)
    except Exception as exc:  # noqa: BLE001 - doctor reports, never crashes
        add("services", False, str(exc))
        return checks
    if config.backup.enabled:
        rs = s.restic.repository_status()
        add("repository", rs["state"] == "present" and rs["password_configured"], json.dumps(rs, sort_keys=True))
    tokens = [t for t in s.tokens.list() if not t["revoked_at"]]
    add("tokens", bool(tokens), f"{len(tokens)} active token(s)", level="warning")
    stale = [j["id"] for j in s.jobs.list(state="running") if (j["heartbeat"]["age_seconds"] or 0) > 300]
    add("jobs", not stale, f"running jobs without a recent heartbeat: {stale}" if stale else "no stale running jobs", level="warning")
    out = s.outbox.summary()
    add("notifications", out["failed"] == 0, json.dumps(out), level="warning")
    return checks


def cmd_doctor(args) -> int:
    checks = doctor_checks(args.config)
    if args.json:
        _print(checks)
    else:
        for c in checks:
            print(f"[{c['status']:7}] {c['check']}: {c['detail']}")
    return 1 if any(c["status"] == "error" for c in checks) else 0


# --- development fixtures & contract ------------------------------------------

def cmd_dev_fixtures(args) -> int:
    from .devfixtures import create_dev_fixtures
    _print(create_dev_fixtures(Path(args.root), with_restic=args.with_restic))
    return 0


def cmd_contract(args) -> int:
    from .contract import export_contract
    written = export_contract(Path(args.out))
    for p in written:
        print(p)
    return 0


def cmd_observe(args) -> int:
    from .observe import load_samples, record, summarize
    if args.action == "record":
        sample = record(args.config)
        print(json.dumps({"at": sample["at"], "doctor": sample["doctor"]}, sort_keys=True))
        return 0
    lines, problems = summarize(load_samples(load_config(args.config).data_dir, args.hours))
    print("\n".join(lines))
    print("\n" + ("needs attention:\n- " + "\n- ".join(problems) if problems else "no problems observed"))
    return 1 if problems else 0


def cmd_gate(args) -> int:
    from .gate.cli import run_gate_command
    return run_gate_command(args)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="home-services")
    ap.add_argument("--config", default=os.environ.get("HOME_SERVICES_CONFIG", "/etc/home-services/config.toml"))
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the API gateway").set_defaults(func=cmd_serve)
    w = sub.add_parser("worker", help="run a job worker")
    w.add_argument("queue", choices=["storage", "backup", "camera"])
    w.set_defaults(func=cmd_worker)
    sub.add_parser("notifier", help="run the ntfy outbox notifier").set_defaults(func=cmd_notifier)

    t = sub.add_parser("tokens", help="manage scoped API tokens")
    tsub = t.add_subparsers(dest="action", required=True)
    tc = tsub.add_parser("create")
    tc.add_argument("--name", required=True)
    tc.add_argument("--preset", choices=sorted(PRESETS))
    tc.add_argument("--scope", action="append")
    tc.add_argument("--ttl-days", type=float, help="lifetime in days (fractions allowed)")
    tsub.add_parser("list")
    tr = tsub.add_parser("revoke")
    tr.add_argument("token_id")
    t.set_defaults(func=cmd_tokens)

    cn = sub.add_parser("connections", help="QR/paste invitations and per-app access (local administration)")
    cns = cn.add_subparsers(dest="action", required=True)
    cns.add_parser("catalog")
    cns.add_parser("list")
    ci = cns.add_parser("invite")
    ci.add_argument("--preset", choices=sorted(p for p in PRESETS if p != "admin"), required=True)
    ci.add_argument("--name", required=True)
    ci.add_argument("--base-url", required=True)
    ci.add_argument("--scope", action="append", help="optional subset of preset permissions")
    cc = cns.add_parser("cancel")
    cc.add_argument("invitation_id")
    ca = cns.add_parser("adopt", help="manage an existing token without changing its credential")
    ca.add_argument("token_id")
    ca.add_argument("--preset", choices=sorted(p for p in PRESETS if p != "admin"), required=True)
    cr = cns.add_parser("revoke")
    cr.add_argument("connection_id")
    cu = cns.add_parser("update")
    cu.add_argument("connection_id")
    cg = cu.add_mutually_exclusive_group()
    cg.add_argument("--scope", action="append", help="complete enabled scope set, bounded by original approval")
    cg.add_argument("--disable-all", action="store_true")
    cu.add_argument("--preferences-json", help="upload_folder/camera_id client preferences; not resource ACLs")
    cn.set_defaults(func=cmd_connections)

    v = sub.add_parser("volumes", help="volume status and root preparation")
    vsub = v.add_subparsers(dest="action", required=True)
    vsub.add_parser("status")
    vp = vsub.add_parser("prepare")
    vp.add_argument("--volume", required=True)
    vp.add_argument("--yes", action="store_true")
    v.set_defaults(func=cmd_volumes)

    b = sub.add_parser("backups", help="backup source ownership")
    bsub = b.add_subparsers(dest="action", required=True)
    bsub.add_parser("sources")
    bs = bsub.add_parser("set-scheduler")
    bs.add_argument("source_id")
    bs.add_argument("scheduler")
    br = bsub.add_parser("register-source")
    br.add_argument("--host", required=True)
    br.add_argument("--path", required=True)
    br.add_argument("--tag", required=True)
    br.add_argument("--scheduler", required=True)
    rp = bsub.add_parser("retention-preview", help="what a keep policy would remove (read-only; runs no forget)")
    for k in ("last", "hourly", "daily", "weekly", "monthly", "yearly"):
        rp.add_argument(f"--keep-{k}", type=int, dest=f"keep_{k}")
    rp.add_argument("--json", action="store_true")
    b.set_defaults(func=cmd_backups)

    d = sub.add_parser("doctor", help="diagnostics")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_doctor)

    df = sub.add_parser("dev-fixtures", help="create isolated development volumes and mount table")
    df.add_argument("--root", default=".dev")
    df.add_argument("--with-restic", action="store_true", help="also create a disposable fixture restic repository")
    df.set_defaults(func=cmd_dev_fixtures)

    c = sub.add_parser("contract", help="regenerate OpenAPI and example fixtures")
    c.add_argument("--out", default="contracts/v1")
    c.set_defaults(func=cmd_contract)

    o = sub.add_parser("observe", help="record a health sample or summarize the observation log")
    osub = o.add_subparsers(dest="action", required=True)
    osub.add_parser("record")
    orp = osub.add_parser("report")
    orp.add_argument("--hours", type=float, default=48)
    o.set_defaults(func=cmd_observe)

    g = sub.add_parser("gate", help="camera engine feasibility gate")
    from .gate.cli import add_gate_arguments
    add_gate_arguments(g)
    g.set_defaults(func=cmd_gate)
    return ap


def quiet_third_party_loggers() -> None:
    """httpx logs every request URL at INFO, and the ntfy URL carries the
    private topic. Only warnings from HTTP client libraries are kept."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    quiet_third_party_loggers()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except ServiceError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
