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

import os
import shutil
import tempfile
import unittest
import urllib.parse
import responses

try:
    import boto3
    import botocore
    from moto import mock_s3
except ImportError:
    boto3 = None

    def mock_s3(cls):
        """A no-op decorator in case moto mock_s3 can not be imported.
        """
        return cls

from lsst.daf.butler import ButlerURI
from lsst.daf.butler.core.s3utils import (setAwsEnvCredentials,
                                          unsetAwsEnvCredentials)

TESTDIR = os.path.abspath(os.path.dirname(__file__))


class FileURITestCase(unittest.TestCase):
    """Concrete tests for local files"""

    def setUp(self):
        # Use a local tempdir because on macOS the temp dirs use symlinks
        # so relsymlink gets quite confused.
        self.tmpdir = tempfile.mkdtemp(dir=TESTDIR)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def testFile(self):
        file = os.path.join(self.tmpdir, "test.txt")
        uri = ButlerURI(file)
        self.assertFalse(uri.exists(), f"{uri} should not exist")
        self.assertEqual(uri.ospath, file)

        content = "abcdefghijklmnopqrstuv\n"
        uri.write(content.encode())
        self.assertTrue(os.path.exists(file), "File should exist locally")
        self.assertTrue(uri.exists(), f"{uri} should now exist")
        self.assertEqual(uri.read().decode(), content)

    def testRelative(self):
        """Check that we can get subpaths back from two URIs"""
        parent = ButlerURI(self.tmpdir, forceDirectory=True, forceAbsolute=True)
        child = ButlerURI(os.path.join(self.tmpdir, "dir1", "file.txt"), forceAbsolute=True)

        self.assertEqual(child.relative_to(parent), "dir1/file.txt")

        not_child = ButlerURI("/a/b/dir1/file.txt")
        self.assertFalse(not_child.relative_to(parent))

        not_directory = ButlerURI(os.path.join(self.tmpdir, "dir1", "file2.txt"))
        self.assertFalse(child.relative_to(not_directory))

        # Relative URIs
        parent = ButlerURI("a/b/", forceAbsolute=False)
        child = ButlerURI("a/b/c/d.txt", forceAbsolute=False)
        self.assertFalse(child.scheme)
        self.assertEqual(child.relative_to(parent), "c/d.txt")

        # File URI and schemeless URI
        parent = ButlerURI("file:/a/b/c/")
        child = ButlerURI("e/f/g.txt", forceAbsolute=False)

        # If the child is relative and the parent is absolute we assume
        # that the child is a child of the parent unless it uses ".."
        self.assertEqual(child.relative_to(parent), "e/f/g.txt")

        child = ButlerURI("../e/f/g.txt", forceAbsolute=False)
        self.assertFalse(child.relative_to(parent))

        child = ButlerURI("../c/e/f/g.txt", forceAbsolute=False)
        self.assertEqual(child.relative_to(parent), "e/f/g.txt")

    def testMkdir(self):
        tmpdir = ButlerURI(self.tmpdir)
        newdir = tmpdir.join("newdir/seconddir")
        newdir.mkdir()
        self.assertTrue(newdir.exists())
        newfile = newdir.join("temp.txt")
        newfile.write("Data".encode())
        self.assertTrue(newfile.exists())

    def testTransfer(self):
        src = ButlerURI(os.path.join(self.tmpdir, "test.txt"))
        content = "Content is some content\nwith something to say\n\n"
        src.write(content.encode())

        for mode in ("copy", "link", "hardlink", "symlink", "relsymlink"):
            dest = ButlerURI(os.path.join(self.tmpdir, f"dest_{mode}.txt"))
            dest.transfer_from(src, transfer=mode)
            self.assertTrue(dest.exists(), f"Check that {dest} exists (transfer={mode})")

            with open(dest.ospath, "r") as fh:
                new_content = fh.read()
            self.assertEqual(new_content, content)

            if mode in ("symlink", "relsymlink"):
                self.assertTrue(os.path.islink(dest.ospath), f"Check that {dest} is symlink")

            with self.assertRaises(FileExistsError):
                dest.transfer_from(src, transfer=mode)

            dest.transfer_from(src, transfer=mode, overwrite=True)

            os.remove(dest.ospath)

        b = src.read()
        self.assertEqual(b.decode(), new_content)

        nbytes = 10
        subset = src.read(size=nbytes)
        self.assertEqual(len(subset), nbytes)
        self.assertEqual(subset.decode(), content[:nbytes])

        with self.assertRaises(ValueError):
            src.transfer_from(src, transfer="unknown")

    def testResource(self):
        u = ButlerURI("resource://lsst.daf.butler/configs/datastore.yaml")
        self.assertTrue(u.exists(), f"Check {u} exists")

        content = u.read().decode()
        self.assertTrue(content.startswith("datastore:"))

        truncated = u.read(size=9).decode()
        self.assertEqual(truncated, "datastore")

        d = ButlerURI("resource://lsst.daf.butler/configs", forceDirectory=True)
        self.assertTrue(u.exists(), f"Check directory {d} exists")

        j = d.join("datastore.yaml")
        self.assertEqual(u, j)
        self.assertFalse(j.dirLike)
        self.assertFalse(d.join("not-there.yaml").exists())

    def testEscapes(self):
        """Special characters in file paths"""
        src = ButlerURI("bbb/???/test.txt", root=self.tmpdir, forceAbsolute=True)
        self.assertFalse(src.scheme)
        src.write(b"Some content")
        self.assertTrue(src.exists())

        # Use the internal API to force to a file
        file = src._force_to_file()
        self.assertTrue(file.exists())
        self.assertIn("???", file.ospath)
        self.assertNotIn("???", file.path)

        file.updateFile("tests??.txt")
        self.assertNotIn("??.txt", file.path)
        file.write(b"Other content")
        self.assertEqual(file.read(), b"Other content")

        src.updateFile("tests??.txt")
        self.assertIn("??.txt", src.path)
        self.assertEqual(file.read(), src.read(), f"reading from {file.ospath} and {src.ospath}")

        # File URI and schemeless URI
        parent = ButlerURI("file:" + urllib.parse.quote("/a/b/c/de/??/"))
        child = ButlerURI("e/f/g.txt", forceAbsolute=False)
        self.assertEqual(child.relative_to(parent), "e/f/g.txt")

        child = ButlerURI("e/f??#/g.txt", forceAbsolute=False)
        self.assertEqual(child.relative_to(parent), "e/f??#/g.txt")

        child = ButlerURI("file:" + urllib.parse.quote("/a/b/c/de/??/e/f??#/g.txt"))
        self.assertEqual(child.relative_to(parent), "e/f??#/g.txt")

        self.assertEqual(child.relativeToPathRoot, "a/b/c/de/??/e/f??#/g.txt")

        # Schemeless so should not quote
        dir = ButlerURI("bbb/???/", root=self.tmpdir, forceAbsolute=True, forceDirectory=True)
        self.assertIn("???", dir.ospath)
        self.assertIn("???", dir.path)
        self.assertFalse(dir.scheme)

        # dir.join() morphs into a file scheme
        new = dir.join("test_j.txt")
        self.assertIn("???", new.ospath, f"Checking {new}")
        new.write(b"Content")

        new2name = "###/test??.txt"
        new2 = dir.join(new2name)
        self.assertIn("???", new2.ospath)
        new2.write(b"Content")
        self.assertTrue(new2.ospath.endswith(new2name))
        self.assertEqual(new.read(), new2.read())

        fdir = dir._force_to_file()
        self.assertNotIn("???", fdir.path)
        self.assertIn("???", fdir.ospath)
        self.assertEqual(fdir.scheme, "file")
        fnew = dir.join("test_jf.txt")
        fnew.write(b"Content")

        fnew2 = fdir.join(new2name)
        fnew2.write(b"Content")
        self.assertTrue(fnew2.ospath.endswith(new2name))
        self.assertNotIn("###", fnew2.path)

        self.assertEqual(fnew.read(), fnew2.read())

        # Test that children relative to schemeless and file schemes
        # still return the same unquoted name
        self.assertEqual(fnew2.relative_to(fdir), new2name)
        self.assertEqual(fnew2.relative_to(dir), new2name)
        self.assertEqual(new2.relative_to(fdir), new2name, f"{new2} vs {fdir}")
        self.assertEqual(new2.relative_to(dir), new2name)

        # Check for double quoting
        plus_path = "/a/b/c+d/"
        with self.assertLogs(level="WARNING"):
            uri = ButlerURI(urllib.parse.quote(plus_path), forceDirectory=True)
        self.assertEqual(uri.ospath, plus_path)


