"""Tests for NrkPodcastAPI."""

from __future__ import annotations

import asyncio
import logging
import socket
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp.web_response import json_response
from aresponses import ResponsesMockServer
import pytest
from yarl import URL

from nrk_psapi import NrkPodcastAPI
from nrk_psapi.const import NRK_RADIO_INTERACTION_BASE_URL, PSAPI_BASE_URL
from nrk_psapi.exceptions import (
    NrkPsApiAuthenticationError,
    NrkPsApiConnectionError,
    NrkPsApiConnectionTimeoutError,
    NrkPsApiError,
    NrkPsApiGeoBlockedError,
    NrkPsApiNotFoundError,
    NrkPsApiRateLimitError,
)
from nrk_psapi.models import (
    CategoriesResponse,
    Channel,
    ChannelPlug,
    ContinuationsResponse,
    Curated,
    CuratedSection,
    Episode,
    EpisodePlug,
    FavouriteType,
    Included,
    IncludedSection,
    IpCheck,
    LinkPlug,
    Manifest,
    Page,
    PageListItem,
    PagePlug,
    Pages,
    Plug,
    Podcast,
    PodcastEpisodeMetadata,
    PodcastEpisodePlug,
    PodcastManifest,
    PodcastMetadata,
    PodcastMetadataEmbedded,
    PodcastPlug,
    PodcastSeasonPlug,
    PodcastSeries,
    PodcastStandard,
    PodcastType,
    PodcastUmbrella,
    Program,
    ProgressContentType,
    QueueContentType,
    QueuePositionWhere,
    QueueResponse,
    Recommendation,
    SearchResponse,
    Season,
    Section,
    SeriesListItem,
    SeriesPlug,
    SeriesType,
    StandaloneProgramPlug,
    UpNextContentType,
    UpNextContext,
    UpNextResponse,
    UserFavourite,
    UserFavouriteNewEpisodesCountResponse,
    UserFavouritesResponse,
    UserProgressResponse,
)

from .helpers import CustomRoute, load_fixture_json, setup_auth_mocks

logger = logging.getLogger(__name__)


class _FakeResponse:
    status = 200
    headers = {"Content-Type": "application/json"}

    async def text(self):
        return '{"ok": true}'


class _FakeSession:
    def __init__(self):
        self.last_headers = {}

    async def request(self, _method, _url, **kwargs):
        self.last_headers = kwargs.get("headers", {})
        return _FakeResponse()


async def test_ipcheck(aresponses: ResponsesMockServer):
    fixture_name = "ipcheck"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        "/ipcheck",
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.ipcheck()
        assert isinstance(result, IpCheck)
        assert result.country_code == fixture["data"]["countryCode"]


async def test_check_availability_passes_for_norwegian_ip(aresponses: ResponsesMockServer):
    """check_availability() should return IpCheck when the IP has NRK access."""
    fixture = load_fixture_json("ipcheck")
    aresponses.add(URL(PSAPI_BASE_URL).host, "/ipcheck", "GET", json_response(data=fixture))
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.check_availability()
        assert isinstance(result, IpCheck)
        assert result.is_ip_norwegian is True


async def test_check_availability_raises_for_world_ip(aresponses: ResponsesMockServer):
    """check_availability() should raise NrkPsApiGeoBlockedError for non-EEA IPs."""
    blocked_fixture = {
        "_links": {"self": {"href": "/ipcheck"}},
        "data": {
            "clientIpAddress": "1.2.3.4",
            "countryCode": "US",
            "isIpNorwegian": False,
            "lookupSource": "MaxMind",
            "proxyType": "",
            "accessGroup": "WORLD",
        },
    }
    aresponses.add(URL(PSAPI_BASE_URL).host, "/ipcheck", "GET", json_response(data=blocked_fixture))
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        with pytest.raises(NrkPsApiGeoBlockedError, match="US"):
            await nrk_api.check_availability()


async def test_get_playback_manifest_raises_for_geoblocked_content(aresponses: ResponsesMockServer):
    """get_playback_manifest() should raise NrkPsApiGeoBlockedError for geo-blocked content."""
    item_id = "l_9a443e59-5c18-45d8-843e-595c18b5d849"
    blocked_manifest = {
        "_links": {
            "self": {"href": f"/playback/manifest/podcast/{item_id}"},
            "metadata": {"href": f"/playback/metadata/podcast/{item_id}"},
        },
        "id": item_id,
        "playability": "nonPlayable",
        "streamingMode": "onDemand",
        "availability": {
            "information": "Not available outside Norway",
            "isGeoBlocked": True,
            "externalEmbeddingAllowed": False,
        },
        "statistics": {"snowplow": {"source": "podcast"}},
        "playable": None,
        "nonPlayable": {"reason": "geoBlock"},
        "sourceMedium": "audio",
        "displayAspectRatio": None,
    }
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/playback/manifest/podcast/{item_id}",
        "GET",
        json_response(data=blocked_manifest),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        with pytest.raises(NrkPsApiGeoBlockedError, match=item_id):
            await nrk_api.get_playback_manifest(item_id, podcast=True)


