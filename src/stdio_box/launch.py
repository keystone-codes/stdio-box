"""Run provider installation at container start.

``install_step`` is what ``stdio-box launch`` calls before the process
split. The split (spawner, wrapper, per-slot uids) is not in this module.
``run_launch`` returns after a successful install so the caller that owns
the split can invoke ``install_step`` and continue instead of exiting.

On success, pass ``InstalledServer.argv0`` to the spawner's argv allow
list and pass ``installed_payload`` as the wrapper's ``--installed``
argument. ``InstalledServer.args`` is the child argv tail.
``install_status`` is the ``install`` object for ``credsctl status`` and
``/healthz``. ``InstalledServer.key`` is ``server.installed_key`` for
``creds_providers``. The ``install`` log line is the audit event until the
store records it.
"""

import os
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from stdio_box.boxcommand import BoxCommandError, parse_box_command
from stdio_box.config import ConfigError, InstallSettings, load_settings
from stdio_box.install import (
    INSTALLER_UID,
    InstalledServer,
    InstallFailed,
    configure_install_logging,
    ensure_installed,
    installed_payload,
    installer_environment,
    log_event,
    prepare_cache_root,
    setuid_available,
    verify_against_lock,
)
from stdio_box.provider import ProviderError, load_provider


def install_step(settings: InstallSettings) -> InstalledServer:
    """Parse ``BOX_COMMAND``, check the lock, and install as uid 10002 when possible."""
    if not settings.command:
        raise ConfigError(["BOX_COMMAND is required"])
    if settings.provider_dir is None:
        raise ConfigError(["BOX_PROVIDER is required"])
    command = parse_box_command(settings.command)
    provider = load_provider(settings.provider_dir)
    verify_against_lock(command, settings.provider_dir, provider)
    run_as = INSTALLER_UID if setuid_available() else None
    if run_as is None:
        log_event("install_isolation_unavailable")
    prepare_cache_root(settings.cache_dir, run_as)
    return ensure_installed(
        command,
        provider,
        settings.provider_dir,
        settings.cache_dir,
        run_as=run_as,
        timeout=settings.install_timeout_seconds,
        env=installer_environment(
            settings.cache_dir,
            pip_index_url=settings.pip_index_url,
            npm_registry=settings.npm_registry,
        ),
    )


def run_launch(environ: Mapping[str, str] | None = None) -> int:
    """Install the provider server. Exit 3 on failure. Do not fork the spawner."""
    configure_install_logging()
    source = os.environ if environ is None else environ
    try:
        settings = load_settings(source)
        if not settings.command:
            raise ConfigError(["BOX_COMMAND is required"])
        if settings.provider_dir is None:
            raise ConfigError(["BOX_PROVIDER is required"])
    except ConfigError as exc:
        for message in exc.messages:
            print(f"config error: {message}", file=sys.stderr)
        return 2
    try:
        installed = install_step(settings)
    except BoxCommandError as exc:
        log_event("provider_install_failed", code=exc.code, reason=exc.message)
        return 3
    except ProviderError as exc:
        reason = exc.messages[0] if exc.messages else "provider is not valid"
        log_event("provider_install_failed", code="provider_invalid", reason=reason)
        return 3
    except InstallFailed as exc:
        log_event("provider_install_failed", code="install_failed", reason=exc.reason)
        return 3
    log_event(
        "install",
        key=installed.key,
        at=installed.at,
        duration_ms=installed.duration_ms,
        from_cache=installed.from_cache,
        outcome="ok",
    )
    print(installed_payload(installed), flush=True)
    return 0


def run_install(argv: list[str], environ: Mapping[str, str] | None = None) -> int:
    """Install without the process split. A non-root caller does not change uid."""
    configure_install_logging()
    try:
        provider_dir, cache_dir = _install_flags(argv)
    except ValueError:
        print(_INSTALL_USAGE, file=sys.stderr)
        return 2
    source = os.environ if environ is None else environ
    try:
        settings = load_settings(source)
    except ConfigError as exc:
        for message in exc.messages:
            print(f"config error: {message}", file=sys.stderr)
        return 2
    if provider_dir is None:
        provider_dir = settings.provider_dir
    if provider_dir is None:
        print("config error: BOX_PROVIDER is required", file=sys.stderr)
        return 2
    if cache_dir is not None:
        settings = replace(settings, cache_dir=cache_dir)
    try:
        provider = load_provider(provider_dir)
        command_text = settings.command or provider.server.command
        command = parse_box_command(command_text)
        run_as = INSTALLER_UID if setuid_available() else None
        prepare_cache_root(settings.cache_dir, run_as)
        installed = ensure_installed(
            command,
            provider,
            provider_dir,
            settings.cache_dir,
            run_as=run_as,
            timeout=settings.install_timeout_seconds,
            env=installer_environment(
                settings.cache_dir,
                pip_index_url=settings.pip_index_url,
                npm_registry=settings.npm_registry,
            ),
        )
    except BoxCommandError as exc:
        log_event("provider_install_failed", code=exc.code, reason=exc.message)
        return 3
    except ProviderError as exc:
        reason = exc.messages[0] if exc.messages else "provider is not valid"
        log_event("provider_install_failed", code="provider_invalid", reason=reason)
        return 3
    except InstallFailed as exc:
        log_event("provider_install_failed", code="install_failed", reason=exc.reason)
        return 3
    log_event(
        "install",
        key=installed.key,
        at=installed.at,
        duration_ms=installed.duration_ms,
        from_cache=installed.from_cache,
        outcome="ok",
    )
    print(installed_payload(installed), flush=True)
    return 0


_INSTALL_USAGE = "usage: stdio-box install [--provider-dir DIR] [--cache DIR]"


def _install_flags(argv: list[str]) -> tuple[Path | None, Path | None]:
    provider_dir: Path | None = None
    cache_dir: Path | None = None
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag in {"--provider-dir", "--cache"}:
            if index + 1 >= len(argv):
                raise ValueError(flag)
            value = Path(argv[index + 1])
            if flag == "--provider-dir":
                provider_dir = value
            else:
                cache_dir = value
            index += 2
            continue
        raise ValueError(flag)
    return provider_dir, cache_dir
