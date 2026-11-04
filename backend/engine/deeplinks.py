from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import Config

_LOGGER = logging.getLogger(__name__)

JUSTWATCH_GRAPHQL = "https://apis.justwatch.com/graphql"

JUSTWATCH_QUERY = """
query GetSearchTitles(
  $searchTitlesFilter: TitleFilter!,
  $country: Country!,
  $language: Language!,
  $first: Int!,
  $filter: OfferFilter!
) {
  popularTitles(
    country: $country
    filter: $searchTitlesFilter
    first: $first
    sortBy: POPULAR
    sortRandomSeed: 0
  ) {
    edges {
      node {
        id
        objectType
        content(country: $country, language: $language) {
          title
          fullPath
        }
        offers(country: $country, platform: WEB, filter: $filter) {
          monetizationType
          presentationType
          standardWebURL
          package {
            clearName
            technicalName
            shortName
          }
        }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class DeepLink:
    """A resolved tvOS deep link for one title/service."""

    title: str
    service: str
    url: str
    source: str = "justwatch"


def normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s']", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _quote_json(payload: dict[str, str]) -> str:
    return urllib.parse.quote(
        json.dumps(payload, separators=(",", ":")),
        safe="",
    )


def peacock_deeplink(payload: dict[str, str]) -> str:
    return "https://www.peacocktv.com/deeplink?deeplinkData=" + _quote_json(payload)


def adapt_offer_url(url: str, *, play: bool = False) -> str | None:
    """Turn a provider web URL into the URL pyatv should launch.

    JustWatch mostly exposes web URLs. Some are universal links and already
    work on tvOS (Disney+, Apple TV, Max). Others need the provider's own
    deep-link form; Peacock and Hulu are handled here.
    """
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path

    if "peacocktv.com" in host:
        parts = [part for part in path.split("/") if part]
        # /watch/asset/tv/<show>/<providerSeriesId>/seasons/1/episodes/...
        series_id = next(
            (
                part
                for part in parts
                if part.isdigit() and len(part) >= 10
            ),
            None,
        )
        pvid = next(
            (
                part
                for part in reversed(parts)
                if re.fullmatch(
                    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
                    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    r"[0-9a-fA-F]{12}",
                    part,
                )
            ),
            None,
        )
        if play and pvid:
            return peacock_deeplink(
                {"pvid": pvid, "type": "PROGRAMME", "action": "PLAY"}
            )
        if series_id:
            return peacock_deeplink(
                {
                    "providerSeriesId": series_id,
                    "type": "SERIES",
                    "action": "PLAY" if play else "PDP",
                }
            )
        if pvid:
            return peacock_deeplink(
                {
                    "pvid": pvid,
                    "type": "PROGRAMME",
                    "action": "PLAY" if play else "PDP",
                }
            )
        return None

    if "hulu.com" in host:
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"watch", "series", "movie"}:
            return f"hulu://{parts[0]}/{parts[1]}"
        return None

    if "watch.amazon.com" in host or "amazon.com" in host:
        query = urllib.parse.parse_qs(parsed.query)
        gti = (query.get("gti") or [None])[0]
        if gti:
            return "https://app.primevideo.com/detail?gti=" + urllib.parse.quote(
                gti, safe=""
            )

    # Disney+, Apple TV, Max and others publish universal links that pyatv
    # can hand to tvOS directly. Keep the original URL for those.
    return url


def _service_matches(package: dict[str, Any], wanted: str | None) -> bool:
    if not wanted:
        return True
    haystack = " ".join(
        str(package.get(key, "") or "")
        for key in ("clearName", "technicalName", "shortName")
    ).lower()
    key = wanted.lower().strip()
    aliases = {
        "peacock": ["peacock"],
        "hulu": ["hulu"],
        "disney+": ["disney"],
        "disney": ["disney"],
        "max": ["max", "hbo"],
        "hbo max": ["max", "hbo"],
        "prime video": ["primevideo", "prime video", "amazonprime"],
        "amazon": ["primevideo", "prime video", "amazonprime"],
        "apple tv+": ["apple tv"],
        "apple tv": ["apple tv"],
        "netflix": ["netflix"],
        "paramount+": ["paramount"],
        "paramount": ["paramount"],
    }
    tokens = aliases.get(key, [key])
    return any(token in haystack for token in tokens)


def _service_name(package: dict[str, Any]) -> str:
    name = str(package.get("clearName", "") or "the service")
    for suffix in (" Premium Plus", " Premium"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    if name in {"HBO Max", "Max"}:
        return "Max"
    if name == "Disney Plus":
        return "Disney+"
    if name == "Amazon Prime Video":
        return "Prime Video"
    if name == "Apple TV Store":
        return "Apple TV"
    return name


def _monetization_rank(value: str) -> int:
    return {
        "FLATRATE": 0,
        "FREE": 0,
        "ADS": 1,
        "RENT": 2,
        "BUY": 3,
    }.get(value, 9)


class JustWatchResolver:
    """Resolve a title to a streaming service and a tvOS deep link.

    Uses the public (unofficial) JustWatch GraphQL endpoint. Results are
    cached in memory. A local catalog entry in config.toml always wins.
    """

    def __init__(self, config: Config):
        self.enabled = bool(config.get("appletv.resolver.enabled", True))
        self.provider = str(config.get("appletv.resolver.provider", "justwatch"))
        self.country = str(config.get("appletv.resolver.country", "US")).upper()
        self.language = str(config.get("appletv.resolver.language", "en"))
        self.timeout = float(config.get("appletv.resolver.timeout", 8))
        self.cache_seconds = int(config.get("appletv.resolver.cache_seconds", 3600))
        self.first = int(config.get("appletv.resolver.max_results", 8))
        preferred = config.get("appletv.resolver.preferred", [])
        self.preferred = [
            str(item) for item in preferred
        ] if isinstance(preferred, list) else []
        self._cache: dict[
            tuple[str, str | None, bool], tuple[float, DeepLink | None]
        ] = {}
        self._lock = threading.Lock()

    def resolve(
        self,
        title: str,
        *,
        service: str | None = None,
        play: bool = False,
    ) -> DeepLink | None:
        if not self.enabled or self.provider.lower() != "justwatch":
            return None
        wanted = normalize(title)
        if not wanted:
            return None
        key = (wanted, normalize(service) if service else None, play)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                return cached[1]

        try:
            result = self._lookup(title, service=service, play=play)
        except Exception as exc:  # noqa: BLE001 - resolver is best effort
            _LOGGER.warning("JustWatch lookup failed for %r: %s", title, exc)
            result = None

        ttl = self.cache_seconds if result else min(300, self.cache_seconds)
        with self._lock:
            self._cache[key] = (now + ttl, result)
        return result

    def _lookup(
        self,
        title: str,
        *,
        service: str | None,
        play: bool,
    ) -> DeepLink | None:
        payload = {
            "operationName": "GetSearchTitles",
            "variables": {
                "first": self.first,
                "searchTitlesFilter": {
                    "searchQuery": title,
                    "includeTitlesWithoutUrl": True,
                },
                "country": self.country,
                "language": self.language,
                "filter": {"bestOnly": True},
            },
            "query": JUSTWATCH_QUERY,
        }
        request = urllib.request.Request(
            JUSTWATCH_GRAPHQL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Voice Assistant/0.1 (local Home Assistant assistant)",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))

        if data.get("errors"):
            _LOGGER.warning("JustWatch returned errors for %r: %s", title, data["errors"])
            return None
        edges = ((data.get("data") or {}).get("popularTitles") or {}).get("edges") or []
        wanted = normalize(title)

        exact: list[dict[str, Any]] = []
        for edge in edges:
            node = edge.get("node") or {}
            content = node.get("content") or {}
            if normalize(str(content.get("title", ""))) == wanted:
                exact.append(node)
        if not exact:
            return None

        # Keep JustWatch's popularity order. For a title shared by a show and
        # a movie, the first exact result is usually the one people mean.
        best: tuple[tuple[int, int, int, int], DeepLink] | None = None
        for candidate_rank, node in enumerate(exact):
            content = node.get("content") or {}
            offers = node.get("offers") or []
            for offer in offers:
                package = offer.get("package") or {}
                if not _service_matches(package, service):
                    continue
                url = adapt_offer_url(
                    str(offer.get("standardWebURL", "") or ""),
                    play=play,
                )
                if not url:
                    continue
                service_name = _service_name(package)
                if service:
                    service_rank = 0
                else:
                    service_rank = next(
                        (
                            index
                            for index, preferred in enumerate(self.preferred)
                            if _service_matches(package, preferred)
                        ),
                        len(self.preferred),
                    )
                addon_penalty = (
                    1
                    if "amazon channel" in str(package.get("clearName", "")).lower()
                    else 0
                )
                score = (
                    candidate_rank,
                    service_rank,
                    addon_penalty,
                    _monetization_rank(
                        str(offer.get("monetizationType", "") or "")
                    ),
                )
                link = DeepLink(
                    title=str(content.get("title") or title),
                    service=service_name,
                    url=url,
                    source="justwatch",
                )
                if best is None or score < best[0]:
                    best = (score, link)
        return best[1] if best else None