async def test_request_injects_bearer_for_userdata_requests():
    """Userdata endpoints should include bearer auth automatically."""
    session = _FakeSession()
    nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
    nrk_api.auth_client.async_get_access_token = AsyncMock(return_value="token-123")

    await nrk_api._request("radio/userdata/some-user/favourites")

    assert session.last_headers.get("authorization") == "Bearer token-123"
    nrk_api.auth_client.async_get_access_token.assert_awaited_once()


async def test_request_skips_bearer_for_anonymous_userdata_requests():
    """Anonymous userdata endpoints should not include bearer auth."""
    session = _FakeSession()
    nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
    nrk_api.auth_client.async_get_access_token = AsyncMock(return_value="token-123")

    await nrk_api._request("radio/userdata/anonymous/upnext/programs/ABC12345678")

    assert "authorization" not in {k.lower(): v for k, v in session.last_headers.items()}
    nrk_api.auth_client.async_get_access_token.assert_not_awaited()


@pytest.mark.parametrize(
    "series_id",
    [
        "hele-historien",
    ],
)
async def test_get_series(
    aresponses: ResponsesMockServer,
    series_id: str,
):
    fixture_name = f"radio_catalog_series_{series_id}"
    uri = f"/radio/catalog/series/{series_id}"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        uri,
        "GET",
        json_response(data=fixture),
    )

    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.get_series(series_id)
        assert fixture["_links"]["self"]["href"] == uri
        assert isinstance(result, Podcast)


@pytest.mark.parametrize(
    "podcast_id",
    [
        "hele_historien",
    ],
)
async def test_get_umbrella_podcast(
    aresponses: ResponsesMockServer,
    podcast_id: str,
):
    fixture_name = f"radio_catalog_podcast_{podcast_id}"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/catalog/podcast/{podcast_id}",
        "GET",
        json_response(data=fixture),
    )

    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.get_podcast(podcast_id)

        # assert uri == fixture_data["_links"]["self"]["href"]
        assert isinstance(result, PodcastUmbrella)
        # assert result.series.id == fixture_data["series"]["id"]
        assert result.series_type == SeriesType.UMBRELLA


@pytest.mark.parametrize(
    "podcast_id",
    [
        "tore_sagens_podkast",
    ],
)
async def test_get_standard_podcast(
    aresponses: ResponsesMockServer,
    podcast_id: str,
):
    fixture_name = f"radio_catalog_podcast_{podcast_id}"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/catalog/podcast/{podcast_id}",
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        results = await nrk_api.get_podcasts([podcast_id])
        result = results[0]
        # uri, fixture_data = mock_client_session[-1]
        # assert uri == fixture_data["_links"]["self"]["href"]
        assert isinstance(result, PodcastStandard)
        assert isinstance(result.series, PodcastSeries)
        assert all(isinstance(item, Episode) for item in result.episodes)
        # assert result.series.id == fixture_data["series"]["id"]
        assert result.series_type == SeriesType.STANDARD


@pytest.mark.parametrize(
    ("podcast_id", "season_id"),
    [
        ("tore_sagens_podkast", "2021"),
    ],
)
async def test_get_podcast_episodes(
    aresponses: ResponsesMockServer,
    podcast_id: str,
    season_id: str | None,
):
    page_size = 15
    uri = f"/radio/catalog/podcast/{podcast_id}/seasons/{season_id}/episodes"
    fixture_name = f"radio_catalog_podcast_{podcast_id}_seasons_{season_id}_episodes"
    aresponses.add(
        response=json_response(data=load_fixture_json(fixture_name)),
        route=CustomRoute(
            host_pattern=URL(PSAPI_BASE_URL).host,
            path_pattern=uri,
            path_qs={"pageSize": page_size, "page": 1},
            method_pattern="GET",
        ),
    )

    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.get_podcast_episodes(podcast_id, season_id, page_size=page_size, page=1)
        assert isinstance(result, list)
        assert len(result) > 0
        assert all(isinstance(item, Episode) for item in result)


