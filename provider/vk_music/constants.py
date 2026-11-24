"""Shared constants of the VK Music provider."""
from __future__ import annotations

import re

from music_assistant_models.enums import ProviderFeature

CONF_TOKEN = "token"
CONF_USER_AGENT = "user_agent"
SUPPORTED_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.BROWSE,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.RECOMMENDATIONS,
}

# VK catalog.getAudio understands only ?section=... links; ?catalog=...,
# vk.cc shortlinks, ?popup=... and /artist pages silently fall back to the
# default "Главная" digest, so they must not become browse folders.
_CATALOG_LINK_PREFIX = "https://vk.com/audio?section="
# VK catalog blocks are paginated via next_from, but MA's browse API has no
# server-side paging (the frontend fetches a folder in ONE request and then
# virtual-scrolls locally), so pages are folded into one listing. The first
# view is bounded; if the block chain still has more, a single stateless
# continuation folder «Ещё треки <chain_pos>» is appended — the count in the
# path segment re-resolves the position by re-walking the chain, so it
# survives provider restarts (no server-side cache). A mid-chain VK error
# returns the partial listing instead of failing the whole browse.
_MAX_AGGREGATED_TRACKS = 50
_MAX_AGGREGATED_PLAYLISTS = 1000
_MORE_TRACKS_RE = re.compile(r"^Ещё треки(?: (\d+)(?:-(\d+))?)?$")
