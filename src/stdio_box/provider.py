"""Load and check a provider directory's provider.yaml."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

_FIELD_TOKEN = re.compile(r"\{([^{}]+)\}")
_SCHEMA_VALIDATOR: Draft202012Validator | None = None


class ProviderError(Exception):
    """The provider directory failed schema or semantic checks."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__("\n".join(messages))


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    secret: bool
    hint: str
    arg: str | None
    max_length: int


@dataclass(frozen=True)
class HttpAuth:
    kind: str
    basic: tuple[str, str] | None = None
    bearer: str | None = None
    header_name: str | None = None
    header_scheme: str | None = None
    header_field: str | None = None


@dataclass(frozen=True)
class HttpCheck:
    method: str
    url: str
    expect_status: tuple[int, ...]
    auth: HttpAuth | None
    headers: tuple[tuple[str, str], ...]
    identity: str | None
    timeout_seconds: int | None


@dataclass(frozen=True)
class ToolCheck:
    name: str
    arguments: dict | None
    timeout_seconds: int | None


@dataclass(frozen=True)
class Check:
    kind: str
    http: HttpCheck | None = None
    tool: ToolCheck | None = None


@dataclass(frozen=True)
class Variant:
    name: str
    title: str | None
    fields: tuple[str, ...]
    tools: tuple[str, ...] | None
    allowed_hosts: tuple[str, ...] | None
    checks: tuple[Check, ...]


@dataclass(frozen=True)
class Server:
    command: str
    bin: str
    lock: str
    static_env: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ToolRules:
    strip_prefix: str | None
    normalize: str | None
    rename: tuple[tuple[str, str], ...]
    hide: tuple[str, ...]


@dataclass(frozen=True)
class Discovery:
    mode: str
    env: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Provider:
    schema_version: int
    name: str
    title: str
    server: Server
    fields: tuple[Field, ...]
    variants: tuple[Variant, ...]
    tools: ToolRules | None
    discovery: Discovery
    identity_to_child: str
    directory: Path


