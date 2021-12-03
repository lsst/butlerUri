# This file is part of lsst-resources.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# Use of this source code is governed by a 3-clause BSD-style
# license that can be found in the LICENSE file.

from __future__ import annotations

import os
import os.path
import requests
import tempfile
import logging
import functools

__all__ = ('HttpResourcePath', )

from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

from typing import (
    TYPE_CHECKING,
    Optional,
    Tuple,
    Union,
)

from lsst.utils.timer import time_this
from ._resourcePath import ResourcePath

if TYPE_CHECKING:
    from .utils import TransactionProtocol

log = logging.getLogger(__name__)

# Default timeout for all HTTP requests, in seconds
TIMEOUT = 20


def getHttpSession() -> requests.Session:
    """Create a requests.Session pre-configured with environment variable data.

    Returns
    -------
    session : `requests.Session`
        An http session used to execute requests.

    Notes
    -----
    The following environment variables must be set:
    - LSST_BUTLER_WEBDAV_CA_BUNDLE: the directory where CA
        certificates are stored if you intend to use HTTPS to
        communicate with the endpoint.
    - LSST_BUTLER_WEBDAV_AUTH: which authentication method to use.
        Possible values are X509 and TOKEN
    - (X509 only) LSST_BUTLER_WEBDAV_PROXY_CERT: path to proxy
        certificate used to authenticate requests
    - (TOKEN only) LSST_BUTLER_WEBDAV_TOKEN_FILE: file which
        contains the bearer token used to authenticate requests
    - (OPTIONAL) LSST_BUTLER_WEBDAV_EXPECT100: if set, we will add an
        "Expect: 100-Continue" header in all requests. This is required
        on certain endpoints where requests redirection is made.
    """
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])

    session = requests.Session()
    session.mount("http://", HTTPAdapter(max_retries=retries))
    session.mount("https://", HTTPAdapter(max_retries=retries))

    log.debug("Creating new HTTP session...")

    ca_bundle = None
    try:
        ca_bundle = os.environ['LSST_BUTLER_WEBDAV_CA_BUNDLE']
    except KeyError:
        log.debug("Environment variable LSST_BUTLER_WEBDAV_CA_BUNDLE is not set: "
                  "If you would like to trust additional CAs, please consider "
                  "exporting this variable.")
    session.verify = ca_bundle

    try:
        env_auth_method = os.environ['LSST_BUTLER_WEBDAV_AUTH']
    except KeyError:
        log.debug("Environment variable LSST_BUTLER_WEBDAV_AUTH is not set, "
                  "no authentication configured.")
        log.debug("Unauthenticated session configured and ready.")
        return session

    if env_auth_method == "X509":
        log.debug("... using x509 authentication.")
        try:
            proxy_cert = os.environ['LSST_BUTLER_WEBDAV_PROXY_CERT']
        except KeyError:
            raise KeyError("Environment variable LSST_BUTLER_WEBDAV_PROXY_CERT is not set")
        session.cert = (proxy_cert, proxy_cert)
    elif env_auth_method == "TOKEN":
        log.debug("... using bearer-token authentication.")
        refreshToken(session)
    else:
        raise ValueError("Environment variable LSST_BUTLER_WEBDAV_AUTH must be set to X509 or TOKEN")

    log.debug("Authenticated session configured and ready.")
    return session


def useExpect100() -> bool:
    """Return the status of the "Expect-100" header.

    Returns
    -------
    useExpect100 : `bool`
        True if LSST_BUTLER_WEBDAV_EXPECT100 is set, False otherwise.
    """
    # This header is required for request redirection, in dCache for example
    if "LSST_BUTLER_WEBDAV_EXPECT100" in os.environ:
        log.debug("Expect: 100-Continue header enabled.")
        return True
    return False


def isTokenAuth() -> bool:
    """Return the status of bearer-token authentication.

    Returns
    -------
    isTokenAuth : `bool`
        True if LSST_BUTLER_WEBDAV_AUTH is set to TOKEN, False otherwise.
    """
    try:
        env_auth_method = os.environ['LSST_BUTLER_WEBDAV_AUTH']
    except KeyError:
        raise KeyError("Environment variable LSST_BUTLER_WEBDAV_AUTH is not set, "
                       "please use values X509 or TOKEN")

    if env_auth_method == "TOKEN":
        return True
    return False


def refreshToken(session: requests.Session) -> None:
    """Refresh the session token.

    Set or update the 'Authorization' header of the session,
    configure bearer token authentication, with the value fetched
    from LSST_BUTLER_WEBDAV_TOKEN_FILE

    Parameters
    ----------
    session : `requests.Session`
        Session on which bearer token authentication must be configured.
    """
    try:
        token_path = os.environ['LSST_BUTLER_WEBDAV_TOKEN_FILE']
        if not os.path.isfile(token_path):
            raise FileNotFoundError(f"No token file: {token_path}")
        with open(os.environ['LSST_BUTLER_WEBDAV_TOKEN_FILE'], "r") as fh:
            bearer_token = fh.read().replace('\n', '')
    except KeyError:
        raise KeyError("Environment variable LSST_BUTLER_WEBDAV_TOKEN_FILE is not set")

    session.headers.update({'Authorization': 'Bearer ' + bearer_token})


