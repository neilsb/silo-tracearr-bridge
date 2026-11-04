"""Environment configuration; secrets are deliberately excluded from repr."""

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    silo_url: str
    silo_api_key: str = field(repr=False)
    bridge_api_key: str = field(repr=False)
    host: str = "0.0.0.0"
    port: int = 8097
    name: str = "Silo (Tracearr bridge)"

    def __post_init__(self):
        url = urlsplit(self.silo_url)
        if (url.scheme not in ("http", "https") or not url.hostname
                or url.username or url.password or url.query or url.fragment):
            raise ValueError("SILO_URL must be an HTTP(S) base URL without credentials or query")
        for key in (self.silo_api_key, self.bridge_api_key):
            if not key or not key.isascii() or any(c.isspace() for c in key) or '"' in key:
                raise ValueError("API keys must be nonempty ASCII tokens without whitespace or quotes")
        if self.silo_api_key == self.bridge_api_key:
            raise ValueError("BRIDGE_API_KEY must differ from SILO_API_KEY")
        if not 1 <= self.port <= 65535:
            raise ValueError("BRIDGE_PORT must be between 1 and 65535")

    @classmethod
    def from_env(cls):
        return cls(
            silo_url=os.environ.get("SILO_URL", "").rstrip("/"),
            silo_api_key=os.environ.get("SILO_API_KEY", ""),
            bridge_api_key=os.environ.get("BRIDGE_API_KEY", ""),
            host=os.environ.get("BRIDGE_HOST", "0.0.0.0"),
            port=int(os.environ.get("BRIDGE_PORT", "8097")),
            name=os.environ.get("BRIDGE_NAME", "Silo (Tracearr bridge)"),
        )