@pytest.mark.parametrize(
    "podcast_id",
    [
        "tore_sagens_podkast",
    ],
)
async def test_get_all_podcast_episodes(
    aresponses: ResponsesMockServer,
    podcast_id: str,
):
    page_size = 15

    uri = f"/radio/catalog/podcast/{podcast_id}/episodes"
    for page_no in range(1, 3):
        fixture_name = f"radio_catalog_podcast_{podcast_id}_episodes_page{page_no}"
        aresponses.add(
            response=json_response(data=load_fixture_json(fixture_name)),
            route=CustomRoute(
                host_pattern=URL(PSAPI_BASE_URL).host,
                path_pattern=uri,
                path_qs={"pageSize": page_size, "page": page_no},
                method_pattern="GET",
            ),
        )

    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.get_podcast_episodes(podcast_id, page_size=page_size, page=-1)
        assert isinstance(result, list)
        assert len(result) > 0
        assert all(isinstance(item, Episode) for item in result)


@pytest.mark.parametrize(
    ("podcast_id", "season_id"),
    [
        ("hele_historien", "alene-i-atlanteren"),
    ],
)
async def test_get_podcast_season(
    aresponses: ResponsesMockServer,
    podcast_id: str,
    season_id: str,
):
    fixture_name = f"radio_catalog_podcast_{podcast_id}_seasons_{season_id}"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/catalog/podcast/{podcast_id}/seasons/{season_id}",
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.get_podcast_season(podcast_id, season_id)
        assert isinstance(result, Season)


async def test_get_all_podcasts(aresponses: ResponsesMockServer):
    fixture_name = "radio_search_categories_podcast"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        "/radio/search/categories/podcast",
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.get_all_podcasts()
        assert isinstance(result, list)
        assert len(result) > 0
        assert all(isinstance(item, SeriesListItem) for item in result)


@pytest.mark.parametrize(
    "series_id",
    [
        "hele-historien",
    ],
)
async def test_get_series_type(aresponses: ResponsesMockServer, series_id: str):
    fixture_name = f"radio_catalog_series_{series_id}_type"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/catalog/series/{series_id}/type",
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.get_series_type(series_id)
        assert isinstance(result, SeriesType)
        assert result == SeriesType.STANDARD
        assert str(result) == fixture["seriesType"]


@pytest.mark.parametrize(
    ("series_id", "season_id"),
    [
        ("karsten-og-petra-radio", "200511"),
    ],
)
async def test_get_series_season(
    aresponses: ResponsesMockServer,
    series_id: str,
    season_id: str,
):
    fixture_name = f"radio_catalog_series_{series_id}_seasons_{season_id}"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/catalog/series/{series_id}/seasons/{season_id}",
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.get_series_season(series_id, season_id)
        assert isinstance(result, Season)
        assert result.type == PodcastType.SERIES
        assert result.series_type == SeriesType.STANDARD


@pytest.mark.parametrize(
    ("series_id", "season_id"),
    [
        ("karsten-og-petra-radio", None),
        ("karsten-og-petra-radio", "200511"),
    ],
)
async def test_get_series_episodes(
    aresponses: ResponsesMockServer,
    series_id: str,
    season_id: str | None,
):
    if season_id is not None:
        uri = f"/radio/catalog/series/{series_id}/seasons/{season_id}/episodes"
        fixture_name = f"radio_catalog_series_{series_id}_seasons_{season_id}_episodes"
    else:
        uri = f"/radio/catalog/series/{series_id}/episodes"
        fixture_name = f"radio_catalog_series_{series_id}_episodes"

    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        uri,
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.get_series_episodes(series_id, season_id)
        assert isinstance(result, list)
        assert len(result) > 0
        assert all(isinstance(item, Episode) for item in result)


@pytest.mark.parametrize(
    ("series_id", "season_id"),
    [
        ("karsten-og-petra-radio", None),
        ("karsten-og-petra-radio", "200511"),
    ],
)
async def test_get_all_series_episodes(
    aresponses: ResponsesMockServer,
    series_id: str,
    season_id: str | None,
):
    if season_id is not None:
        uri = f"/radio/catalog/series/{series_id}/seasons/{season_id}/episodes"
        fixture_name = f"radio_catalog_series_{series_id}_seasons_{season_id}_episodes"
    else:
        uri = f"/radio/catalog/series/{series_id}/episodes"
        fixture_name = f"radio_catalog_series_{series_id}_episodes"

    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        uri,
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.get_series_episodes(series_id, season_id, page_size=20, page=-1)
        assert isinstance(result, list)
        assert len(result) > 0
        assert all(isinstance(item, Episode) for item in result)