@functools.lru_cache
def isWebdavEndpoint(path: Union[ResourcePath, str]) -> bool:
    """Check whether the remote HTTP endpoint implements Webdav features.

    Parameters
    ----------
    path : `ResourcePath` or `str`
        URL to the resource to be checked.
        Should preferably refer to the root since the status is shared
        by all paths in that server.

    Returns
    -------
    isWebdav : `bool`
        True if the endpoint implements Webdav, False if it doesn't.
    """
    ca_bundle = None
    try:
        ca_bundle = os.environ['LSST_BUTLER_WEBDAV_CA_BUNDLE']
    except KeyError:
        log.warning("Environment variable LSST_BUTLER_WEBDAV_CA_BUNDLE is not set: "
                    "some HTTPS requests will fail. If you intend to use HTTPS, please "
                    "export this variable.")

    log.debug("Detecting HTTP endpoint type for '%s'...", path)
    r = requests.options(str(path), verify=ca_bundle)
    return True if 'DAV' in r.headers else False


def finalurl(r: requests.Response) -> str:
    """Calculate the final URL, including redirects.

    Check whether the remote HTTP endpoint redirects to a different
    endpoint, and return the final destination of the request.
    This is needed when using PUT operations, to avoid starting
    to send the data to the endpoint, before having to send it again once
    the 307 redirect response is received, and thus wasting bandwidth.

    Parameters
    ----------
    r : `requests.Response`
        An HTTP response received when requesting the endpoint

    Returns
    -------
    destination_url: `string`
        The final destination to which requests must be sent.
    """
    destination_url = r.url
    if r.status_code == 307:
        destination_url = r.headers['Location']
        log.debug("Request redirected to %s", destination_url)
    return destination_url


