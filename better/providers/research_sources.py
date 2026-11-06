"""Reviewed source identities, independent of an agent's self-reported label.

Club domains checked against https://www.legaseriea.it/team and club-info
website links on 2026-10-29. A recognized host does not prove a factual claim,
publication date or lineup confirmation. Unknown hosts remain unverified.
"""

from urllib.parse import urlsplit

OFFICIAL_DOMAINS = {
    **dict.fromkeys((
        "atalanta.it", "bolognafc.it", "cagliaricalcio.com", "comofootball.com",
        "acffiorentina.com", "frosinonecalcio.com", "genoacfc.it", "inter.it",
        "juventus.com", "sslazio.it", "uslecce.it", "acmilan.com", "acmonza.com",
        "sscnapoli.it", "parmacalcio1913.com", "asroma.com", "sassuolocalcio.it",
        "torinofc.it", "udinese.it", "veneziafc.it",
    ), "OFFICIAL_CLUB"),
    "legaseriea.it": "OFFICIAL_COMPETITION",
}
SOURCE_TYPES = (
    "OFFICIAL_CLUB", "OFFICIAL_COMPETITION", "FEDERATION", "INSTITUTIONAL",
    "TRUSTED_MEDIA", "LOCAL_MEDIA", "BROADCASTER", "TEAM_REPORTER", "AGGREGATOR", "SECONDARY",
    "UNKNOWN",
)


def source_category(source):
    label = source.source_type.strip().upper().replace(" ", "_").replace("-", "_")
    host = (urlsplit(source.url).hostname or "").lower().removeprefix("www.")
    canonical_host = (urlsplit(source.canonical_url or source.url).hostname or "").lower().removeprefix("www.")
    if host != canonical_host:
        return "UNKNOWN"
    identity = OFFICIAL_DOMAINS.get(host)
    if label.startswith("OFFICIAL_") or label in {"FEDERATION", "INSTITUTIONAL"}:
        # Neither an official-looking URL path nor a free-text label proves identity.
        return identity or "UNKNOWN"
    aliases = {"TEAM_NEWS_AGGREGATOR": "AGGREGATOR", "LINEUP_AGGREGATOR": "AGGREGATOR"}
    return aliases.get(label, label if label in SOURCE_TYPES else "UNKNOWN")