@pytest.mark.parametrize(
    "podcast_id",
    [
        "hele_historien",
    ],
)
async def test_get_podcast_type(aresponses: ResponsesMockServer, podcast_id: str):
    fixture_name = f"radio_catalog_podcast_{podcast_id}_type"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/catalog/podcast/{podcast_id}/type",
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.get_podcast_type(podcast_id)
        assert isinstance(result, SeriesType)
        assert result == SeriesType.UMBRELLA
        assert str(result) == fixture["seriesType"]


@pytest.mark.parametrize(
    "item_id",
    [
        "l_81a66a37-853f-48c1-a66a-37853fa8c104",
    ],
)
async def test_get_recommendations(aresponses: ResponsesMockServer, item_id: str):
    fixture_name = f"radio_recommendations_{item_id}"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/recommendations/{item_id}",
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.get_recommendations(item_id)
        assert isinstance(result, Recommendation)


@pytest.mark.parametrize(("category", "letter"), [(None, "A"), ("kultur", None)])
async def test_browse(aresponses: ResponsesMockServer, category: str, letter: str):
    per_page = 10
    page = 1

    fixture_name = f"radio_search_categories_{category or 'alt-innhold'}"
    uri = f"/radio/search/categories/{category or 'alt-innhold'}"
    uri_qs = {
        "take": per_page,
        "skip": per_page * (page - 1),
    }
    if letter is not None:
        fixture_name += f"_{letter}"
        uri_qs["letter"] = letter

    aresponses.add(
        response=json_response(data=load_fixture_json(fixture_name)),
        route=CustomRoute(
            host_pattern=URL(PSAPI_BASE_URL).host,
            path_pattern=uri,
            path_qs=uri_qs,
            method_pattern="GET",
        ),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.browse(category=category, letter=letter, per_page=per_page, page=page)
        assert isinstance(result, CategoriesResponse)
        assert isinstance(result.series, list)
        assert all(isinstance(item, SeriesListItem) for item in result.series)


@pytest.mark.parametrize(
    "query",
    ["beyer"],
)
async def test_search(aresponses: ResponsesMockServer, query: str):
    fixture_name = f"radio_search_search_{query}"
    uri = "/radio/search/search"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        uri,
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.search(query=query, per_page=10)
        assert isinstance(result, SearchResponse)


@pytest.mark.parametrize(
    "query",
    ["bren"],
)
async def test_search_suggest(aresponses: ResponsesMockServer, query: str):
    fixture_name = f"radio_search_search_suggest_{query}"
    uri = "/radio/search/search/suggest"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        uri,
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.search_suggest(query)
        assert isinstance(result, list)
        assert len(result) > 0


@pytest.mark.parametrize(
    ("media_type", "media_id"),
    [
        ("podcast", "l_d3d4424e-e692-4ab8-9442-4ee6929ab82a"),
        ("channel", "p1"),
        ("program", "MDFP01003524"),
        (None, "l_d3d4424e-e692-4ab8-9442-4ee6929ab82a"),
    ],
)
async def test_get_metadata(aresponses: ResponsesMockServer, media_type: str | None, media_id: str):
    podcast, channel, program = (media_type == "podcast", media_type == "channel", media_type == "program")
    if media_type is None:
        media_type = "podcast"
    fixture_name = f"playback_metadata_{media_type}_{media_id}"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/playback/metadata/{media_id}",
        "GET",
        json_response(data=fixture),
    )
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/playback/metadata/{media_type}/{media_id}",
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.get_playback_metadata(
            media_id,
            podcast=podcast,
            channel=channel,
            program=program,
        )
        assert isinstance(result, PodcastMetadata)
        if media_type == "podcast":
            assert isinstance(result.podcast, PodcastMetadataEmbedded)
            assert isinstance(result.podcast_episode, PodcastEpisodeMetadata)
            assert result.podcast.titles.title == fixture["_embedded"]["podcast"]["titles"]["title"]
            assert result.podcast_episode.clip_id == fixture["_embedded"]["podcastEpisode"]["clipId"]
        assert isinstance(result.manifests, list)
        assert all(isinstance(item, Manifest) for item in result.manifests)


