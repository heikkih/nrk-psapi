"""nrk-psapi."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
import socket
from typing import TYPE_CHECKING

import aiofiles
from aiohttp.client import ClientError, ClientResponse, ClientResponseError, ClientSession
from aiohttp.hdrs import METH_GET, METH_POST, METH_PUT
import async_timeout
import orjson
import platformdirs
from yarl import URL

from .auth import NrkAuthClient
from .caching import cache, disable_cache, set_cache_dir
from .const import LOGGER as _LOGGER, NRK_RADIO_INTERACTION_BASE_URL, PSAPI_BASE_URL
from .exceptions import (
    NrkPsApiConnectionError,
    NrkPsApiConnectionTimeoutError,
    NrkPsApiError,
    NrkPsApiNotFoundError,
    NrkPsApiRateLimitError,
    NrkPsAuthorizationError,
)
from .models.catalog import (
    Episode,
    Podcast,
    PodcastSequential,
    PodcastStandard,
    PodcastUmbrella,
    Program,
    Season,
    SeriesType,
)
from .models.channels import Channel
from .models.common import FetchedFileInfo, IpCheck, SortOrder
from .models.interaction import RadioMessage
from .models.metadata import PodcastMetadata
from .models.pages import (
    Curated,
    CuratedPodcast,
    CuratedSection,
    Included,
    IncludedSection,
    Page,
    Pages,
    PodcastPlug,
)
from .models.playback import PodcastManifest
from .models.recommendations import Recommendation, RecommendationContext
from .models.search import (
    CategoriesResponse,
    SearchResponse,
    SearchResultStrType,
    SearchResultType,
    SeriesListItem,
    SingleLetter,
)
from .models.userdata import (
    ContinuationsResponse,
    FavouriteLevel,
    FavouriteType,
    ProgressContentType,
    QueueContentType,
    QueuePositionWhere,
    QueueResponse,
    UpNextContentType,
    UpNextContext,
    UpNextResponse,
    UserFavourite,
    UserFavouriteNewEpisodesCountResponse,
    UserFavouritesResponse,
    UserProgressResponse,
)
from .utils import (
    fetch_file_info,
    get_nested_items,
    tiled_images,
)
from .version import __version__

if TYPE_CHECKING:
    from os import PathLike


@dataclass
class NrkPodcastAPI:
    auth_client: NrkAuthClient = field(default_factory=NrkAuthClient)
    """auth_client (NrkAuthClient): The authentication client."""
    disable_credentials_storage: bool = False
    """Whether to disable credential storage."""
    user_agent: str | None = None
    """User agent string."""
    enable_cache: bool = True
    """Enable caching, defaults to True."""
    cache_directory: str | None = None
    """Cache directory, defaults to (in order):

    1. Value of environment variable `NRK_PSAPI_CACHE_DIR`
    2. `~/.cache/nrk-psapi`
    """
    request_timeout: int = 15
    """Request timeout in seconds, defaults to 15."""
    session: ClientSession | None = None
    """Optional web session to use for requests."""

    _conf_dir = platformdirs.user_config_dir(__package__, ensure_exists=True)
    _close_session: bool = False

    def __post_init__(self):
        if not self.enable_cache:
            disable_cache()
            _LOGGER.warning("Cache disabled")

        if self.cache_directory is not None:
            set_cache_dir(self.cache_directory)

    def _resolve_credentials_path(self, filename: PathLike | None) -> Path:
        if filename is None:
            filename = Path(self._conf_dir) / "credentials.json"
        return Path(filename).resolve()

    async def save_credentials(self, filename: PathLike | None = None) -> None:
        """Save the current authentication credentials to a file.

        Args:
            filename (PathLike | None): The file to save the credentials to.
                If None, uses the default location.

        """
        filename = self._resolve_credentials_path(filename)
        credentials = self.auth_client.get_credentials()
        if credentials is None:  # pragma: no cover
            _LOGGER.warning("Tried to save non-existing credentials")
            return
        async with aiofiles.open(filename, "wb") as f:
            await f.write(orjson.dumps(credentials))

    async def load_credentials(self, filename: PathLike | None = None) -> None:
        """Load authentication credentials from a file.

        Args:
            filename (PathLike | None): The file to load the credentials from.
                If None, uses the default location.

        """
        filename = self._resolve_credentials_path(filename)
        if not await asyncio.to_thread(filename.exists):
            _LOGGER.warning("Credentials file does not exist: <%s>", filename)
            return
        async with aiofiles.open(filename) as f:
            data = await f.read()
            if data:
                self.auth_client.set_credentials(data)

    @property
    def request_header(self) -> dict[str, str]:
        """Generate a header for HTTP requests to the server."""
        return {
            "Accept": "application/json;api-version=3.5",
            "User-Agent": self.user_agent or f"NrkPodcastAPI/{__version__}",
        }

    @staticmethod
    def _uri_requires_auth(uri: str) -> bool:
        """Return True when the endpoint requires authenticated user context.

        Currently this is primarily userdata endpoints, except explicitly anonymous
        variants.
        """
        path = uri.lower().strip()
        return "/userdata/" in path and "/anonymous/" not in path

    async def _build_request_headers(
        self,
        uri: str,
        headers: dict[str, str] | None,
        authenticated: bool | None,
    ) -> dict[str, str]:
        """Build request headers and inject bearer auth when needed."""
        request_headers = self.request_header if headers is None else dict(headers)

        if authenticated is None:
            authenticated = self._uri_requires_auth(uri)

        if authenticated:
            has_auth_header = any(key.lower() == "authorization" for key in request_headers)
            if not has_auth_header:
                access_token = await self.auth_client.async_get_access_token_refreshed()
                request_headers["authorization"] = f"Bearer {access_token}"

        return request_headers

    async def _request_paged_all(
        self,
        uri: str,
        method: str = METH_GET,
        items_key: str | None = None,
        page_size: int | None = None,
        **kwargs,
    ) -> list:
        """Make a paged request."""
        results = []
        page = 1

        while True:
            data = await self._request_paged(uri, method, page_size=page_size, page=page, **kwargs)

            items = get_nested_items(data, items_key)
            results.extend(items)

            if "_links" in data and "next" in data["_links"]:
                page += 1
            else:
                break

        return results

    async def _request_paged(
        self,
        uri: str,
        method: str = METH_GET,
        page_size: int | None = None,
        page: int | None = None,
        **kwargs,
    ):
        """Make a paged request."""
        if page_size is None:
            page_size = 50
        if page is None:
            page = 1
        return await self._request(uri, method, params={"pageSize": page_size, "page": page}, **kwargs)

    @staticmethod
    async def _request_check_status(response: ClientResponse):
        if response.status == HTTPStatus.TOO_MANY_REQUESTS:
            raise NrkPsApiRateLimitError("Too many requests to NRK API. Try again later.")
        if response.status == HTTPStatus.NOT_FOUND:
            raise NrkPsApiNotFoundError("Resource not found")
        if response.status == HTTPStatus.BAD_REQUEST:
            raise NrkPsApiError("Bad request syntax or unsupported method")
        if response.status == HTTPStatus.FORBIDDEN:
            raise NrkPsAuthorizationError("Authorization failed")
        if not HTTPStatus(response.status).is_success:
            raise NrkPsApiError(response)

    async def _request(
        self,
        uri: str,
        method: str = METH_GET,
        base_url: str | None = None,
        **kwargs,
    ) -> str | dict[any, any] | list[any] | None:
        """Make a request."""
        if base_url is None:
            base_url = PSAPI_BASE_URL
        url = URL(base_url).join(URL(uri))
        headers = await self._build_request_headers(
            uri=uri,
            headers=kwargs.get("headers"),
            authenticated=kwargs.pop("authenticated", None),
        )

        params = kwargs.get("params")
        if params is not None:
            kwargs.update(params={k: v for k, v in params.items() if v is not None})

        if self.session is None:
            self.session = ClientSession()
            _LOGGER.debug("New session created.")
            self._close_session = True

        _LOGGER.debug(
            "Executing %s API request to %s.",
            method,
            url.with_query(kwargs.get("params")),
        )

        try:
            async with async_timeout.timeout(self.request_timeout):
                response = await self.session.request(
                    method,
                    url,
                    **kwargs,
                    headers=headers,
                    raise_for_status=self._request_check_status,
                )
        except asyncio.TimeoutError as exception:
            raise NrkPsApiConnectionTimeoutError(
                "Timeout occurred while connecting to NRK API"
            ) from exception
        except (
            ClientError,
            ClientResponseError,
            socket.gaierror,
        ) as exception:
            msg = f"Error occurred while communicating with NRK API: {exception}"
            raise NrkPsApiConnectionError(msg) from exception

        if response.status in [HTTPStatus.NO_CONTENT, HTTPStatus.ACCEPTED]:
            return None
        content_type = response.headers.get("Content-Type", "")
        text = await response.text()
        if "application/json" not in content_type:
            msg = "Unexpected response from the NRK API"
            raise NrkPsApiError(
                msg,
                {"Content-Type": content_type, "response": text},
            )
        return orjson.loads(text)

    async def ipcheck(self) -> IpCheck:
        """Check if IP is blocked."""
        result = await self._request("ipcheck")
        return IpCheck.from_dict(result["data"])

    async def send_message(
        self,
        podcast_id: str,
        message: str,
        *,
        anonymous: bool = False,
        phone: str | None = None,
    ) -> None:
        """Send a message to a podcast.

        Args:
            podcast_id(str): Id of the podcast
            message(str): Message to send
            anonymous(bool, optional): Anonymous. Defaults to False.
            phone(str, optional): Phone number. Defaults to None.

        """
        payload = RadioMessage(
            anonymous=anonymous,
            message=message,
            phone=phone,
            accept_terms=True,
        )
        await self._request(
            f"submit/{podcast_id}",
            method=METH_POST,
            base_url=NRK_RADIO_INTERACTION_BASE_URL,
            json=payload.to_dict(),
        )

    @cache(ignore=(0,))
    async def get_playback_manifest(
        self,
        item_id: str,
        *,
        podcast=False,
        program=False,
        channel=False,
    ) -> PodcastManifest:
        """Get the manifest for an episode/program/channel.

        Args:
            item_id(str): Media id
            channel(bool, optional): Media is a channel. Defaults to False.
            program(bool, optional): Media is a program. Defaults to False.
            podcast(bool, optional): Media is a podcast. Defaults to False.

        """
        if podcast:
            endpoint = "/podcast"
        elif program:
            endpoint = "/program"
        elif channel:
            endpoint = "/channel"
        else:
            endpoint = ""
        result = await self._request(f"playback/manifest{endpoint}/{item_id}")
        return PodcastManifest.from_dict(result)

    @cache(ignore=(0,))
    async def get_playback_metadata(
        self,
        item_id: str,
        *,
        podcast=False,
        program=False,
        channel=False,
    ) -> PodcastMetadata:
        """Get the metadata for an episode/program/channel.

        Args:
            item_id(str, optional): Media id
            channel(bool, optional): Media is a channel. Defaults to False.
            program(bool, optional): Media is a program. Defaults to False.
            podcast(bool, optional): Media is a podcast. Defaults to False.

        """
        if podcast:
            endpoint = "/podcast"
        elif program:
            endpoint = "/program"
        elif channel:
            endpoint = "/channel"
        else:
            endpoint = ""
        result = await self._request(f"playback/metadata{endpoint}/{item_id}")
        return PodcastMetadata.from_dict(result)

    @cache(ignore=(0,))
    async def get_episode(self, podcast_id: str, episode_id: str) -> Episode:
        """Get episode.

        Args:
            podcast_id(str): Podcast ID.
            episode_id(str): Episode ID.

        """
        result = await self._request(f"radio/catalog/podcast/{podcast_id}/episodes/{episode_id}")
        return Episode.from_dict(result)

    @cache(ignore=(0,))
    async def get_series_type(self, series_id: str) -> SeriesType:
        """Get series type.

        Args:
            series_id(str): Series ID.

        """
        result = await self._request(f"radio/catalog/series/{series_id}/type")
        return SeriesType.from_str(result["seriesType"])

    @cache(ignore=(0,))
    async def get_podcast_type(self, podcast_id: str) -> SeriesType:
        """Get podcast type.

        Args:
            podcast_id(str): Podcast ID.

        """
        result = await self._request(f"radio/catalog/podcast/{podcast_id}/type")
        return SeriesType.from_str(result["seriesType"])

    @cache(ignore=(0,))
    async def get_series_season(self, series_id: str, season_id: str) -> Season:
        """Get series season.

        Args:
            series_id(str): Series ID.
            season_id(str): Season ID.

        """
        result = await self._request(f"radio/catalog/series/{series_id}/seasons/{season_id}")
        return Season.from_dict(result)

    @cache(ignore=(0,))
    async def get_series_episodes(
        self,
        series_id: str,
        season_id: str | None = None,
        *,
        page_size: int | None = None,
        page: int = 1,
    ) -> list[Episode]:
        """Get series episodes.

        Args:
            series_id(str): Series ID.
            season_id(str, optional): Season ID.
            page_size(int, optional): Number of episodes to return per page (defaults to 50)
            page(int, optional): Page number, set to -1 for all (defaults to 1)

        """
        if season_id is not None:
            uri = f"radio/catalog/series/{series_id}/seasons/{season_id}/episodes"
        else:
            uri = f"radio/catalog/series/{series_id}/episodes"

        if page == -1:
            result = await self._request_paged_all(
                uri,
                page_size=page_size,
                items_key="_embedded.episodes",
            )
        else:
            results = await self._request_paged(
                uri,
                page_size=page_size,
                page=page,
            )
            result = results.get("_embedded", {}).get("episodes", [])

        return [Episode.from_dict(e) for e in result]

    @cache(ignore=(0,))
    async def get_live_channel(self, channel_id: str) -> Channel:
        """Get live channel.

        Args:
            channel_id(str): Channel ID.

        """
        result = await self._request(f"radio/channels/livebuffer/{channel_id}")
        return Channel.from_dict(result["channel"])

    @cache(ignore=(0,))
    async def get_program(self, program_id: str) -> Program:
        """Get program.

        Args:
            program_id(str): Program ID.

        """
        result = await self._request(f"radio/catalog/programs/{program_id}")
        return Program.from_dict(result)

    @cache(ignore=(0,))
    async def get_podcast(
        self, podcast_id: str
    ) -> Podcast | PodcastStandard | PodcastUmbrella | PodcastSequential:
        """Get podcast.

        Args:
            podcast_id(str): Podcast ID.

        """
        result = await self._request(f"radio/catalog/podcast/{podcast_id}")
        return Podcast.from_dict(result)

    # @cache(ignore=(0,))
    async def get_podcasts(self, podcast_ids: list[str]) -> list[Podcast]:
        """Get podcasts.

        Args:
            podcast_ids(list[str]): List of podcast ids.

        """
        results = await asyncio.gather(*[self.get_podcast(podcast_id) for podcast_id in podcast_ids])
        return list(results)

    @cache(ignore=(0,))
    async def get_podcast_season(self, podcast_id: str, season_id: str) -> Season:
        """Get podcast season.

        Args:
            podcast_id(str): Podcast ID
            season_id(str): Season ID

        """
        result = await self._request(f"radio/catalog/podcast/{podcast_id}/seasons/{season_id}")
        return Season.from_dict(result)

    @cache(ignore=(0,))
    async def get_podcast_episodes(
        self,
        podcast_id: str,
        season_id: str | None = None,
        *,
        page_size: int | None = None,
        page: int | None = None,
    ) -> list[Episode]:
        """Get podcast episodes.

        Args:
            podcast_id(str): Podcast ID
            season_id(str, optional): Season ID
            page_size(int, optional): Number of episodes to return per page (defaults to 50)
            page(int, optional): Page number, set to -1 for all (defaults to 1)

        """
        if season_id is not None:
            uri = f"radio/catalog/podcast/{podcast_id}/seasons/{season_id}/episodes"
        else:
            uri = f"radio/catalog/podcast/{podcast_id}/episodes"

        if page == -1:
            result = await self._request_paged_all(
                uri,
                page_size=page_size,
                items_key="_embedded.episodes",
            )
        else:
            results = await self._request_paged(
                uri,
                page_size=page_size,
                page=page,
            )
            result = results.get("_embedded", {}).get("episodes", [])

        return [Episode.from_dict(e) for e in result]

    @cache(ignore=(0,))
    async def get_all_podcasts(self) -> list[SeriesListItem]:
        """Get all podcasts."""
        result = await self._request(
            "radio/search/categories/podcast",
            params={
                "take": 1000,
            },
        )
        return [SeriesListItem.from_dict(s) for s in result["series"]]

    @cache(ignore=(0,))
    async def get_series(self, series_id: str) -> Podcast:
        """Get series.

        Args:
            series_id(str): Series ID.

        """
        result = await self._request(f"radio/catalog/series/{series_id}")
        return Podcast.from_dict(result)

    @cache(ignore=(0,))
    async def get_recommendations(
        self,
        item_id: str,
        context_id: RecommendationContext | None = None,
        limit: int | None = None,
    ) -> Recommendation:
        """Get recommendations.

        Args:
            item_id(str): A id of a series/program/episode/season etc.
            context_id(RecommendationContext, optional): Which context (front page, series page, etc.) the user is in.
            limit(int, optional): Number of recommendations returned (max 25). Defaults to 12.

        """

        result = await self._request(
            f"radio/recommendations/{item_id}",
            params={
                "list": context_id,
                "maxNumber": limit,
            },
        )
        return Recommendation.from_dict(result)

    async def count_new_favourited_episodes(
        self,
        favourite_level: FavouriteLevel | None = None,
        since: datetime | None = None,
    ):
        """Count new episodes."""
        if favourite_level is None:
            favourite_level = FavouriteLevel.MANUAL_FAVOURITES
        if since is None:
            since = datetime.now(tz=timezone.utc) - timedelta(days=30)
        user_id = await self.auth_client.get_user_id()
        result = await self._request(
            f"radio/userdata/{user_id}/newepisodes/count",
            params={
                "favouriteLevel": favourite_level,
                "since": since.isoformat().replace("+00:00", "Z"),
            },
        )
        return UserFavouriteNewEpisodesCountResponse.from_dict(result)

    # favouriteType=any&key=17097876000000000!-20&sortOrder=descending&pageSize=20
    async def get_user_favorites(
        self,
        manual_only: bool = False,
        sort_order: SortOrder = SortOrder.DESCENDING,
        key: str | None = None,
        page_size: int | None = None,
    ):
        """Get user favorites."""
        if key is None:
            key = datetime.now(tz=timezone.utc).timestamp()
        user_id = await self.auth_client.get_user_id()
        result = await self._request(
            f"radio/userdata/{user_id}/favourites",
            params={
                "sortOrder": sort_order,
                "pageSize": page_size,
                "key": key,
                "favouriteType": "manual" if manual_only else "any",
            },
        )
        return UserFavouritesResponse.from_dict(result)

    async def add_user_favourite(self, item_type: FavouriteType, item_id: str):
        """Add user favourite."""
        user_id = await self.auth_client.get_user_id()
        result = await self._request(
            f"radio/userdata/{user_id}/favourites/{item_type}/{item_id}",
            method=METH_PUT,
            json={
                "when": None,
            },
        )
        return UserFavourite.from_dict(result)

    async def get_progress(
        self,
        content_id: str,
        content_type: ProgressContentType = ProgressContentType.PODCASTEPISODE,
        *,
        source_medium: str = "radio",
    ) -> UserProgressResponse:
        """Get progress and reporting interval for a content item."""
        user_id = await self.auth_client.get_user_id()
        result = await self._request(
            f"{source_medium}/userdata/{user_id}/progress/{content_type}/{content_id}"
        )
        return UserProgressResponse.from_dict(result)

    async def save_progress(
        self,
        content_id: str,
        position: str,
        start_playback_position: str,
        content_type: ProgressContentType = ProgressContentType.PODCASTEPISODE,
        *,
        when: datetime | None = None,
        source_medium: str = "radio",
    ) -> None:
        """Save progress for a content item."""
        user_id = await self.auth_client.get_user_id()
        await self._request(
            f"{source_medium}/userdata/{user_id}/progress/{content_type}/{content_id}",
            method=METH_PUT,
            json={
                "position": position,
                "startPlaybackPosition": start_playback_position,
                "when": when.isoformat().replace("+00:00", "Z") if when else None,
            },
        )

    async def get_upnext(
        self,
        content_id: str,
        content_type: UpNextContentType = UpNextContentType.PODCASTEPISODE,
        *,
        context: UpNextContext | None = None,
        favourite_level: FavouriteLevel | None = None,
    ) -> UpNextResponse:
        """Get personalized up-next recommendation for a content item."""
        user_id = await self.auth_client.get_user_id()
        result = await self._request(
            f"radio/userdata/{user_id}/upnext/{content_type}/{content_id}",
            params={
                "context": context,
                "favouriteLevel": favourite_level,
            },
        )
        return UpNextResponse.from_dict(result)

    async def get_queue(self) -> QueueResponse:
        """Get the user's radio queue."""
        user_id = await self.auth_client.get_user_id()
        result = await self._request(f"radio/userdata/{user_id}/queue")
        return QueueResponse.from_dict(result)

    async def add_to_queue(
        self,
        content_id: str,
        content_type: QueueContentType,
        *,
        where: QueuePositionWhere = QueuePositionWhere.LAST,
        after_id: str | None = None,
        after_type: QueueContentType | None = None,
    ) -> QueueResponse:
        """Add or move an element in the user's queue."""
        user_id = await self.auth_client.get_user_id()
        position: dict[str, any] = {"where": str(where)}
        if where == QueuePositionWhere.AFTER:
            if after_id is None or after_type is None:
                raise ValueError("after_id and after_type are required when where='after'")
            position["after"] = {
                "id": after_id,
                "type": str(after_type),
            }

        result = await self._request(
            f"radio/userdata/{user_id}/queue/add",
            method=METH_PUT,
            json={
                "id": content_id,
                "type": str(content_type),
                "position": position,
            },
        )
        return QueueResponse.from_dict(result)

    async def delete_from_queue(self, content_id: str, content_type: QueueContentType) -> QueueResponse:
        """Delete an element from the user's queue."""
        user_id = await self.auth_client.get_user_id()
        result = await self._request(
            f"radio/userdata/{user_id}/queue/delete",
            method=METH_PUT,
            json={
                "id": content_id,
                "type": str(content_type),
            },
        )
        return QueueResponse.from_dict(result)

    async def sync_queue(self, operations: list[dict[str, any]]) -> QueueResponse:
        """Sync queue operations performed offline."""
        user_id = await self.auth_client.get_user_id()
        result = await self._request(
            f"radio/userdata/{user_id}/queue/sync",
            method=METH_PUT,
            json=operations,
        )
        return QueueResponse.from_dict(result)

    async def list_continuations(
        self, key: str | None = None, page_size: int | None = None
    ) -> ContinuationsResponse:
        """List continuation items for the user."""
        user_id = await self.auth_client.get_user_id()
        result = await self._request(
            f"radio/userdata/{user_id}/continuations",
            params={
                "key": key,
                "pageSize": page_size,
            },
        )
        return ContinuationsResponse.from_dict(result)

    async def delete_continuation(self, continuation_id: str) -> None:
        """Delete a continuation item by internal continuation id."""
        user_id = await self.auth_client.get_user_id()
        await self._request(
            f"radio/userdata/{user_id}/continuations/{continuation_id}",
            method="DELETE",
        )

    @cache(ignore=(0,))
    async def browse(
        self,
        letter: SingleLetter | str | None = None,
        category: str | None = None,
        per_page: int = 50,
        page: int = 1,
    ) -> CategoriesResponse:
        """Browse all series, podcast and umbrella seasons, optionally filtered by category.

        Alphabetical listing of all series, podcasts and umbrella seasons that are not excluded from search
        results in given category. Categories correspond to those in from :meth:`~.radio_pages`.
        For the category 'podcast', all podcasts are listed, also those excluded from search results.

        Args:
            category(str, optional): Category. Defaults to None, which will list all.
            letter(SingleLetter, optional): A single letter.
            per_page(int, optional): Number of items per page. Defaults to 50.
            page(int, optional): Page number. Defaults to 1.

        """

        if category is None:
            category = "alt-innhold"

        result = await self._request(
            f"radio/search/categories/{category}",
            params={
                "letter": letter,
                "take": per_page,
                "skip": (page - 1) * per_page,
            },
        )
        return CategoriesResponse.from_dict(result)

    async def search(
        self,
        query: str,
        per_page: int = 50,
        page: int = 1,
        search_type: SearchResultType | SearchResultStrType | None = None,
    ) -> SearchResponse:
        """Search anything.

        Args:
            query(str): Search query.
            per_page(int, optional): Number of items per page. Defaults to 50.
            page(int, optional): Page number. Defaults to 1.
            search_type(SearchResultType, optional): Search type, one of :class:`~.models.search.SearchResultType`. Defaults to all.

        """
        result = await self._request(
            "radio/search/search",
            params={
                "q": query,
                "take": per_page,
                "skip": (page - 1) * per_page,
                "page": page,
                "type": str(search_type) if search_type else None,
            },
        )
        return SearchResponse.from_dict(result)

    async def search_suggest(self, query: str) -> list[str]:
        """Search autocomplete/auto-suggest.

        Args:
            query(str): Search query

        """
        return await self._request("/radio/search/search/suggest", params={"q": query})

    @cache(ignore=(0,))
    async def radio_pages(self) -> Pages:
        """Get radio pages."""
        result = await self._request("radio/pages")
        return Pages.from_dict(result)

    @cache(ignore=(0,))
    async def radio_page(self, page_id: str, section_id: str | None = None) -> Page | Included | None:
        """Get radio page.

        Args:
            page_id(str): Name of the page, e.g. 'discover'.
            section_id(str, optional): Web friendly title of the section, e.g. 'krim-fra-virkeligheten'.

        """
        result = await self._request(f"radio/pages/{page_id}")
        page = Page.from_dict(result)
        if section_id is None:
            return page

        for section in page.sections:
            if isinstance(section, IncludedSection) and section.id == section_id:
                return section.included
        return None

    @cache(ignore=(0,))
    async def curated_podcasts(self) -> Curated:
        """Get curated podcasts.
        This is a wrapper around :meth:`~NrkPodcastAPI.radio_page`, with the section_id set to "podcast" and
        some logic to make it easier to use for accessing curated podcasts.

        """
        page = await self.radio_page(page_id="podcast")
        sections = []
        for section in page.sections:
            if isinstance(section, IncludedSection):
                podcasts = [
                    CuratedPodcast(
                        id=plug.id,
                        title=plug.title,
                        subtitle=plug.tagline,
                        image=plug.podcast.image_url,
                        number_of_episodes=plug.podcast.number_of_episodes,
                    )
                    for plug in section.included.plugs
                    if isinstance(plug, PodcastPlug)
                ]
                if len(podcasts) > 1:
                    sections.append(
                        CuratedSection(
                            id=section.id,
                            title=section.included.title,
                            podcasts=podcasts,
                        )
                    )
        return Curated(sections=sections)

    @cache(ignore=(0,))
    async def fetch_file_info(self, url: URL | str) -> FetchedFileInfo:
        """Proxies call to :func:`.utils.fetch_file_info`, passing on :attr:`~.NrkPodcastAPI.session`."""
        return await fetch_file_info(url, self.session)

    @cache(ignore=(0,))
    async def generate_tiled_images(
        self,
        image_urls: list[str],
        tile_size: int = 100,
        columns: int = 3,
        aspect_ratio: str | None = None,
    ) -> bytes:  # pragma: no cover
        """Proxies call to :func:`.utils.tiled_images`, passing on :attr:`~.session`."""
        return await tiled_images(image_urls, tile_size, columns, aspect_ratio, session=self.session)

    async def close(self) -> None:
        """Close open client session."""
        if self.session and self._close_session:
            await self.session.close()
        if not self.disable_credentials_storage:
            await self.save_credentials()

    async def __aenter__(self):
        """Async enter."""
        if not self.disable_credentials_storage:
            await self.load_credentials()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        """Async exit."""
        await self.close()
