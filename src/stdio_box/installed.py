"""Result of the provider install step.

``launch`` passes this to the spawner (the ``argv0`` allow-list) and to the
wrapper as the ``--installed`` JSON object. The install step itself is filled
in later; this revision only builds the value from the debug override.
"""

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class InstalledServer:
    argv0: str
    key: str
    from_cache: bool = False
    duration_ms: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "argv0": self.argv0,
                "key": self.key,
                "from_cache": self.from_cache,
                "duration_ms": self.duration_ms,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> "InstalledServer":
        data = json.loads(text)
        if not isinstance(data, dict) or not isinstance(data.get("argv0"), str):
            raise ValueError("installed server JSON must contain argv0")
        if not isinstance(data.get("key"), str):
            raise ValueError("installed server JSON must contain key")
        return cls(
            argv0=data["argv0"],
            key=data["key"],
            from_cache=bool(data.get("from_cache", False)),
            duration_ms=int(data.get("duration_ms", 0)),
        )