@pytest.mark.parametrize(
    ("media_type", "media_id"),
    [
        ("podcast", "l_9a443e59-5c18-45d8-843e-595c18b5d849"),
        ("channel", "p1"),
        ("program", "MDFP01003524"),
        (None, "l_9a443e59-5c18-45d8-843e-595c18b5d849"),
    ],
)
async def test_get_manifest(aresponses: ResponsesMockServer, media_type: str | None, media_id: str):
    podcast, channel, program = (media_type == "podcast", media_type == "channel", media_type == "program")
    if media_type is None:
        media_type = "podcast"
    fixture_name = f"playback_manifest_{media_type}_{media_id}"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/playback/manifest/{media_id}",
        "GET",
        json_response(data=fixture),
    )
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/playback/manifest/{media_type}/{media_id}",
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.get_playback_manifest(
            media_id,
            podcast=podcast,
            program=program,
            channel=channel,
        )
        assert isinstance(result, PodcastManifest)


@pytest.mark.parametrize(
    ("podcast_id", "episode_id"),
    [
        ("desken_brenner", "l_8c60be4d-ce0b-41d0-a0be-4dce0b81d01a"),
    ],
)
async def test_get_episode(
    aresponses: ResponsesMockServer,
    podcast_id: str,
    episode_id: str,
):
    fixture_name = f"radio_catalog_podcast_{podcast_id}_episodes_{episode_id}"
    uri = f"/radio/catalog/podcast/{podcast_id}/episodes/{episode_id}"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        uri,
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.get_episode(podcast_id, episode_id)
        assert uri == fixture["_links"]["self"]["href"]
        assert isinstance(result, Episode)


@pytest.mark.parametrize(
    "channel_id",
    [
        "p1",
    ],
)
async def test_get_live_channel(aresponses: ResponsesMockServer, channel_id: str):
    fixture_name = f"radio_channels_livebuffer_{channel_id}"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/channels/livebuffer/{channel_id}",
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.get_live_channel(channel_id)
        assert isinstance(result, Channel)


@pytest.mark.parametrize(
    "program_id",
    [
        "MKTT05000905",
    ],
)
async def test_get_program(aresponses: ResponsesMockServer, program_id: str):
    fixture_name = f"radio_catalog_programs_{program_id}"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/catalog/programs/{program_id}",
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.get_program(program_id)
        assert isinstance(result, Program)


async def test_curated_podcasts(aresponses: ResponsesMockServer):
    fixture_name = "radio_pages_podcast"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        "/radio/pages/podcast",
        "GET",
        json_response(data=fixture),
    )

    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.curated_podcasts()
        assert isinstance(result, Curated)
        assert isinstance(result.sections, list)
        assert len(result.sections) > 0
        assert all(isinstance(item, CuratedSection) for item in result.sections)
        section = result.get_section_by_id("70bef2b0-2b46-11ec-9a7f-45ebc1a764fe")
        assert isinstance(section, CuratedSection)
        section = result.get_section_by_id("non-existent")
        assert section is None


async def test_pages(aresponses: ResponsesMockServer):
    fixture_name = "radio_pages"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        "/radio/pages",
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.radio_pages()
        assert isinstance(result, Pages)
        assert all(isinstance(item, PageListItem) for item in result.pages)
        for page in result.pages:
            assert len(page.title) > 0


@pytest.mark.parametrize(
    ("page_id", "section_id"),
    [
        ("podcast", None),
        ("podcast", "8a135591-dd27-11ee-8f21-d12745ae0a69"),
        ("podcast", "nonexistent"),
    ],
)
async def test_podcast_page(
    aresponses: ResponsesMockServer,
    page_id: str,
    section_id: str | None,
):
    fixture_name = f"radio_pages_{page_id}"
    fixture = load_fixture_json(fixture_name)
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/pages/{page_id}",
        "GET",
        json_response(data=fixture),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        result = await nrk_api.radio_page(page_id, section_id)
        if section_id == "nonexistent":
            assert result is None
            return

        if section_id is not None:
            assert isinstance(result, Included)
            plugs = result.plugs
        else:
            assert isinstance(result, Page)
            assert isinstance(result.sections, list)
            assert all(isinstance(item, Section) for item in result.sections)
            included_sections = [item for item in result.sections if isinstance(item, IncludedSection)]
            plugs = [plug for section in included_sections for plug in section.included.plugs]
        assert all(isinstance(plug, Plug) for plug in plugs)
        for plug in plugs:
            assert len(plug.id) > 0
            if isinstance(plug, PagePlug):
                assert len(plug.page.page_id) > 0
            if isinstance(plug, PodcastPlug):
                assert len(plug.podcast.podcast_title) > 0
            if isinstance(plug, PodcastEpisodePlug):
                assert len(plug.podcast_episode.podcast_episode_title) > 0
            if isinstance(plug, PodcastSeasonPlug):
                assert len(plug.podcast_season.podcast_season_title) > 0
            if isinstance(plug, SeriesPlug):
                assert len(plug.series.series_title) > 0
            if isinstance(plug, EpisodePlug):
                assert len(plug.episode.episode_title) > 0
                assert len(plug.episode.program_id) > 0
            if isinstance(plug, LinkPlug):
                assert len(str(plug.link)) > 0
            if isinstance(plug, ChannelPlug):
                assert len(plug.channel.channel_title) > 0
            if isinstance(plug, StandaloneProgramPlug):
                assert len(plug.standalone_program.program_title) > 0


