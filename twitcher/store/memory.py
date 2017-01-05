"""
Read or write data from or to local memory.

Though not very valuable in a production setup, these store adapters are great
for testing purposes.
"""

from twitcher.store.base import AccessTokenStore
from twitcher.exceptions import AccessTokenNotFound


class MemoryTokenStore(AccessTokenStore):
    """
    Stores tokens in memory.
    Useful for testing purposes or APIs with a very limited set of clients.

    Use mongodb as storage to be able to scale.
    """
    def __init__(self):
        self.access_tokens = {}

    def save_token(self, access_token):
        self.access_tokens[access_token.token] = access_token
        return True

    def delete_token(self, token):
        if token in self.access_tokens:
            del self.access_tokens[token]

    def fetch_by_token(self, token):
        if token not in self.access_tokens:
            raise AccessTokenNotFound

        return self.access_tokens[token]

    def clean_tokens(self):
        self.access_tokens = {}

from twitcher.store.base import ServiceRegistryStore
from twitcher.datatype import doc2dict
from twitcher.exceptions import ServiceRegistrationError
from twitcher import namesgenerator
from twitcher.utils import parse_service_name
from twitcher.utils import baseurl


class MemoryRegistryStore(ServiceRegistryStore):
    """
    Stores OWS services in memory. Useful for testing purposes.
    """
    def __init__(self):
        self.url_index = {}
        self.name_index = {}

    def _delete(url=None, name=None):
        if url:
            service = self.url_index[url]
            del self.name_index[service['name']]
            del self.url_index[url]
        elif name:
            service = self.name_index[name]
            del self.url_index[service['url']]
            del self.name_index[name]

    def _insert(service):
        self.name_index[service['name']] = service
        self.url_index[service['url']] = service

    def register_service(self, url, name=None, service_type='wps', public=False, c4i=False, overwrite=True):
        """
        Adds OWS service with given name to registry database.
        """

        service_url = baseurl(url)
        # check if service is already registered
        if service_url in self.service_url_index:
            if overwrite:
                self._delete(url=service_url)
            else:
                raise ServiceRegistrationError("service url already registered.")

        name = namesgenerator.get_sane_name(name)
        if not name:
            name = namesgenerator.get_random_name()
            if name in self.name_index:
                name = namesgenerator.get_random_name(retry=True)
        if name in self.name_index:
            if overwrite:
                self._delete(name=name)
            else:
                raise Exception("service name already registered.")
        service = dict(url=service_url, name=name, type=service_type, public=public, c4i=c4i)
        self._insert(service)
        return self.get_service_by_url(url=service_url)

    def unregister_service(self, name):
        """
        Removes service from registry database.
        """
        self._delete(name=name)

    def list_services(self):
        """
        Lists all services in registry database.
        """
        my_services = []
        for service in self.url_index.itervalues():
            my_services.append({
                'name': service['name'],
                'type': service['type'],
                'url': service['url'],
                'public': service.get('public', False),
                'c4i': service.get('c4i', False)})
        return my_services

    def get_service_by_name(self, name):
        """
        Get service for given ``name`` from registry database.
        """
        service = self.name_index.get(name)
        if service is None:
            raise ValueError('service not found')
        if 'url' not in service:
            raise ValueError('service has no url')
        return doc2dict(service)

    def get_service_by_url(self, url):
        """
        Get service for given ``url`` from registry database.
        """
        service = self.url_index(baseurl(url))
        if not service:
            raise ValueError('service not found')
        return doc2dict(service)

    def get_service_name(self, url):
        try:
            service_name = parse_service_name(url)
        except ValueError:
            service = self.get_service_by_url(url)
            service_name = service['name']
        return service_name

    def is_public(self, name):
        try:
            service = self.get_service_by_name(name)
            public = service.get('public', False)
        except ValueError:
            public = False
        return public

    def clear_services(self):
        """
        Removes all OWS services from registry database.
        """
        self.url_index = {}
        self.name_index = {}
