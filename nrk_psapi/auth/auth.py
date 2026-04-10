from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from http import HTTPStatus
from urllib.parse import quote_plus

from aiohttp.client import ClientError, ClientResponse, ClientResponseError, ClientSession, ClientTimeout
import scrypt
from yarl import URL

from nrk_psapi.auth.const import (
    DEFAULT_USER_AGENT,
    OAUTH_AUTH_BASE_URL,
    OAUTH_CLIENT_ID,
    OAUTH_LOGIN_BASE_URL,
    OAUTH_RETURN_URL,
)
from nrk_psapi.auth.models import HashingInstructions, NrkAuthCredentials, NrkUserLoginDetails
from nrk_psapi.auth.utils import parse_hashing_algorithm
from nrk_psapi.const import LOGGER as _LOGGER
from nrk_psapi.exceptions import (
    NrkPsApiAuthenticationError,
    NrkPsApiConnectionError,
    NrkPsApiConnectionTimeoutError,
    NrkPsApiError,
    NrkPsApiNoCredentialsError,
    NrkPsApiNoCredentialsOrLoginDetailsError,
    NrkPsApiNotFoundError,
    NrkPsApiRateLimitError,
    NrkPsAuthorizationError,
)


@dataclass
class NrkAuthClient:
    user_agent: str = DEFAULT_USER_AGENT

    request_timeout: int = 15
    session: ClientSession | None = None

    credentials: NrkAuthCredentials | None = None
    login_details: NrkUserLoginDetails | None = None

    _credentials: NrkAuthCredentials | None = field(default=None, init=False)
    _close_session: bool = False

    def __post_init__(self):
        """Initialize the client after dataclass initialization."""
        if self.credentials is not None:
            self.set_credentials(self.credentials)

    def get_credentials(self) -> dict | None:
        """Get the current credentials as a dictionary, or None if not set."""
        if self._credentials is not None:
            return self._credentials.to_dict()
        return None  # pragma: no cover

    def set_credentials(self, credentials: NrkAuthCredentials | dict | str):
        """Set the credentials.

        Args:
            credentials (NrkAuthCredentials | dict | str): The credentials to set.

        """
        if isinstance(credentials, NrkAuthCredentials):
            self._credentials = credentials
        elif isinstance(credentials, dict):
            self._credentials = NrkAuthCredentials.from_dict(credentials)
        else:
            self._credentials = NrkAuthCredentials.from_json(credentials)

    async def async_get_access_token(self) -> str:
        """Get access token."""
        if self._credentials is None and self.login_details is None:
            raise NrkPsApiNoCredentialsOrLoginDetailsError("No credentials or login details set")

        if self._credentials is None:
            try:
                credentials = await self.authorize(self.login_details)
            except NrkPsApiAuthenticationError as err:
                raise NrkPsApiAuthenticationError("Unable to get access token") from err
            except NrkPsApiConnectionError as err:
                _LOGGER.warning("Unable to get access token: %s", err)
                raise
            self.set_credentials(credentials)

        return self._credentials.access_token

    async def async_get_access_token_refreshed(self) -> str:
        """Get access token and refresh expired credentials when possible."""
        if (
            self._credentials is not None
            and self._credentials.is_expired()
            and self.login_details is not None
        ):
            try:
                credentials = await self.authorize(self.login_details)
            except NrkPsApiAuthenticationError as err:
                raise NrkPsApiAuthenticationError("Unable to refresh access token") from err
            except NrkPsApiConnectionError as err:
                _LOGGER.warning("Unable to refresh access token: %s", err)
                raise
            self.set_credentials(credentials)

        return await self.async_get_access_token()

    async def get_user_id(self) -> str:
        """Get user id."""
        if self._credentials is None:
            raise NrkPsApiNoCredentialsError("No credentials set")
        return self._credentials.session.user.sub

    @property
    def request_header(self) -> dict[str, str]:
        """Generate a header for HTTP requests to the server."""
        return {
            "user-agent": self.user_agent,
        }

    def setup_session(self):
        if self.session is None:
            timeout = ClientTimeout(total=self.request_timeout)
            self.session = ClientSession(timeout=timeout)
            _LOGGER.debug("New session created.")
            self._close_session = True

    @staticmethod
    def _build_url(uri: str, base_url: str | None = None) -> str:
        if base_url is None:
            base_url = OAUTH_AUTH_BASE_URL
        return str(URL(base_url).join(URL(uri)))

    @staticmethod
    async def _request_check_status(response: ClientResponse):
        content_type = response.headers.get("Content-Type", "")
        error_msg = None
        if "application/json" in content_type:
            error = await response.json()
            with contextlib.suppress(KeyError, IndexError, TypeError):
                error_msg = error["errors"][0]["message"]

        if response.status == HTTPStatus.TOO_MANY_REQUESTS:
            raise NrkPsApiRateLimitError(error_msg or "Too many requests to NRK API. Try again later.")
        if response.status == HTTPStatus.NOT_FOUND:
            raise NrkPsApiNotFoundError("Resource not found")
        if response.status == HTTPStatus.BAD_REQUEST:
            raise NrkPsApiAuthenticationError(error_msg or "Bad request syntax or unsupported method")
        if response.status == HTTPStatus.UNAUTHORIZED:
            raise NrkPsAuthorizationError(error_msg or "Permission denied")
        if response.status == HTTPStatus.FORBIDDEN:
            raise NrkPsAuthorizationError(error_msg or "Authorization failed")
        if not HTTPStatus(response.status).is_success:
            raise NrkPsApiError(response)

    async def _get_hashing_instructions(self, email: str) -> HashingInstructions:
        """Fetch hashing instructions from the server."""
        async with self.session.post(
            self._build_url("getHashingInstructions"),
            json={"email": email},
            headers=self.request_header,
            raise_for_status=self._request_check_status,
        ) as response:
            data = await response.json()
            return HashingInstructions.from_dict(data)

    async def _get_callback_url(self) -> URL:
        """Get callback url."""
        async with self.session.get(
            self._build_url("auth/web/login", OAUTH_LOGIN_BASE_URL),
            params={
                "returnUrl": OAUTH_RETURN_URL,
            },
            headers=self.request_header,
            raise_for_status=self._request_check_status,
        ) as response:
            return response.history[-1].url

    async def _login(self, auth_email: str, auth_password: str, hashing_instructions: HashingInstructions):
        """Login."""

        # Generate hashed password
        algo = parse_hashing_algorithm(hashing_instructions.current.algorithm)
        hashed_password = scrypt.hash(
            auth_password, hashing_instructions.current.salt, algo["n"], algo["r"], algo["p"], algo["dkLen"]
        ).hex()

        async with self.session.post(
            self._build_url("logginn"),
            json={
                "username": auth_email,
                "password": auth_password,
                "hashedPassword": {
                    "current": {
                        "recipe": hashing_instructions.current.to_dict(),
                        "hash": hashed_password,
                    },
                    "next": None,
                },
                "clientId": OAUTH_CLIENT_ID,
                "addUser": False,
            },
            params={
                "encodedExitUrl": quote_plus(OAUTH_RETURN_URL),
            },
            headers=self.request_header,
            raise_for_status=self._request_check_status,
        ) as response:
            user_data = await response.json()
            _LOGGER.debug("Got user data: %s", user_data)

    async def _finalize_login(self, params: dict[str, str]) -> dict:
        # Finalize auth flow
        async with self.session.get(
            self._build_url("connect/authorize/callback"),
            params=params,
            headers=self.request_header,
            raise_for_status=self._request_check_status,
        ) as response:
            await response.text()

        return await self.token_for_sub()

    async def token_for_sub(self, sub: str | None = None) -> dict:
        """Get token for sub."""
        async with self.session.post(
            self._build_url("auth/csrf_init", OAUTH_LOGIN_BASE_URL),
            headers=self.request_header,
            raise_for_status=self._request_check_status,
        ) as response:
            await response.json()

        async with self.session.get(
            self._build_url("auth/contextinfo", OAUTH_LOGIN_BASE_URL),
        ) as response:
            await response.json()

        headers = self.request_header
        if sub is None:
            sub = "_"
        elif self._credentials is not None:
            headers.update(self._credentials.authenticated_headers())

        # Fetch token
        async with self.session.post(
            self._build_url(f"auth/session/tokenforsub/{sub}", OAUTH_LOGIN_BASE_URL),
            headers=headers,
            raise_for_status=self._request_check_status,
        ) as response:
            credentials = await response.json()

        cookies = self.session.cookie_jar.filter_cookies(OAUTH_LOGIN_BASE_URL)
        credentials["nrk_login"] = cookies.get("nrk.login").value
        return credentials

    async def authorize(self, login_details: NrkUserLoginDetails) -> NrkAuthCredentials:
        """Authorize."""
        auth_email = login_details.email
        auth_password = login_details.password

        try:
            callback_url = await self._get_callback_url()
            hashing_instructions = await self._get_hashing_instructions(auth_email)
            await self._login(auth_email, auth_password, hashing_instructions)

            callback_params = dict(callback_url.query)
            auth_data = await self._finalize_login(callback_params)
            return NrkAuthCredentials.from_dict(auth_data)
        except asyncio.TimeoutError as exception:
            raise NrkPsApiConnectionTimeoutError("Timed out while waiting for server response") from exception
        except (ClientError, ClientResponseError) as err:
            raise NrkPsApiConnectionError("Unknown error during authentication") from err

    async def close(self) -> None:
        """Close open client session."""
        if self.session and self._close_session:
            await self.session.close()

    async def __aenter__(self):
        """Async enter."""
        self.setup_session()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        """Async exit."""
        await self.close()