@pytest.mark.parametrize(
    ("expected_content_length", "expected_content_type"),
    [
        ("1234", "audio/mpeg"),
    ],
)
async def test_fetch_file_info(
    aresponses: ResponsesMockServer,
    expected_content_length: str,
    expected_content_type: str,
):
    url = URL(f"{PSAPI_BASE_URL}/file")

    aresponses.add(
        url.host,
        url.path,
        "HEAD",
        aresponses.Response(
            headers={
                "Content-Length": expected_content_length,
                "Content-Type": expected_content_type,
            },
        ),
    )
    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        file_info = await nrk_api.fetch_file_info(URL(url))

    assert file_info["content_length"] == int(expected_content_length)
    assert file_info["content_type"] == expected_content_type


@pytest.mark.parametrize(
    ("podcast_id", "phone"),
    [
        ("desken_brenner", "12345678"),
    ],
)
async def test_radio_interaction(aresponses: ResponsesMockServer, nrk_client, podcast_id: str, phone: str):
    """Test radio interaction."""
    aresponses.add(
        URL(NRK_RADIO_INTERACTION_BASE_URL).host,
        f"/submit/{podcast_id}",
        "POST",
        aresponses.Response(status=202),
    )
    async with nrk_client() as nrk_api:
        nrk_api: NrkPodcastAPI
        await nrk_api.send_message(podcast_id, "Test message", phone=phone)
        with pytest.raises(NrkPsApiError):
            await nrk_api.send_message(podcast_id, "")


@pytest.mark.parametrize(
    "user_id",
    [
        "382cb4d7-aaaa-aaaa-aaaa-000000000000",
    ],
)
async def test_get_user_favorites(aresponses: ResponsesMockServer, nrk_client, user_id):
    """Test get user favorites."""
    setup_auth_mocks(aresponses)

    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/userdata/{user_id}/favourites",
        "GET",
        json_response(data=load_fixture_json("radio_userdata_favourites")),
    )

    async with nrk_client() as nrk_api:
        nrk_api: NrkPodcastAPI
        favourites = await nrk_api.get_user_favorites()
        assert isinstance(favourites, UserFavouritesResponse)


@pytest.mark.parametrize(
    "user_id",
    [
        "382cb4d7-aaaa-aaaa-aaaa-000000000000",
    ],
)
async def test_count_new_favourited_episodes(aresponses: ResponsesMockServer, nrk_client, user_id):
    """Test count new favourited episodes."""
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/userdata/{user_id}/newepisodes/count",
        "GET",
        json_response(data={"count": 50, "since": "0001-01-01T00:00:00Z"}),
    )
    async with nrk_client() as nrk_api:
        nrk_api: NrkPodcastAPI
        result = await nrk_api.count_new_favourited_episodes()
        assert isinstance(result, UserFavouriteNewEpisodesCountResponse)
        assert result.count == 50


@pytest.mark.parametrize(
    ("user_id", "item_type", "item_id"),
    [
        ("382cb4d7-aaaa-aaaa-aaaa-000000000000", "series", "distriktsprogram-troendelag"),
    ],
)
async def test_add_user_favourite(aresponses: ResponsesMockServer, nrk_client, user_id, item_type, item_id):
    """Test add user favourite."""
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/userdata/{user_id}/favourites/{item_type}/{item_id}",
        "PUT",
        json_response(data=load_fixture_json("radio_userdata_favourite")),
    )
    async with nrk_client() as nrk_api:
        nrk_api: NrkPodcastAPI
        result = await nrk_api.add_user_favourite(FavouriteType(item_type), item_id)
        assert isinstance(result, UserFavourite)


