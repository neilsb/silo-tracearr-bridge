"""Bounded playback-poster cache; URLs come only from Silo observations."""

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import threading
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener
from urllib.error import HTTPError, URLError

from .client import NoRedirects, UpstreamError


@dataclass(frozen=True)
class Image:
    body: bytes
    content_type: str


class Artwork:
    def __init__(self, config):
        self.base = config.silo_url.rstrip("/")
        self.lock = threading.Lock()
        self.urls = OrderedDict()
        self.images = OrderedDict()

    def register(self, item_id, reference):
        if not reference:
            return None
        url = urljoin(self.base + "/", reference)
        parsed, base = urlsplit(url), urlsplit(self.base)
        prefix = base.path.rstrip("/") + "/api/v2/artwork/"
        if ((parsed.scheme, parsed.netloc) != (base.scheme, base.netloc)
                or not parsed.path.startswith(prefix) or parsed.username or parsed.password
                or parsed.fragment or "%" in parsed.path or ".." in parsed.path.split("/")):
            return None
        # Signed query tokens can rotate without the image itself changing.
        tag = hashlib.sha256(parsed.path.encode()).hexdigest()[:24]
        with self.lock:
            self.urls[(item_id, tag)] = url
            self.urls.move_to_end((item_id, tag))
            while len(self.urls) > 512:
                self.urls.popitem(last=False)
        return tag

    def get(self, item_id, tag=None):
        with self.lock:
            if tag is None:
                tag = next((key[1] for key in reversed(self.urls) if key[0] == item_id), None)
            key = (item_id, tag)
            cached = self.images.get(key)
            if cached:
                self.images.move_to_end(key)
                return cached
            url = self.urls.get(key)
        if not url:
            return None
        try:
            # Silo's artwork URL is already signed. Never send an admin key.
            with build_opener(NoRedirects).open(Request(url, headers={"Accept": "image/*"}), timeout=8) as response:
                mime = response.headers.get_content_type()
                if mime not in {"image/jpeg", "image/png", "image/webp", "image/avif", "image/gif"}:
                    raise UpstreamError("Silo artwork returned an unsupported content type")
                body = response.read(8 * 1024 * 1024 + 1)
                if not body or len(body) > 8 * 1024 * 1024:
                    raise UpstreamError("Silo artwork was empty or exceeded 8 MiB")
                image = Image(body, mime)
        except HTTPError as error:
            code = error.code
            error.close()
            raise UpstreamError(f"Silo artwork returned HTTP {code}") from None
        except (URLError, OSError, TimeoutError):
            raise UpstreamError("Silo artwork connection failed") from None
        with self.lock:
            self.images[key] = image
            self.images.move_to_end(key)
            while len(self.images) > 16:
                self.images.popitem(last=False)
        return image