def load_provider(directory: str | Path) -> Provider:
    """Read `<directory>/provider.yaml` and return a checked provider."""
    root = Path(directory)
    document_path = root / "provider.yaml"
    if not document_path.is_file():
        raise ProviderError(["provider.yaml: file not found"])
    try:
        loaded = yaml.safe_load(document_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProviderError([f"provider.yaml: {exc}"]) from exc
    if not isinstance(loaded, dict):
        raise ProviderError(["provider.yaml: document must be a mapping"])
    schema_errors = _schema_errors(loaded)
    if schema_errors:
        raise ProviderError(schema_errors)
    semantic_errors = _semantic_errors(root, loaded)
    if semantic_errors:
        raise ProviderError(semantic_errors)
    return _build(root, loaded)


def _schema_path() -> Path:
    here = Path(__file__).resolve()
    candidates = (
        here.parent / "schemas" / "provider-v1.json",
        here.parents[2] / "schemas" / "provider-v1.json",
        Path("/opt/stdio-box/schemas/provider-v1.json"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("schemas/provider-v1.json was not found")


def _validator() -> Draft202012Validator:
    global _SCHEMA_VALIDATOR
    if _SCHEMA_VALIDATOR is None:
        schema = json.loads(_schema_path().read_text(encoding="utf-8"))
        _SCHEMA_VALIDATOR = Draft202012Validator(schema)
    return _SCHEMA_VALIDATOR


def _schema_errors(document: dict) -> list[str]:
    messages: list[str] = []
    for error in _validator().iter_errors(document):
        for item in _flatten(error):
            messages.append(f"{_json_path(item)}: {item.message}")
    return messages


def _flatten(error: ValidationError) -> list[ValidationError]:
    if error.validator in {"oneOf", "anyOf"} and error.context:
        return [item for child in error.context for item in _flatten(child)]
    return [error]


def _json_path(error: ValidationError) -> str:
    path = ""
    for item in error.absolute_path:
        if isinstance(item, int):
            path += f"[{item}]"
        else:
            path = f"{path}.{item}" if path else str(item)
    return path or "<root>"


def _semantic_errors(directory: Path, document: dict) -> list[str]:
    errors: list[str] = []
    name = document["name"]
    if name != directory.name:
        errors.append(f"name: must equal directory name '{directory.name}'")

    server = document["server"]
    command = server["command"]
    lock_name = server["lock"]
    lock_path = directory / lock_name
    if lock_name == "requirements.txt" and not command.startswith("uvx"):
        errors.append("server.command: requirements.txt requires a command starting with 'uvx'")
    if lock_name == "package-lock.json" and not command.startswith("npx"):
        errors.append("server.command: package-lock.json requires a command starting with 'npx'")
    if not lock_path.is_file():
        errors.append(f"server.lock: '{lock_name}' does not exist")
    if lock_name == "package-lock.json" and not (directory / "package.json").is_file():
        errors.append("server.lock: package.json must exist beside package-lock.json")

    fields = document["fields"]
    fields_by_name: dict[str, dict] = {}
    seen_fields: set[str] = set()
    for index, field in enumerate(fields):
        field_name = field["name"]
        if field_name in seen_fields:
            errors.append(f"fields[{index}].name: duplicate field name '{field_name}'")
        seen_fields.add(field_name)
        fields_by_name[field_name] = field
        target = field.get("to", "env")
        arg = None if target == "env" else target["arg"]
        if field["secret"] and arg is not None:
            errors.append(
                f"fields[{index}].to: secret field {field_name} cannot be passed as an argument"
            )

    variants = document["variants"]
    seen_variants: set[str] = set()
    seen_field_sets: dict[frozenset[str], str] = {}
    for index, variant in enumerate(variants):
        variant_name = variant["name"]
        if variant_name in seen_variants:
            errors.append(f"variants[{index}].name: duplicate variant name '{variant_name}'")
        seen_variants.add(variant_name)
        variant_fields = tuple(variant["fields"])
        for field_index, field_name in enumerate(variant_fields):
            if field_name not in fields_by_name:
                errors.append(
                    f"variants[{index}].fields[{field_index}]: unknown field '{field_name}'"
                )
        field_set = frozenset(variant_fields)
        previous = seen_field_sets.get(field_set)
        if previous is not None:
            errors.append(
                f"variants[{index}].fields: variant '{variant_name}' has the same fields as "
                f"variant '{previous}'"
            )
        else:
            seen_field_sets[field_set] = variant_name
        variant_field_set = set(variant_fields)
        if "allowed_hosts" in variant and not any(
            fields_by_name[item]["type"] == "url"
            for item in variant_field_set
            if item in fields_by_name
        ):
            errors.append(
                f"variants[{index}].allowed_hosts: allowed_hosts requires a field of type url"
            )
        for check_index, check in enumerate(variant["checks"]):
            prefix = f"variants[{index}].checks[{check_index}]"
            if "http" in check:
                _check_http(
                    prefix, check["http"], variant_name, variant_field_set, fields_by_name, errors
                )

    discovery = document["discovery"]
    if discovery["mode"] == "placeholder":
        env = discovery.get("env") or {}
        covered = any(
            all(field_name in env and env[field_name] != "" for field_name in variant["fields"])
            for variant in variants
        )
        if not covered:
            errors.append(
                "discovery.env: placeholder values must cover every field of at least one variant"
            )
        for field_name, value in env.items():
            field = fields_by_name.get(field_name)
            if field is None or field["type"] != "url":
                continue
            host = urlsplit(str(value)).hostname
            if not host or not host.endswith(".invalid"):
                errors.append(f"discovery.env.{field_name}: host must end with .invalid")
    return errors


def _check_http(
    prefix: str,
    http: dict,
    variant_name: str,
    variant_fields: set[str],
    fields_by_name: dict[str, dict],
    errors: list[str],
) -> None:
    _check_interpolation(
        f"{prefix}.http.url", http["url"], variant_name, variant_fields, fields_by_name, errors
    )
    for header_name, header_value in (http.get("headers") or {}).items():
        _check_interpolation(
            f"{prefix}.http.headers.{header_name}",
            header_value,
            variant_name,
            variant_fields,
            fields_by_name,
            errors,
        )
    auth = http.get("auth")
    if auth is None:
        return
    if "basic" in auth:
        _require_field(
            f"{prefix}.http.auth.basic[0]",
            auth["basic"][0],
            variant_name,
            variant_fields,
            fields_by_name,
            errors,
            secret=False,
        )
        _require_field(
            f"{prefix}.http.auth.basic[1]",
            auth["basic"][1],
            variant_name,
            variant_fields,
            fields_by_name,
            errors,
            secret=True,
        )
    elif "bearer" in auth:
        _require_field(
            f"{prefix}.http.auth.bearer",
            auth["bearer"],
            variant_name,
            variant_fields,
            fields_by_name,
            errors,
            secret=True,
        )
    else:
        _require_field(
            f"{prefix}.http.auth.header.field",
            auth["header"]["field"],
            variant_name,
            variant_fields,
            fields_by_name,
            errors,
            secret=True,
        )


def _check_interpolation(
    path: str,
    text: str,
    variant_name: str,
    variant_fields: set[str],
    fields_by_name: dict[str, dict],
    errors: list[str],
) -> None:
    for token in _FIELD_TOKEN.findall(text):
        field = fields_by_name.get(token)
        if field is None:
            errors.append(f"{path}: unknown field '{token}'")
            continue
        if token not in variant_fields:
            errors.append(f"{path}: field '{token}' is not part of variant '{variant_name}'")
            continue
        if field["secret"]:
            errors.append(f"{path}: secret field '{token}' cannot be interpolated")


def _require_field(
    path: str,
    field_name: str,
    variant_name: str,
    variant_fields: set[str],
    fields_by_name: dict[str, dict],
    errors: list[str],
    *,
    secret: bool,
) -> None:
    field = fields_by_name.get(field_name)
    if field is None:
        errors.append(f"{path}: unknown field '{field_name}'")
        return
    if field_name not in variant_fields:
        errors.append(f"{path}: field '{field_name}' is not part of variant '{variant_name}'")
        return
    # basic[0] is the username and may be non-secret, as in the atlassian descriptor.
    # The credential slot (basic[1], bearer, header.field) has to be a secret field.
    if secret and not field["secret"]:
        errors.append(f"{path}: field '{field_name}' must be a secret")


def _build(directory: Path, document: dict) -> Provider:
    server = document["server"]
    tools = document.get("tools")
    discovery = document["discovery"]
    return Provider(
        schema_version=document["schema_version"],
        name=document["name"],
        title=document["title"],
        server=Server(
            command=server["command"],
            bin=server["bin"],
            lock=server["lock"],
            static_env=tuple((server.get("static_env") or {}).items()),
        ),
        fields=tuple(_build_field(item) for item in document["fields"]),
        variants=tuple(_build_variant(item) for item in document["variants"]),
        tools=None if tools is None else _build_tools(tools),
        discovery=Discovery(
            mode=discovery["mode"], env=tuple((discovery.get("env") or {}).items())
        ),
        identity_to_child=document.get("identity_to_child", "none"),
        directory=directory,
    )


def _build_field(item: dict) -> Field:
    target = item.get("to", "env")
    return Field(
        name=item["name"],
        type=item["type"],
        secret=item["secret"],
        hint=item["hint"],
        arg=None if target == "env" else target["arg"],
        max_length=item.get("max_length", 4096),
    )


def _build_variant(item: dict) -> Variant:
    return Variant(
        name=item["name"],
        title=item.get("title"),
        fields=tuple(item["fields"]),
        tools=None if "tools" not in item else tuple(item["tools"]),
        allowed_hosts=None if "allowed_hosts" not in item else tuple(item["allowed_hosts"]),
        checks=tuple(_build_check(check) for check in item["checks"]),
    )


def _build_check(item: dict) -> Check:
    if "http" in item:
        http = item["http"]
        return Check(kind="http", http=_build_http(http))
    tool = item["tool"]
    arguments = tool.get("arguments")
    return Check(
        kind="tool",
        tool=ToolCheck(
            name=tool["name"],
            arguments=None if arguments is None else json.loads(json.dumps(arguments)),
            timeout_seconds=tool.get("timeout_seconds"),
        ),
    )


def _build_http(item: dict) -> HttpCheck:
    return HttpCheck(
        method=item["method"],
        url=item["url"],
        expect_status=tuple(item["expect_status"]),
        auth=None if "auth" not in item else _build_auth(item["auth"]),
        headers=tuple((item.get("headers") or {}).items()),
        identity=item.get("identity"),
        timeout_seconds=item.get("timeout_seconds"),
    )


def _build_auth(item: dict) -> HttpAuth:
    if "basic" in item:
        user, secret = item["basic"]
        return HttpAuth(kind="basic", basic=(user, secret))
    if "bearer" in item:
        return HttpAuth(kind="bearer", bearer=item["bearer"])
    header = item["header"]
    return HttpAuth(
        kind="header",
        header_name=header["name"],
        header_scheme=header.get("scheme"),
        header_field=header["field"],
    )


def _build_tools(item: dict) -> ToolRules:
    return ToolRules(
        strip_prefix=item.get("strip_prefix"),
        normalize=item.get("normalize"),
        rename=tuple((item.get("rename") or {}).items()),
        hide=tuple(item.get("hide") or ()),
    )