@unittest.skipIf(not boto3, "Warning: boto3 AWS SDK not found!")
@mock_s3
class S3URITestCase(unittest.TestCase):
    """Tests involving S3"""

    bucketName = "any_bucket"
    """Bucket name to use in tests"""

    def setUp(self):
        # Local test directory
        self.tmpdir = tempfile.mkdtemp()

        # set up some fake credentials if they do not exist
        self.usingDummyCredentials = setAwsEnvCredentials()

        # MOTO needs to know that we expect Bucket bucketname to exist
        s3 = boto3.resource("s3")
        s3.create_bucket(Bucket=self.bucketName)

    def tearDown(self):
        s3 = boto3.resource("s3")
        bucket = s3.Bucket(self.bucketName)
        try:
            bucket.objects.all().delete()
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "404":
                # the key was not reachable - pass
                pass
            else:
                raise

        bucket = s3.Bucket(self.bucketName)
        bucket.delete()

        # unset any potentially set dummy credentials
        if self.usingDummyCredentials:
            unsetAwsEnvCredentials()

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def makeS3Uri(self, path):
        return f"s3://{self.bucketName}/{path}"

    def testTransfer(self):
        src = ButlerURI(os.path.join(self.tmpdir, "test.txt"))
        content = "Content is some content\nwith something to say\n\n"
        src.write(content.encode())

        dest = ButlerURI(self.makeS3Uri("test.txt"))
        self.assertFalse(dest.exists())
        dest.transfer_from(src, transfer="copy")
        self.assertTrue(dest.exists())

        dest2 = ButlerURI(self.makeS3Uri("copied.txt"))
        dest2.transfer_from(dest, transfer="copy")
        self.assertTrue(dest2.exists())

        local = ButlerURI(os.path.join(self.tmpdir, "copied.txt"))
        local.transfer_from(dest2, transfer="copy")
        with open(local.ospath, "r") as fd:
            new_content = fd.read()
        self.assertEqual(new_content, content)

        with self.assertRaises(ValueError):
            dest2.transfer_from(local, transfer="symlink")

        b = dest.read()
        self.assertEqual(b.decode(), new_content)

        nbytes = 10
        subset = dest.read(size=nbytes)
        self.assertEqual(len(subset), nbytes)  # Extra byte comes back
        self.assertEqual(subset.decode(), content[:nbytes])

        with self.assertRaises(FileExistsError):
            dest.transfer_from(src, transfer="copy")

        dest.transfer_from(src, transfer="copy", overwrite=True)

    def testWrite(self):
        s3write = ButlerURI(self.makeS3Uri("created.txt"))
        content = "abcdefghijklmnopqrstuv\n"
        s3write.write(content.encode())
        self.assertEqual(s3write.read().decode(), content)

    def testRelative(self):
        """Check that we can get subpaths back from two URIs"""
        parent = ButlerURI(self.makeS3Uri("rootdir"), forceDirectory=True)
        child = ButlerURI(self.makeS3Uri("rootdir/dir1/file.txt"))

        self.assertEqual(child.relative_to(parent), "dir1/file.txt")

        not_child = ButlerURI(self.makeS3Uri("/a/b/dir1/file.txt"))
        self.assertFalse(not_child.relative_to(parent))

        not_s3 = ButlerURI(os.path.join(self.tmpdir, "dir1", "file2.txt"))
        self.assertFalse(child.relative_to(not_s3))

    def testQuoting(self):
        """Check that quoting works."""
        parent = ButlerURI(self.makeS3Uri("rootdir"), forceDirectory=True)
        subpath = "rootdir/dir1+/file?.txt"
        child = ButlerURI(self.makeS3Uri(urllib.parse.quote(subpath)))

        self.assertEqual(child.relative_to(parent), "dir1+/file?.txt")
        self.assertEqual(child.basename(), "file?.txt")
        self.assertEqual(child.relativeToPathRoot, subpath)
        self.assertIn("%", child.path)
        self.assertEqual(child.unquoted_path, "/" + subpath)