async def test_get_progress(aresponses: ResponsesMockServer, nrk_client):
    user_id = "382cb4d7-aaaa-aaaa-aaaa-000000000000"
    content_id = "hele_historien|l_6194e7b7-cabd-4da7-94e7-b7cabdfda7a3"
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/userdata/{user_id}/progress/podcastepisode/{content_id}",
        "GET",
        json_response(
            data={
                "id": content_id,
                "progress": "notStarted",
                "reportInterval": {
                    "notBefore": "PT5S",
                    "interval": "PT30S",
                },
                "_links": {
                    "self": {"href": f"/radio/userdata/{user_id}/progress/podcastepisode/{content_id}"}
                },
            }
        ),
    )

    async with nrk_client() as nrk_api:
        nrk_api: NrkPodcastAPI
        result = await nrk_api.get_progress(content_id, ProgressContentType.PODCASTEPISODE)
        assert isinstance(result, UserProgressResponse)
        assert result.id == content_id
        assert result.report_interval is not None
        assert result.report_interval.interval == "PT30S"
        result_dict = result.to_dict()
        assert result_dict["_links"]["self"]["href"].endswith(content_id)


async def test_get_upnext(aresponses: ResponsesMockServer, nrk_client):
    user_id = "382cb4d7-aaaa-aaaa-aaaa-000000000000"
    content_id = "hele_historien|l_6194e7b7-cabd-4da7-94e7-b7cabdfda7a3"
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/userdata/{user_id}/upnext/podcastepisode/{content_id}",
        "GET",
        json_response(
            data={
                "contentId": content_id,
                "upNextSource": "series",
                "upNextContentType": "podcastepisode",
                "_links": {
                    "self": {"href": f"/radio/userdata/{user_id}/upnext/podcastepisode/{content_id}"},
                    "next": {"href": f"/radio/userdata/{user_id}/upnext/podcastepisode/{content_id}"},
                },
            }
        ),
    )

    async with nrk_client() as nrk_api:
        nrk_api: NrkPodcastAPI
        result = await nrk_api.get_upnext(
            content_id,
            content_type=UpNextContentType.PODCASTEPISODE,
            context=UpNextContext.SERIES,
        )
        assert isinstance(result, UpNextResponse)
        assert result.content_id == content_id
        result_dict = result.to_dict()
        assert result_dict["_links"]["self"]["href"].endswith(content_id)


async def test_get_queue(aresponses: ResponsesMockServer, nrk_client):
    user_id = "382cb4d7-aaaa-aaaa-aaaa-000000000000"
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/userdata/{user_id}/queue",
        "GET",
        json_response(
            data={
                "_links": {
                    "self": {"href": f"/radio/userdata/{user_id}/queue"},
                    "add": {"href": f"/radio/userdata/{user_id}/queue/add"},
                    "delete": {"href": f"/radio/userdata/{user_id}/queue/delete"},
                },
                "queue": [],
                "status": {"code": "normal"},
            }
        ),
    )

    async with nrk_client() as nrk_api:
        nrk_api: NrkPodcastAPI
        result = await nrk_api.get_queue()
        assert isinstance(result, QueueResponse)
        assert isinstance(result.queue, list)
        result_dict = result.to_dict()
        assert result_dict["_links"]["self"]["href"].endswith("/queue")


async def test_add_to_queue(aresponses: ResponsesMockServer, nrk_client):
    user_id = "382cb4d7-aaaa-aaaa-aaaa-000000000000"
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/userdata/{user_id}/queue/add",
        "PUT",
        json_response(
            data={
                "_links": {
                    "self": {"href": f"/radio/userdata/{user_id}/queue"},
                    "add": {"href": f"/radio/userdata/{user_id}/queue/add"},
                    "delete": {"href": f"/radio/userdata/{user_id}/queue/delete"},
                },
                "queue": [{"id": "FNGH28374615", "type": "programs"}],
                "status": {"code": "normal"},
            }
        ),
    )

    async with nrk_client() as nrk_api:
        nrk_api: NrkPodcastAPI
        result = await nrk_api.add_to_queue(
            "FNGH28374615",
            QueueContentType.PROGRAMS,
            where=QueuePositionWhere.FIRST,
        )
        assert isinstance(result, QueueResponse)
        assert result.queue[0].id == "FNGH28374615"
        assert result.queue[0].type == "programs"


