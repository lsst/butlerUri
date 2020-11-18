# This file is part of daf_butler.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (http://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import annotations

import contextlib
import urllib
import posixpath
import copy
import logging
import re

from pathlib import PurePath, PurePosixPath

__all__ = ('ButlerURI',)

from typing import (
    TYPE_CHECKING,
    Any,
    Iterator,
    Optional,
    Tuple,
    Type,
    Union,
)

from .utils import NoTransaction

if TYPE_CHECKING:
    from ..datastore import DatastoreTransaction


log = logging.getLogger(__name__)

# Regex for looking for URI escapes
ESCAPES_RE = re.compile(r"%[A-F0-9]{2}")


class ButlerURI:
    """Convenience wrapper around URI parsers.

    Provides access to URI components and can convert file
    paths into absolute path URIs. Scheme-less URIs are treated as if
    they are local file system paths and are converted to absolute URIs.

    A specialist subclass is created for each supported URI scheme.

    Parameters
    ----------
    uri : `str` or `urllib.parse.ParseResult`
        URI in string form.  Can be scheme-less if referring to a local
        filesystem path.
    root : `str` or `ButlerURI`, optional
        When fixing up a relative path in a ``file`` scheme or if scheme-less,
        use this as the root. Must be absolute.  If `None` the current
        working directory will be used. Can be a file URI.
    forceAbsolute : `bool`, optional
        If `True`, scheme-less relative URI will be converted to an absolute
        path using a ``file`` scheme. If `False` scheme-less URI will remain
        scheme-less and will not be updated to ``file`` or absolute path.
    forceDirectory: `bool`, optional
        If `True` forces the URI to end with a separator, otherwise given URI
        is interpreted as is.
    isTemporary : `bool`, optional
        If `True` indicates that this URI points to a temporary resource.
    """

    _pathLib: Type[PurePath] = PurePosixPath
    """Path library to use for this scheme."""

    _pathModule = posixpath
    """Path module to use for this scheme."""

    transferModes: Tuple[str, ...] = ("copy", "auto", "move")
    """Transfer modes supported by this implementation.

    Move is special in that it is generally a copy followed by an unlink.
    Whether that unlink works depends critically on whether the source URI
    implements unlink. If it does not the move will be reported as a failure.
    """

    transferDefault: str = "copy"
    """Default mode to use for transferring if ``auto`` is specified."""

    quotePaths = True
    """True if path-like elements modifying a URI should be quoted.

    All non-schemeless URIs have to internally use quoted paths. Therefore
    if a new file name is given (e.g. to updateFile or join) a decision must
    be made whether to quote it to be consistent.
    """

    isLocal = False
    """If `True` this URI refers to a local file."""

    # This is not an ABC with abstract methods because the __new__ being
    # a factory confuses mypy such that it assumes that every constructor
    # returns a ButlerURI and then determines that all the abstract methods
    # are still abstract. If they are not marked abstract but just raise
    # mypy is fine with it.

    # mypy is confused without these
    _uri: urllib.parse.ParseResult
    isTemporary: bool

    def __new__(cls, uri: Union[str, urllib.parse.ParseResult, ButlerURI],
                root: Optional[Union[str, ButlerURI]] = None, forceAbsolute: bool = True,
                forceDirectory: bool = False, isTemporary: bool = False) -> ButlerURI:
        parsed: urllib.parse.ParseResult
        dirLike: bool
        subclass: Optional[Type] = None

        # Record if we need to post process the URI components
        # or if the instance is already fully configured
        if isinstance(uri, str):
            # Since local file names can have special characters in them
            # we need to quote them for the parser but we can unquote
            # later. Assume that all other URI schemes are quoted.
            # Since sometimes people write file:/a/b and not file:///a/b
            # we should not quote in the explicit case of file:
            if "://" not in uri and not uri.startswith("file:"):
                if ESCAPES_RE.search(uri):
                    log.warning("Possible double encoding of %s", uri)
                else:
                    uri = urllib.parse.quote(uri)
            parsed = urllib.parse.urlparse(uri)
        elif isinstance(uri, urllib.parse.ParseResult):
            parsed = copy.copy(uri)
        elif isinstance(uri, ButlerURI):
            parsed = copy.copy(uri._uri)
            dirLike = uri.dirLike
            # No further parsing required and we know the subclass
            subclass = type(uri)
        else:
            raise ValueError(f"Supplied URI must be string, ButlerURI, or ParseResult but got '{uri!r}'")

        if subclass is None:
            # Work out the subclass from the URI scheme
            if not parsed.scheme:
                from .schemeless import ButlerSchemelessURI
                subclass = ButlerSchemelessURI
            elif parsed.scheme == "file":
                from .file import ButlerFileURI
                subclass = ButlerFileURI
            elif parsed.scheme == "s3":
                from .s3 import ButlerS3URI
                subclass = ButlerS3URI
            elif parsed.scheme.startswith("http"):
                from .http import ButlerHttpURI
                subclass = ButlerHttpURI
            elif parsed.scheme == "resource":
                # Rules for scheme names disallow pkg_resource
                from .packageresource import ButlerPackageResourceURI
                subclass = ButlerPackageResourceURI
            elif parsed.scheme == "mem":
                # in-memory datastore object
                from .mem import ButlerInMemoryURI
                subclass = ButlerInMemoryURI
            else:
                raise NotImplementedError(f"No URI support for scheme: '{parsed.scheme}'"
                                          " in {parsed.geturl()}")

            parsed, dirLike = subclass._fixupPathUri(parsed, root=root,
                                                     forceAbsolute=forceAbsolute,
                                                     forceDirectory=forceDirectory)

            # It is possible for the class to change from schemeless
            # to file so handle that
            if parsed.scheme == "file":
                from .file import ButlerFileURI
                subclass = ButlerFileURI

        # Now create an instance of the correct subclass and set the
        # attributes directly
        self = object.__new__(subclass)
        self._uri = parsed
        self.dirLike = dirLike
        self.isTemporary = isTemporary
        return self

    @property
    def scheme(self) -> str:
        """The URI scheme (``://`` is not part of the scheme)."""
        return self._uri.scheme

    @property
    def netloc(self) -> str:
        """The URI network location."""
        return self._uri.netloc

    @property
    def path(self) -> str:
        """The path component of the URI."""
        return self._uri.path

    @property
    def unquoted_path(self) -> str:
        """The path component of the URI with any URI quoting reversed."""
        return urllib.parse.unquote(self._uri.path)

    @property
    def ospath(self) -> str:
        """Path component of the URI localized to current OS."""
        raise AttributeError(f"Non-file URI ({self}) has no local OS path.")

    @property
    def relativeToPathRoot(self) -> str:
        """Returns path relative to network location.

        Effectively, this is the path property with posix separator stripped
        from the left hand side of the path.

        Always unquotes.
        """
        p = self._pathLib(self.path)
        relToRoot = str(p.relative_to(p.root))
        if self.dirLike and not relToRoot.endswith("/"):
            relToRoot += "/"
        return urllib.parse.unquote(relToRoot)

    @property
    def is_root(self) -> bool:
        """`True` if this URI points to the root of the network location.

        This means that the path components refers to the top level.
        """
        relpath = self.relativeToPathRoot
        if relpath == "./":
            return True
        return False

    @property
    def fragment(self) -> str:
        """The fragment component of the URI."""
        return self._uri.fragment

    @property
    def params(self) -> str:
        """Any parameters included in the URI."""
        return self._uri.params

    @property
    def query(self) -> str:
        """Any query strings included in the URI."""
        return self._uri.query

    def geturl(self) -> str:
        """Return the URI in string form.

        Returns
        -------
        url : `str`
            String form of URI.
        """
        return self._uri.geturl()

    def split(self) -> Tuple[ButlerURI, str]:
        """Splits URI into head and tail. Equivalent to os.path.split where
        head preserves the URI components.

        Returns
        -------
        head: `ButlerURI`
            Everything leading up to tail, expanded and normalized as per
            ButlerURI rules.
        tail : `str`
            Last `self.path` component. Tail will be empty if path ends on a
            separator. Tail will never contain separators. It will be
            unquoted.
        """
        head, tail = self._pathModule.split(self.path)
        headuri = self._uri._replace(path=head)

        # The file part should never include quoted metacharacters
        tail = urllib.parse.unquote(tail)

        # Schemeless is special in that it can be a relative path
        # We need to ensure that it stays that way. All other URIs will
        # be absolute already.
        forceAbsolute = self._pathModule.isabs(self.path)
        return ButlerURI(headuri, forceDirectory=True, forceAbsolute=forceAbsolute), tail

    def basename(self) -> str:
        """Returns the base name, last element of path, of the URI. If URI ends
        on a slash returns an empty string. This is the second element returned
        by split().

        Equivalent of os.path.basename().

        Returns
        -------
        tail : `str`
            Last part of the path attribute. Trail will be empty if path ends
            on a separator.
        """
        return self.split()[1]

    def dirname(self) -> ButlerURI:
        """Returns a ButlerURI containing all the directories of the path
        attribute.

        Equivalent of os.path.dirname()

        Returns
        -------
        head : `ButlerURI`
            Everything except the tail of path attribute, expanded and
            normalized as per ButlerURI rules.
        """
        return self.split()[0]

    def parent(self) -> ButlerURI:
        """Returns a ButlerURI containing all the directories of the path
        attribute, minus the last one.

        Returns
        -------
        head : `ButlerURI`
            Everything except the tail of path attribute, expanded and
            normalized as per ButlerURI rules.
        """
        # When self is file-like, return self.dirname()
        if not self.dirLike:
            return self.dirname()
        # When self is dir-like, return its parent directory,
        # regardless of the presence of a trailing separator
        originalPath = self._pathLib(self.path)
        parentPath = originalPath.parent
        parentURI = self._uri._replace(path=str(parentPath))

        return ButlerURI(parentURI, forceDirectory=True)

    def replace(self, **kwargs: Any) -> ButlerURI:
        """Replace components in a URI with new values and return a new
        instance.

        Returns
        -------
        new : `ButlerURI`
            New `ButlerURI` object with updated values.
        """
        return self.__class__(self._uri._replace(**kwargs))

    def updateFile(self, newfile: str) -> None:
        """Update in place the final component of the path with the supplied
        file name.

        Parameters
        ----------
        newfile : `str`
            File name with no path component.

        Notes
        -----
        Updates the URI in place.
        Updates the ButlerURI.dirLike attribute. The new file path will
        be quoted if necessary.
        """
        if self.quotePaths:
            newfile = urllib.parse.quote(newfile)
        dir, _ = self._pathModule.split(self.path)
        newpath = self._pathModule.join(dir, newfile)

        self.dirLike = False
        self._uri = self._uri._replace(path=newpath)

    def getExtension(self) -> str:
        """Return the file extension(s) associated with this URI path.

        Returns
        -------
        ext : `str`
            The file extension (including the ``.``). Can be empty string
            if there is no file extension. Usually returns only the last
            file extension unless there is a special extension modifier
            indicating file compression, in which case the combined
            extension (e.g. ``.fits.gz``) will be returned.
        """
        special = {".gz", ".bz2", ".xz", ".fz"}

        extensions = self._pathLib(self.path).suffixes

        if not extensions:
            return ""

        ext = extensions.pop()

        # Multiple extensions, decide whether to include the final two
        if extensions and ext in special:
            ext = f"{extensions[-1]}{ext}"

        return ext

    def join(self, path: str) -> ButlerURI:
        """Create a new `ButlerURI` with additional path components including
        a file.

        Parameters
        ----------
        path : `str`
            Additional file components to append to the current URI. Assumed
            to include a file at the end. Will be quoted depending on the
            associated URI scheme.

        Returns
        -------
        new : `ButlerURI`
            New URI with any file at the end replaced with the new path
            components.

        Notes
        -----
        Schemeless URIs assume local path separator but all other URIs assume
        POSIX separator if the supplied path has directory structure. It
        may be this never becomes a problem but datastore templates assume
        POSIX separator is being used.
        """
        new = self.dirname()  # By definition a directory URI

        # new should be asked about quoting, not self, since dirname can
        # change the URI scheme for schemeless -> file
        if new.quotePaths:
            path = urllib.parse.quote(path)

        newpath = self._pathModule.normpath(self._pathModule.join(new.path, path))
        new._uri = new._uri._replace(path=newpath)
        # Declare the new URI not be dirLike unless path ended in /
        if not path.endswith(self._pathModule.sep):
            new.dirLike = False
        return new

    def relative_to(self, other: ButlerURI) -> Optional[str]:
        """Return the relative path from this URI to the other URI.

        Parameters
        ----------
        other : `ButlerURI`
            URI to use to calculate the relative path. Must be a parent
            of this URI.

        Returns
        -------
        subpath : `str`
            The sub path of this URI relative to the supplied other URI.
            Returns `None` if there is no parent child relationship.
            Scheme and netloc must match.
        """
        if self.scheme != other.scheme or self.netloc != other.netloc:
            return None

        enclosed_path = self._pathLib(self.relativeToPathRoot)
        parent_path = other.relativeToPathRoot
        subpath: Optional[str]
        try:
            subpath = str(enclosed_path.relative_to(parent_path))
        except ValueError:
            subpath = None
        else:
            subpath = urllib.parse.unquote(subpath)
        return subpath

    def exists(self) -> bool:
        """Indicate that the resource is available.

        Returns
        -------
        exists : `bool`
            `True` if the resource exists.
        """
        raise NotImplementedError()

    def remove(self) -> None:
        """Remove the resource."""
        raise NotImplementedError()

    def isabs(self) -> bool:
        """Indicate that the resource is fully specified.

        For non-schemeless URIs this is always true.

        Returns
        -------
        isabs : `bool`
            `True` in all cases except schemeless URI.
        """
        return True

    def _as_local(self) -> Tuple[str, bool]:
        """Return the location of the (possibly remote) resource in the
        local file system.

        This is a helper function for ``as_local`` context manager.

        Returns
        -------
        path : `str`
            If this is a remote resource, it will be a copy of the resource
            on the local file system, probably in a temporary directory.
            For a local resource this should be the actual path to the
            resource.
        is_temporary : `bool`
            Indicates if the local path is a temporary file or not.
        """
        raise NotImplementedError()

    @contextlib.contextmanager
    def as_local(self) -> Iterator[ButlerURI]:
        """Return the location of the (possibly remote) resource in the
        local file system.

        Yields
        ------
        local : `ButlerURI`
            If this is a remote resource, it will be a copy of the resource
            on the local file system, probably in a temporary directory.
            For a local resource this should be the actual path to the
            resource.

        Notes
        -----
        The context manager will automatically delete any local temporary
        file.

        Examples
        --------
        Should be used as a context manager:

        .. code-block:: py

           with uri.as_local() as local:
               ospath = local.ospath
        """
        local_src, is_temporary = self._as_local()
        local_uri = ButlerURI(local_src, isTemporary=is_temporary)

        try:
            yield local_uri
        finally:
            # The caller might have relocated the temporary file
            if is_temporary and local_uri.exists():
                local_uri.remove()

    def read(self, size: int = -1) -> bytes:
        """Open the resource and return the contents in bytes.

        Parameters
        ----------
        size : `int`, optional
            The number of bytes to read. Negative or omitted indicates
            that all data should be read.
        """
        raise NotImplementedError()

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
        raise NotImplementedError()

    def mkdir(self) -> None:
        """For a dir-like URI, create the directory resource if it does not
        already exist.
        """
        raise NotImplementedError()

    def size(self) -> int:
        """For non-dir-like URI, return the size of the resource.

        Returns
        -------
        sz : `int`
            The size in bytes of the resource associated with this URI.
            Returns 0 if dir-like.
        """
        raise NotImplementedError()

    def __str__(self) -> str:
        return self.geturl()

    def __repr__(self) -> str:
        return f'ButlerURI("{self.geturl()}")'

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, ButlerURI):
            return False
        return self.geturl() == other.geturl()

    def __copy__(self) -> ButlerURI:
        # Implement here because the __new__ method confuses things
        # Be careful not to convert a relative schemeless URI to absolute
        return type(self)(str(self), forceAbsolute=self.isabs())

    def __deepcopy__(self, memo: Any) -> ButlerURI:
        # Implement here because the __new__ method confuses things
        return self.__copy__()

    def __getnewargs__(self) -> Tuple:
        return (str(self),)

    @staticmethod
    def _fixupPathUri(parsed: urllib.parse.ParseResult, root: Optional[Union[str, ButlerURI]] = None,
                      forceAbsolute: bool = False,
                      forceDirectory: bool = False) -> Tuple[urllib.parse.ParseResult, bool]:
        """Correct any issues with the supplied URI.

        Parameters
        ----------
        parsed : `~urllib.parse.ParseResult`
            The result from parsing a URI using `urllib.parse`.
        root : `str` or `ButlerURI`, ignored
            Not used by the this implementation since all URIs are
            absolute except for those representing the local file system.
        forceAbsolute : `bool`, ignored.
            Not used by this implementation. URIs are generally always
            absolute.
        forceDirectory : `bool`, optional
            If `True` forces the URI to end with a separator, otherwise given
            URI is interpreted as is. Specifying that the URI is conceptually
            equivalent to a directory can break some ambiguities when
            interpreting the last element of a path.

        Returns
        -------
        modified : `~urllib.parse.ParseResult`
            Update result if a URI is being handled.
        dirLike : `bool`
            `True` if given parsed URI has a trailing separator or
            forceDirectory is True. Otherwise `False`.

        Notes
        -----
        Relative paths are explicitly not supported by RFC8089 but `urllib`
        does accept URIs of the form ``file:relative/path.ext``. They need
        to be turned into absolute paths before they can be used.  This is
        always done regardless of the ``forceAbsolute`` parameter.

        AWS S3 differentiates between keys with trailing POSIX separators (i.e
        `/dir` and `/dir/`) whereas POSIX does not neccessarily.

        Scheme-less paths are normalized.
        """
        # assume we are not dealing with a directory like URI
        dirLike = False

        # URI is dir-like if explicitly stated or if it ends on a separator
        endsOnSep = parsed.path.endswith(posixpath.sep)
        if forceDirectory or endsOnSep:
            dirLike = True
            # only add the separator if it's not already there
            if not endsOnSep:
                parsed = parsed._replace(path=parsed.path+posixpath.sep)

        return parsed, dirLike

    def transfer_from(self, src: ButlerURI, transfer: str,
                      overwrite: bool = False,
                      transaction: Optional[Union[DatastoreTransaction, NoTransaction]] = None) -> None:
        """Transfer the current resource to a new location.

        Parameters
        ----------
        src : `ButlerURI`
            Source URI.
        transfer : `str`
            Mode to use for transferring the resource. Generically there are
            many standard options: copy, link, symlink, hardlink, relsymlink.
            Not all URIs support all modes.
        overwrite : `bool`, optional
            Allow an existing file to be overwritten. Defaults to `False`.
        transaction : `DatastoreTransaction`, optional
            A transaction object that can (depending on implementation)
            rollback transfers on error.  Not guaranteed to be implemented.

        Notes
        -----
        Conceptually this is hard to scale as the number of URI schemes
        grow.  The destination URI is more important than the source URI
        since that is where all the transfer modes are relevant (with the
        complication that "move" deletes the source).

        Local file to local file is the fundamental use case but every
        other scheme has to support "copy" to local file (with implicit
        support for "move") and copy from local file.
        All the "link" options tend to be specific to local file systems.

        "move" is a "copy" where the remote resource is deleted at the end.
        Whether this works depends on the source URI rather than the
        destination URI.  Reverting a move on transaction rollback is
        expected to be problematic if a remote resource was involved.
        """
        raise NotImplementedError(f"No transfer modes supported by URI scheme {self.scheme}")