# Mock required environment variables during tests
@unittest.mock.patch.dict(os.environ, {"WEBDAV_AUTH_METHOD": "TOKEN",
                                       "WEBDAV_BEARER_TOKEN": "XXXXXX"})
class WebdavURITestCase(unittest.TestCase):

    def setUp(self):
        serverRoot = "www.not-exists.orgx"
        existingFolderName = "existingFolder"
        existingFileName = "existingFile"
        notExistingFileName = "notExistingFile"

        self.baseURL = ButlerURI(
            f"https://{serverRoot}", forceDirectory=True)
        self.existingFileButlerURI = ButlerURI(
            f"https://{serverRoot}/{existingFolderName}/{existingFileName}")
        self.notExistingFileButlerURI = ButlerURI(
            f"https://{serverRoot}/{existingFolderName}/{notExistingFileName}")
        self.existingFolderButlerURI = ButlerURI(
            f"https://{serverRoot}/{existingFolderName}", forceDirectory=True)
        self.notExistingFolderButlerURI = ButlerURI(
            f"https://{serverRoot}/{notExistingFileName}", forceDirectory=True)

        # Need to declare the options
        responses.add(responses.OPTIONS,
                      self.baseURL.geturl(),
                      status=200, headers={"DAV": "1,2,3"})

        # Used by ButlerHttpURI.exists()
        responses.add(responses.HEAD,
                      self.existingFileButlerURI.geturl(),
                      status=200, headers={'Content-Length': '1024'})
        responses.add(responses.HEAD,
                      self.notExistingFileButlerURI.geturl(),
                      status=404)

        # Used by ButlerHttpURI.read()
        responses.add(responses.GET,
                      self.existingFileButlerURI.geturl(),
                      status=200,
                      body=str.encode("It works!"))
        responses.add(responses.GET,
                      self.notExistingFileButlerURI.geturl(),
                      status=404)

        # Used by ButlerHttpURI.write()
        responses.add(responses.PUT,
                      self.existingFileButlerURI.geturl(),
                      status=200)

        # Used by ButlerHttpURI.transfer_from()
        responses.add(responses.Response(url=self.existingFileButlerURI.geturl(),
                                         method="COPY",
                                         headers={"Destination": self.existingFileButlerURI.geturl()},
                                         status=200))
        responses.add(responses.Response(url=self.existingFileButlerURI.geturl(),
                                         method="COPY",
                                         headers={"Destination": self.notExistingFileButlerURI.geturl()},
                                         status=200))
        responses.add(responses.Response(url=self.existingFileButlerURI.geturl(),
                                         method="MOVE",
                                         headers={"Destination": self.notExistingFileButlerURI.geturl()},
                                         status=200))

        # Used by ButlerHttpURI.remove()
        responses.add(responses.DELETE,
                      self.existingFileButlerURI.geturl(),
                      status=200)
        responses.add(responses.DELETE,
                      self.notExistingFileButlerURI.geturl(),
                      status=404)

        # Used by ButlerHttpURI.mkdir()
        responses.add(responses.HEAD,
                      self.existingFolderButlerURI.geturl(),
                      status=200, headers={'Content-Length': '1024'})
        responses.add(responses.HEAD,
                      self.baseURL.geturl(),
                      status=200, headers={'Content-Length': '1024'})
        responses.add(responses.HEAD,
                      self.notExistingFolderButlerURI.geturl(),
                      status=404)
        responses.add(responses.Response(url=self.notExistingFolderButlerURI.geturl(),
                                         method="MKCOL",
                                         status=201))
        responses.add(responses.Response(url=self.existingFolderButlerURI.geturl(),
                                         method="MKCOL",
                                         status=403))

    @responses.activate
    def testExists(self):

        self.assertTrue(self.existingFileButlerURI.exists())
        self.assertFalse(self.notExistingFileButlerURI.exists())

    @responses.activate
    def testRemove(self):

        self.assertIsNone(self.existingFileButlerURI.remove())
        with self.assertRaises(FileNotFoundError):
            self.notExistingFileButlerURI.remove()

    @responses.activate
    def testMkdir(self):

        # The mock means that we can't check this now exists
        self.notExistingFolderButlerURI.mkdir()

        # This should do nothing
        self.existingFolderButlerURI.mkdir()

        with self.assertRaises(ValueError):
            self.notExistingFileButlerURI.mkdir()

    @responses.activate
    def testRead(self):

        self.assertEqual(self.existingFileButlerURI.read().decode(), "It works!")
        self.assertNotEqual(self.existingFileButlerURI.read().decode(), "Nope.")
        with self.assertRaises(FileNotFoundError):
            self.notExistingFileButlerURI.read()

    @responses.activate
    def testWrite(self):

        self.assertIsNone(self.existingFileButlerURI.write(data=str.encode("Some content.")))
        with self.assertRaises(FileExistsError):
            self.existingFileButlerURI.write(data=str.encode("Some content."), overwrite=False)

    @responses.activate
    def testTransfer(self):

        self.assertIsNone(self.notExistingFileButlerURI.transfer_from(
            src=self.existingFileButlerURI))
        self.assertIsNone(self.notExistingFileButlerURI.transfer_from(
            src=self.existingFileButlerURI,
            transfer="move"))
        with self.assertRaises(FileExistsError):
            self.existingFileButlerURI.transfer_from(src=self.existingFileButlerURI)
        with self.assertRaises(ValueError):
            self.notExistingFileButlerURI.transfer_from(
                src=self.existingFileButlerURI,
                transfer="unsupported")

    def testParent(self):

        self.assertEqual(self.existingFolderButlerURI.geturl(),
                         self.notExistingFileButlerURI.parent().geturl())
        self.assertEqual(self.baseURL.geturl(),
                         self.baseURL.parent().geturl())
        self.assertEqual(self.existingFileButlerURI.parent().geturl(),
                         self.existingFileButlerURI.dirname().geturl())


if __name__ == "__main__":
    unittest.main()