class HttpResourcePath(ResourcePath):
    """General HTTP(S) resource."""

    _session = requests.Session()
    _sessionInitialized = False
    _is_webdav: Optional[bool] = None

    @property
    def session(self) -> requests.Session:
        """Client object to address remote resource."""
        cls = type(self)
        if cls._sessionInitialized:
            if isTokenAuth():
                refreshToken(cls._session)
            return cls._session

        s = getHttpSession()
        cls._session = s
        cls._sessionInitialized = True
        return s

    @property
    def is_webdav_endpoint(self) -> bool:
        """Check if the current endpoint implements WebDAV features.

        This is stored per URI but cached by root so there is
        only one check per hostname.
        """
        if self._is_webdav is not None:
            return self._is_webdav

        self._is_webdav = isWebdavEndpoint(self.root_uri())
        return self._is_webdav

    def exists(self) -> bool:
        """Check that a remote HTTP resource exists."""
        log.debug("Checking if resource exists: %s", self.geturl())
        r = self.session.head(self.geturl(), timeout=TIMEOUT)

        return True if r.status_code == 200 else False

    def size(self) -> int:
        """Return the size of the remote resource in bytes."""
        if self.dirLike:
            return 0
        r = self.session.head(self.geturl(), timeout=TIMEOUT)
        if r.status_code == 200:
            return int(r.headers['Content-Length'])
        else:
            raise FileNotFoundError(f"Resource {self} does not exist")

    def mkdir(self) -> None:
        """Create the directory resource if it does not already exist."""
        # Only available on WebDAV backends
        if not self.is_webdav_endpoint:
            raise NotImplementedError("Endpoint does not implement WebDAV functionality")

        if not self.dirLike:
            raise ValueError(f"Can not create a 'directory' for file-like URI {self}")

        if not self.exists():
            # We need to test the absence of the parent directory,
            # but also if parent URL is different from self URL,
            # otherwise we could be stuck in a recursive loop
            # where self == parent
            if not self.parent().exists() and self.parent().geturl() != self.geturl():
                self.parent().mkdir()
            log.debug("Creating new directory: %s", self.geturl())
            r = self.session.request("MKCOL", self.geturl(), timeout=TIMEOUT)
            if r.status_code != 201:
                if r.status_code == 405:
                    log.debug("Can not create directory: %s may already exist: skipping.", self.geturl())
                else:
                    raise ValueError(f"Can not create directory {self}, status code: {r.status_code}")

    def remove(self) -> None:
        """Remove the resource."""
        log.debug("Removing resource: %s", self.geturl())
        r = self.session.delete(self.geturl(), timeout=TIMEOUT)
        if r.status_code not in [200, 202, 204]:
            raise FileNotFoundError(f"Unable to delete resource {self}; status code: {r.status_code}")

    def _as_local(self) -> Tuple[str, bool]:
        """Download object over HTTP and place in temporary directory.

        Returns
        -------
        path : `str`
            Path to local temporary file.
        temporary : `bool`
            Always returns `True`. This is always a temporary file.
        """
        log.debug("Downloading remote resource as local file: %s", self.geturl())
        r = self.session.get(self.geturl(), stream=True, timeout=TIMEOUT)
        if r.status_code != 200:
            raise FileNotFoundError(f"Unable to download resource {self}; status code: {r.status_code}")
        with tempfile.NamedTemporaryFile(suffix=self.getExtension(), delete=False) as tmpFile:
            with time_this(log, msg="Downloading %s to local file", args=(self,)):
                for chunk in r.iter_content():
                    tmpFile.write(chunk)
        return tmpFile.name, True

    def read(self, size: int = -1) -> bytes:
        """Open the resource and return the contents in bytes.

        Parameters
        ----------
        size : `int`, optional
            The number of bytes to read. Negative or omitted indicates
            that all data should be read.
        """
        log.debug("Reading from remote resource: %s", self.geturl())
        stream = True if size > 0 else False
        with time_this(log, msg="Read from remote resource %s", args=(self,)):
            r = self.session.get(self.geturl(), stream=stream, timeout=TIMEOUT)
        if r.status_code != 200:
            raise FileNotFoundError(f"Unable to read resource {self}; status code: {r.status_code}")
        if not stream:
            return r.content
        else:
            return next(r.iter_content(chunk_size=size))

    def write(self, data: bytes, overwrite: bool = True) -> None:
        """Write the supplied bytes to the new resource.

        Parameters
        ----------
        data : `bytes`
            The bytes to write to the resource. The entire contents of the
            resource will be replaced.
        overwrite : `bool`, optional
            If `True` the resource will be overwritten if it exists. Otherwise
            the write will fail.
        """
        log.debug("Writing to remote resource: %s", self.geturl())
        if not overwrite:
            if self.exists():
                raise FileExistsError(f"Remote resource {self} exists and overwrite has been disabled")
        dest_url = finalurl(self._emptyPut())
        with time_this(log, msg="Write data to remote %s", args=(self,)):
            r = self.session.put(dest_url, data=data, timeout=TIMEOUT)
        if r.status_code not in [201, 202, 204]:
            raise ValueError(f"Can not write file {self}, status code: {r.status_code}")

    def transfer_from(self, src: ResourcePath, transfer: str = "copy",
                      overwrite: bool = False,
                      transaction: Optional[TransactionProtocol] = None) -> None:
        """Transfer the current resource to a Webdav repository.

        Parameters
        ----------
        src : `ResourcePath`
            Source URI.
        transfer : `str`
            Mode to use for transferring the resource. Supports the following
            options: copy.
        transaction : `~lsst.resources.utils.TransactionProtocol`, optional
            Currently unused.
        """
        # Fail early to prevent delays if remote resources are requested
        if transfer not in self.transferModes:
            raise ValueError(f"Transfer mode {transfer} not supported by URI scheme {self.scheme}")

        # Existence checks cost time so do not call this unless we know
        # that debugging is enabled.
        if log.isEnabledFor(logging.DEBUG):
            log.debug("Transferring %s [exists: %s] -> %s [exists: %s] (transfer=%s)",
                      src, src.exists(), self, self.exists(), transfer)

        if self.exists():
            raise FileExistsError(f"Destination path {self} already exists.")

        if transfer == "auto":
            transfer = self.transferDefault

        if isinstance(src, type(self)):
            # Only available on WebDAV backends
            if not self.is_webdav_endpoint:
                raise NotImplementedError("Endpoint does not implement WebDAV functionality")

            with time_this(log, msg="Transfer from %s to %s directly", args=(src, self)):
                if transfer == "move":
                    r = self.session.request("MOVE", src.geturl(),
                                             headers={"Destination": self.geturl()},
                                             timeout=TIMEOUT)
                    log.debug("Running move via MOVE HTTP request.")
                else:
                    r = self.session.request("COPY", src.geturl(),
                                             headers={"Destination": self.geturl()},
                                             timeout=TIMEOUT)
                    log.debug("Running copy via COPY HTTP request.")
        else:
            # Use local file and upload it
            with src.as_local() as local_uri:
                with open(local_uri.ospath, "rb") as f:
                    dest_url = finalurl(self._emptyPut())
                    with time_this(log, msg="Transfer from %s to %s via local file", args=(src, self)):
                        r = self.session.put(dest_url, data=f, timeout=TIMEOUT)

        if r.status_code not in [201, 202, 204]:
            raise ValueError(f"Can not transfer file {self}, status code: {r.status_code}")

        # This was an explicit move requested from a remote resource
        # try to remove that resource
        if transfer == "move":
            # Transactions do not work here
            src.remove()

    def _emptyPut(self) -> requests.Response:
        """Send an empty PUT request to current URL.

        This is used to detect if redirection is enabled before sending actual
        data.

        Returns
        -------
        response : `requests.Response`
            HTTP Response from the endpoint.
        """
        headers = {"Content-Length": "0"}
        if useExpect100():
            headers["Expect"] = "100-continue"
        return self.session.put(self.geturl(), data=None, headers=headers,
                                allow_redirects=False, timeout=TIMEOUT)
