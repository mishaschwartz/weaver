"""
Based on unittests in https://github.com/wndhydrnt/python-oauth2/tree/master/oauth2/test
"""

import unittest
# noinspection PyPackageRequirements
import mock

from pymongo.collection import Collection
from twitcher.datatype import AccessToken, Service
from twitcher.utils import expires_at
from twitcher.store.mongodb import MongodbTokenStore, MongodbServiceStore


class MongodbTokenStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.access_token = AccessToken(token="abcdef", expires_at=expires_at(hours=1))

    def test_fetch_by_token(self):
        collection_mock = mock.Mock(spec=Collection)
        collection_mock.find_one.return_value = self.access_token

        store = MongodbTokenStore(collection=collection_mock)
        access_token = store.fetch_by_token(token=self.access_token.token)

        collection_mock.find_one.assert_called_with({"token": self.access_token.token})
        assert isinstance(access_token, AccessToken)

    def test_save_token(self):
        collection_mock = mock.Mock(spec=Collection)

        store = MongodbTokenStore(collection=collection_mock)
        store.save_token(self.access_token)

        collection_mock.insert_one.assert_called_with(self.access_token)


class MongodbServiceStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.service = dict(name="loving_flamingo", url="http://somewhere.over.the/ocean", type="wps",
                            public=False, auth='token')
        self.service_public = dict(name="open_pingu", url="http://somewhere.in.the/deep_ocean", type="wps",
                                   public=True, auth='token')
        self.service_special = dict(url="http://wonderload", name="A special Name", type='wps', auth='token')
        self.sane_name_config = {'assert_invalid': False, 'replace_invalid': True}

    def test_fetch_by_name(self):
        collection_mock = mock.Mock(spec=Collection)
        collection_mock.find_one.return_value = self.service
        store = MongodbServiceStore(collection=collection_mock, sane_name_config=self.sane_name_config)
        service = store.fetch_by_name(name=self.service['name'])

        collection_mock.find_one.assert_called_with({"name": self.service['name']})
        assert isinstance(service, dict)

    def test_save_service_default(self):
        collection_mock = mock.Mock(spec=Collection)
        collection_mock.count.return_value = 0
        collection_mock.find_one.return_value = self.service
        store = MongodbServiceStore(collection=collection_mock, sane_name_config=self.sane_name_config)
        store.save_service(Service(self.service))

        collection_mock.insert_one.assert_called_with(self.service)

    def test_save_service_with_special_name(self):
        collection_mock = mock.Mock(spec=Collection)
        collection_mock.count.return_value = 0
        collection_mock.find_one.return_value = self.service_special
        store = MongodbServiceStore(collection=collection_mock, sane_name_config=self.sane_name_config)
        store.save_service(Service(self.service_special))

        collection_mock.insert_one.assert_called_with({
            'url': 'http://wonderload', 'type': 'wps', 'name': 'a_special_name', 'public': False, 'auth': 'token'})

    def test_save_service_public(self):
        collection_mock = mock.Mock(spec=Collection)
        collection_mock.count.return_value = 0
        collection_mock.find_one.return_value = self.service_public
        store = MongodbServiceStore(collection=collection_mock, sane_name_config=self.sane_name_config)
        store.save_service(Service(self.service_public))

        collection_mock.insert_one.assert_called_with(self.service_public)
