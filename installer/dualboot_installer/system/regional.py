"""Parsers for the system's regional data files (tzdata, glibc locales)."""

from __future__ import annotations


def parse_iso3166(text: str) -> dict[str, str]:
    """iso3166.tab: ``CC<TAB>Country name`` -> {CC: name}."""
    countries: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and len(parts[0]) == 2:
            countries[parts[0]] = parts[1].strip()
    return countries


def parse_zone_tab(text: str) -> dict[str, list[str]]:
    """zone.tab: ``CC<TAB>coordinates<TAB>Zone<TAB>comments`` -> {CC: [zones]}."""
    zones: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            zones.setdefault(parts[0], []).append(parts[2].strip())
    return zones


def parse_supported_locales(text: str) -> list[str]:
    """glibc ``SUPPORTED`` or ``locale.gen``: keep the UTF-8 locales."""
    locales: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("#").strip()
        parts = line.split()
        if len(parts) == 2 and parts[1].upper() == "UTF-8" and parts[0].upper().endswith(".UTF-8"):
            locales.append(parts[0])
    return sorted(dict.fromkeys(locales))
