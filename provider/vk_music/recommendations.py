"""VK «Обзор» (explore) sections as recommendation rows on the MA home page.

MA aggregates these through ProviderFeature.RECOMMENDATIONS:
- get_recommendations returns ROW descriptors only (name/subtitle/cover) and
  is served from a ~30 min cache so the home page stays fast;
- get_recommendation_items fetches the actual items of one row on demand,
  re-walking the VK block chain (bounded, see _walk_block_audios).
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import RecommendationFolder

from .base import ProviderContract
from .vendor.vkpymusic.models import Playlist as VKPlaylist

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant_models.media_items import MediaItemType


class RecommendationsMixin(ProviderContract):
    """VK explore digest as home-page recommendation rows."""

    _REC_ROWS_TTL = 30 * 60  # rows descriptors are cheap to cache
    _REC_ROW_ITEMS = 25  # a shelf does not need the full block chain
    _rec_rows_cache: tuple[float, list[RecommendationFolder]] | None = None
    _rec_explore_url: str | None = None

    async def _explore_digest(self) -> tuple[dict, dict] | None:
        """Fetch the «Обзор» digest, resolving the user-specific section url.

        Returns (digest, target_section) or None when the section is missing.
        """
        if not self._rec_explore_url:
            root = await self._catalog_digest()
            for section in root.get("catalog", {}).get("sections", []):
                if self._clean_title(section.get("title")) == "Обзор" and section.get("url"):
                    self._rec_explore_url = section["url"]
                    break
        if not self._rec_explore_url:
            return None
        digest = await self._catalog_digest(self._rec_explore_url)
        target = self._resolve_target_section(digest, self._rec_explore_url)
        if target is None:
            return None
        return digest, target

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Return the VK explore sections as home-page rows (cached descriptors)."""
        now = time.time()
        cached = self._rec_rows_cache
        if cached and now - cached[0] < self._REC_ROWS_TTL:
            return cached[1]
        explore = await self._explore_digest()
        rows: list[RecommendationFolder] = []
        if explore:
            digest, target = explore
            for block in target.get("blocks", []):
                data_type = block.get("data_type")
                if data_type == "music_audios":
                    ids = block.get("audios_ids", [])
                elif data_type == "music_playlists":
                    ids = block.get("playlists_ids", [])
                else:
                    continue
                info = (block.get("meta") or {}).get("show_all_info") or {}
                title = self._clean_title(info.get("title"))
                if not ids or not title:
                    continue
                prefix = "vkr_a" if data_type == "music_audios" else "vkr_p"
                rows.append(
                    RecommendationFolder(
                        item_id=f"{prefix}:{block.get('id')}",
                        provider=self.instance_id,
                        name=title,
                    )
                )
        self._rec_rows_cache = (now, rows)
        return rows

    async def get_recommendation_items(self, item_id: str) -> Sequence[MediaItemType]:
        """Fetch the items of one recommendation row (VK explore block)."""
        prefix, separator, block_id = item_id.partition(":")
        if not separator or prefix not in ("vkr_a", "vkr_p") or not block_id:
            return []
        explore = await self._explore_digest()
        if not explore:
            return []
        digest, target = explore
        block = next((b for b in target.get("blocks", []) if b.get("id") == block_id), None)
        if block is None:
            return []
        if prefix == "vkr_a":
            tracks, _, _ = await self._walk_block_audios(
                block, digest, skip=0, limit=self._REC_ROW_ITEMS, seen_ids=set()
            )
            return tracks
        # playlists row
        pool = digest.get("playlists", [])
        items: list[MediaItemType] = []
        for playlist_id in block.get("playlists_ids", [])[: self._REC_ROW_ITEMS]:
            entry = next(
                (
                    p
                    for p in pool
                    if f"{p.get('owner_id')}_{p.get('id')}" == str(playlist_id)
                ),
                None,
            )
            if entry is not None:
                items.append(
                    self._parse_vk_collection(VKPlaylist.from_json(entry), MediaType.PLAYLIST)
                )
        return items
