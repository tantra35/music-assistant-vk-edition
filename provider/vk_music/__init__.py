"""VK Music provider for Music Assistant 2.10.9.

Uses a token obtained by the interactive VK setup flow. The provider does not
store VK passwords or bypass service access controls.

The implementation lives in thematic mixin modules; this file wires them
together and keeps only the shared plumbing (auth init, request execution,
provider mapping). Setup flow: see setup_flow.py; auth helpers: auth.py; the
vendored vkpymusic fork: vendor/.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from music_assistant.models.music_provider import MusicProvider
from music_assistant_models.errors import LoginFailed, SetupFailedError
from music_assistant_models.media_items import ProviderMapping

from .books_podcasts import BooksPodcastsMixin
from .catalog import CatalogBrowseMixin
from .constants import CONF_TOKEN, CONF_USER_AGENT, SUPPORTED_FEATURES
from .media import MediaApiMixin
from .parsers import MediaParsersMixin
from .recommendations import RecommendationsMixin
from .search import SearchMixin
from .streaming import StreamingMixin
from .vendor.vkpymusic import Service
from .vendor.vkpymusic.vk_api.vk_api_extensions import make_request_async

if TYPE_CHECKING:
    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the provider and its secure account configuration."""
    # VKMusicProvider's resolvable MRO part (mixins) has only object.__init__,
    # the real one comes from the (unresolved-in-IDE) MusicProvider base
    return VKMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)  # type: ignore[arg-type]


class VKMusicProvider(
    MediaParsersMixin,
    SearchMixin,
    MediaApiMixin,
    CatalogBrowseMixin,
    BooksPodcastsMixin,
    StreamingMixin,
    RecommendationsMixin,
    MusicProvider,
):
    """VK track search, artist discovery and playlist/album browsing."""

    _service: Service

    @property
    def is_streaming_provider(self) -> bool:
        """VK is an online music provider."""
        return True

    @property
    def max_concurrent_streams(self) -> int:
        """Use a conservative per-account stream limit."""
        return 2

    async def handle_async_init(self) -> None:
        """Create the API client and validate the saved token."""
        token = str(self.get_setup_value(CONF_TOKEN) or "")
        user_agent = str(self.get_setup_value(CONF_USER_AGENT) or "")
        if not token or not user_agent:
            raise LoginFailed("VK access token and User-Agent are required")
        self._service = Service(user_agent=user_agent, token=token)
        # book_id -> raw audiobook dict (with chapters), refreshed on each
        # browse of a section containing audio_books blocks
        self._book_cache: dict[str, dict] = {}
        # podcast owner_id -> raw show dict, refreshed on each browse of a
        # section containing podcasts blocks
        self._podcast_cache: dict[str, dict] = {}
        # podcast episode item_id -> (fresh signed stream url, fetched_at)
        # (audio.getById does not serve podcast episodes on this surface)
        self._episode_cache: dict[str, tuple[str, float, str]] = {}
        # chapter item_id -> (stream url, duration, title, fetched_at);
        # urls are signed and expire, so get_stream_details refreshes stale
        # entries before playback
        self._chapter_cache: dict[str, tuple[str, int, str, float]] = {}
        try:
            valid = await self._service.is_token_valid_async()
        except Exception as err:
            raise SetupFailedError(
                f"Could not validate VK credentials ({type(err).__name__})"
            ) from err
        if not valid:
            raise LoginFailed("VK access token is invalid or expired")

    def _mapping(self, item_id: str) -> set[ProviderMapping]:
        return {
            ProviderMapping(
                item_id=item_id,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
            )
        }

    async def _request_data(self, request) -> dict:
        """Execute a prepared VK API request and return its raw data dict."""
        self._service.fill_user_agent_and_token(request)
        self.logger.debug(
            "VK call %s %s entering", request.method, request.url
        )
        _t0 = time.time()
        response = await make_request_async(request)
        self.logger.debug(
            "VK call %s %s done in %.2fs", request.method, request.url, time.time() - _t0
        )
        data = response.data
        return data if isinstance(data, dict) else {}
