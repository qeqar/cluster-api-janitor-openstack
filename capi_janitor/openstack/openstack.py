import asyncio
import contextlib
import ssl
import urllib.parse

import httpx

from easykube import rest


class UnsupportedAuthenticationError(Exception):
    """
    Raised when an unsupported authentication method is used.
    """
    def __init__(self, auth_type):
        super().__init__(f"unsupported authentication type: {auth_type}")


class Auth(httpx.Auth):
    """
    Authenticator class for OpenStack connections.
    """
    def __init__(self, auth_url, application_credential_id, application_credential_secret):
        self.url = auth_url
        self._application_credential_id = application_credential_id
        self._application_credential_secret = application_credential_secret
        self._token = None
        self._lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def _refresh_token(self):
        """
        Context manager to ensure only one request at a time triggers a token refresh.
        """
        token = self._token
        async with self._lock:
            # Only yield to the wrapped block if the token has not changed
            # in the time it took to acquire the lock
            if token == self._token:
                yield

    def _build_token_request(self):
        return httpx.Request(
            "POST",
            f"{self.url}/v3/auth/tokens",
            json = {
                "auth": {
                    "identity": {
                        "methods": ["application_credential"],
                        "application_credential": {
                            "id": self._application_credential_id,
                            "secret": self._application_credential_secret,
                        },
                    },
                },
            }
        )
    
    def _handle_token_response(self, response):
        response.raise_for_status()
        self._token = response.headers["X-Subject-Token"]

    async def async_auth_flow(self, request):
        if self._token is None:
            async with self._refresh_token():
                response = yield self._build_token_request()
                self._handle_token_response(response)
        request.headers['X-Auth-Token'] = self._token
        response = yield request


class Resource(rest.Resource):
    """
    Base resource for OpenStack APIs.
    """
    def __init__(self, client, name, prefix = None, plural_name = None, singular_name = None):
        super().__init__(client, name, prefix)
        # Some resources support a /detail endpoint
        # In this case, we just want to use the name up to the slash as the plural name
        self._plural_name = plural_name or self._name.split("/")[0]
        # If no singular name is given, assume the name ends in 's'
        self._singular_name = singular_name or self._plural_name[:-1]

    def _extract_list(self, response):
        # Some resources support a /detail endpoint
        # In this case, we just want to use the name up to the slash
        return response.json()[self._plural_name]
    
    def _extract_next_page(self, response):
        next_url = next(
            (
                link["href"]
                for link in response.json().get(f"{self._plural_name}_links", [])
                if link["rel"] == "next"
            ),
            None
        )
        # Sometimes, the returned URLs have http where they should have https
        # To mitigate this, we split the URL and return the path and params separately
        url = urllib.parse.urlsplit(next_url)
        params = urllib.parse.parse_qs(url.query)
        return url.path, params

    def _extract_one(self, response):
        content_type = response.headers.get("content-type")
        if content_type == "application/json":
            return response.json()[self._singular_name]
        else:
            return super()._extract_one(response)


class Client(rest.AsyncClient):
    """
    Client for OpenStack APIs.
    """
    def __init__(self, /, base_url, prefix = None, **kwargs):
        # Extract the path part of the base_url
        url = urllib.parse.urlsplit(base_url)
        # Initialise the client with the scheme/host
        super().__init__(base_url = f"{url.scheme}://{url.netloc}", **kwargs)
        # If another prefix is not given, use the path from the base URL as the prefix
        # This ensures things like pagination work nicely without duplicating the prefix
        self._prefix = prefix or url.path

    def __aenter__(self):
        # Prevent individual clients from being used in a context manager
        raise RuntimeError("clients must be used via a cloud object")
    
    def resource(self, name, plural_name = None, singular_name = None):
        return Resource(self, name, self._prefix, plural_name, singular_name)


class Cloud:
    """
    Object for interacting with OpenStack clouds.
    """
    def __init__(self, auth, transport, interface):
        self._auth = auth
        self._transport = transport
        self._interface = interface
        self._endpoints = {}
        # A map of api name to client
        self._clients = {}

    async def __aenter__(self):
        await self._transport.__aenter__()
        # Once the transport has been initialised, we can initialise the endpoints
        client = Client(base_url = self._auth.url, auth = self._auth, transport = self._transport)
        response = await client.get("/v3/auth/catalog")
        self._endpoints = {
            entry["type"]: next(
                ep["url"]
                for ep in entry["endpoints"]
                if ep["interface"] == self._interface
            )
            for entry in response.json()["catalog"]
        }
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self._transport.__aexit__(exc_type, exc_value, traceback)

    @property
    def apis(self):
        """
        The APIs supported by the cloud.
        """
        return list(self._endpoints.keys())

    def api_client(self, name, prefix = None):
        """
        Returns a client for the named API.
        """
        if name not in self._clients:
            self._clients[name] = Client(
                base_url = self._endpoints[name],
                prefix = prefix,
                auth = self._auth,
                transport = self._transport
            )
        return self._clients[name]

    @classmethod
    def from_clouds(cls, clouds, cloud = "openstack", cacert = None):
        config = clouds["clouds"][cloud]
        if config["auth_type"] != "v3applicationcredential":
            raise UnsupportedAuthenticationError(config["auth_type"])
        auth = Auth(
            config["auth"]["auth_url"],
            config["auth"]["application_credential_id"],
            config["auth"]["application_credential_secret"]
        )
        # Create a default context using the verification from the config
        context = httpx.create_ssl_context(verify = config.get("verify", True))
        # If a cacert was given, load it into the context
        if cacert is not None:
            context.load_verify_locations(cadata = cacert)
        transport = httpx.AsyncHTTPTransport(verify = context)
        return cls(auth, transport, config.get("interface", "public"))
