"""Installed server as the wrapper receives it on ``--installed``.

``launch`` passes ``installed_payload`` (``path``, ``key``, ``from_cache``,
``duration_ms``, ``at``, ``args``). ``path`` is ``argv0``. ``args`` is the
child argv tail. Older payloads that say ``argv0`` instead of ``path`` still
parse.
"""

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class InstalledServer:
    argv0: str
    key: str
    from_cache: bool = False
    duration_ms: int = 0
    at: str = ""
    args: tuple[str, ...] = ()

    def to_json(self) -> str:
        return json.dumps(
            {
                "path": self.argv0,
                "key": self.key,
                "from_cache": self.from_cache,
                "duration_ms": self.duration_ms,
                "at": self.at,
                "args": list(self.args),
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> "InstalledServer":
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("installed server JSON must be an object")
        path = data.get("path", data.get("argv0"))
        if not isinstance(path, str):
            raise ValueError("installed server JSON must contain path")
        if not isinstance(data.get("key"), str):
            raise ValueError("installed server JSON must contain key")
        raw_args = data.get("args", [])
        if not isinstance(raw_args, list) or any(not isinstance(item, str) for item in raw_args):
            raise ValueError("installed server args must be a list of strings")
        at = data.get("at", "")
        if not isinstance(at, str):
            raise ValueError("installed server at must be a string")
        return cls(
            argv0=path,
            key=data["key"],
            from_cache=bool(data.get("from_cache", False)),
            duration_ms=int(data.get("duration_ms", 0)),
            at=at,
            args=tuple(raw_args),
        )
