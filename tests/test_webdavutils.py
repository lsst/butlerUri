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

import unittest

import requests
import responses
from lsst.resources.http import finalurl, isWebdavEndpoint


class WebdavUtilsTestCase(unittest.TestCase):
    """Test for the Webdav related utilities."""

    session = requests.Session()
    serverRoot = "www.lsstwithwebdav.orgx"
    wrongRoot = "www.lsstwithoutwebdav.org"
    existingfolderName = "testFolder"
    notExistingfolderName = "testFolder_not_exist"
    existingfileName = "testFileName"
    notExistingfileName = "testFileName_not_exist"

    def setUp(self):
        # Used by isWebdavEndpoint()
        responses.add(responses.OPTIONS, f"https://{self.serverRoot}", status=200, headers={"DAV": "1,2,3"})
        responses.add(responses.OPTIONS, f"https://{self.wrongRoot}", status=200)

        # Use by finalurl()
        # Without redirection
        responses.add(
            responses.PUT,
            f"https://{self.serverRoot}/{self.existingfolderName}/{self.existingfileName}",
            status=200,
        )
        # With redirection
        responses.add(
            responses.PUT,
            f"https://{self.wrongRoot}/{self.existingfolderName}/{self.existingfileName}",
            headers={
                "Location": f"https://{self.serverRoot}/{self.existingfolderName}/{self.existingfileName}"
            },
            status=307,
        )

    @responses.activate
    def testIsWebdavEndpoint(self):

        self.assertTrue(isWebdavEndpoint(f"https://{self.serverRoot}"))
        self.assertFalse(isWebdavEndpoint(f"https://{self.wrongRoot}"))

    @responses.activate
    def testFinalurl(self):
        s = f"https://{self.serverRoot}/{self.existingfolderName}/{self.existingfileName}"
        r = f"https://{self.wrongRoot}/{self.existingfolderName}/{self.existingfileName}"

        resp_s = self.session.put(s)
        resp_r = self.session.put(r)

        self.assertEqual(finalurl(resp_s), s)
        self.assertEqual(finalurl(resp_r), s)


if __name__ == "__main__":
    unittest.main()