async def test_list_and_delete_continuations(aresponses: ResponsesMockServer, nrk_client):
    user_id = "382cb4d7-aaaa-aaaa-aaaa-000000000000"
    continuation_id = "9d8e8617-6f53-4a5e-a025-71a9f970f605"

    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/userdata/{user_id}/continuations",
        "GET",
        json_response(
            data={
                "_links": {
                    "self": {"href": f"/radio/userdata/{user_id}/continuations"},
                },
                "continuations": [],
            }
        ),
    )
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        f"/radio/userdata/{user_id}/continuations/{continuation_id}",
        "DELETE",
        aresponses.Response(status=204),
    )

    async with nrk_client() as nrk_api:
        nrk_api: NrkPodcastAPI
        result = await nrk_api.list_continuations(page_size=6)
        assert isinstance(result, ContinuationsResponse)
        assert isinstance(result.continuations, list)
        result_dict = result.to_dict()
        assert result_dict["_links"]["self"]["href"].endswith("/continuations")
        await nrk_api.delete_continuation(continuation_id)


async def test_no_user_id(nrk_client):
    """Test get user favorites."""
    async with nrk_client(load_default_credentials=False) as nrk_api:
        nrk_api: NrkPodcastAPI
        with pytest.raises(NrkPsApiAuthenticationError):
            await nrk_api.get_user_favorites()


async def test_internal_session(aresponses: ResponsesMockServer):
    """Test JSON response is handled correctly."""
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        "/ipcheck",
        "GET",
        aresponses.Response(
            status=200,
            headers={"Content-Type": "application/json"},
            text='{"status": "ok"}',
        ),
    )
    async with NrkPodcastAPI() as nrk_api:
        response = await nrk_api._request("ipcheck")
        assert response == {"status": "ok"}


async def test_timeout(aresponses: ResponsesMockServer):
    """Test request timeout."""

    # Faking a timeout by sleeping
    async def response_handler(_: aiohttp.ClientResponse):
        """Response handler for this test."""
        await asyncio.sleep(2)

    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        "/ipcheck",
        "GET",
        response_handler,
    )

    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False, request_timeout=1)
        with pytest.raises((NrkPsApiConnectionError, NrkPsApiConnectionTimeoutError)):
            assert await nrk_api._request("ipcheck")


async def test_http_error400(aresponses: ResponsesMockServer):
    """Test HTTP 400 response handling."""
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        "/ipcheck",
        "GET",
        aresponses.Response(text="Wtf", status=400),
    )

    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        with pytest.raises(NrkPsApiError):
            assert await nrk_api._request("ipcheck")


async def test_http_error404(aresponses: ResponsesMockServer):
    """Test HTTP 404 response handling."""
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        "/ipcheck",
        "GET",
        aresponses.Response(text="Not found", status=404),
    )

    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        with pytest.raises(NrkPsApiNotFoundError):
            assert await nrk_api._request("ipcheck")


async def test_http_error429(aresponses: ResponsesMockServer):
    """Test HTTP 429 response handling."""
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        "/ipcheck",
        "GET",
        aresponses.Response(text="Too many requests", status=429),
    )

    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        with pytest.raises(NrkPsApiRateLimitError):
            assert await nrk_api._request("ipcheck")


async def test_network_error():
    """Test network error handling."""
    async with aiohttp.ClientSession() as session:
        with patch.object(session, "request", side_effect=socket.gaierror):
            nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
            with pytest.raises(NrkPsApiConnectionError):
                assert await nrk_api._request("ipcheck")


async def test_unexpected_response(aresponses: ResponsesMockServer):
    """Test unexpected response handling."""
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        "/ipcheck",
        "GET",
        aresponses.Response(text="Success", status=200),
    )

    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        with pytest.raises(NrkPsApiError):
            assert await nrk_api._request("ipcheck")


async def test_unexpected_error(aresponses: ResponsesMockServer):
    """Test unexpected error handling."""
    aresponses.add(
        URL(PSAPI_BASE_URL).host,
        "/ipcheck",
        "GET",
        aresponses.Response(text="Error", status=418),
    )

    async with aiohttp.ClientSession() as session:
        nrk_api = NrkPodcastAPI(session=session, enable_cache=False)
        with pytest.raises(NrkPsApiError):
            assert await nrk_api._request("ipcheck")


async def test_close():
    nrk_api = NrkPodcastAPI()
    nrk_api.session = AsyncMock(spec=aiohttp.ClientSession)
    nrk_api._close_session = True  # pylint: disable=protected-access
    await nrk_api.close()
    nrk_api.session.close.assert_called_once()


async def test_context_manager():
    async with NrkPodcastAPI() as api:
        assert isinstance(api, NrkPodcastAPI)
    assert api.session is None or api.session.closed
