from pyramid.view import view_config
from twitcher.adapter import servicestore_factory
from owslib.wps import WebProcessingService
from twitcher.datatype import Service
from twitcher.wps_restapi.utils import restapi_base_url


def get_providers(request):
    """
    Lists providers
    """
    store = servicestore_factory(request.registry, headers=request.headers)
    providers = []

    # TODO Filter by permissions (public / private)
    for service in store.list_services():
        try:
            wps = WebProcessingService(url=service.url)
            providers.append(dict(
                id=service.name,
                title=getattr(wps.identification, 'title', ''),
                abstract=getattr(wps.identification, 'abstract', ''),
                url='{base_url}/providers/{provider_id}'.format(
                    base_url=restapi_base_url(request),
                    provider_id=service.name),
                public=service.public))
        except Exception as e:
            pass

    return providers


def add_provider(request):
    """
    Add a provider
    """
    store = servicestore_factory(request.registry, headers=request.headers)

    # TODO Validate that params have at least a url and a name
    new_service = Service(url=request.json.url, name=request.json.id)
    if hasattr(request.json, 'public'):
        new_service.public = request.json.public
    if hasattr(request.json, 'auth'):
        new_service.auth = request.json.auth
    store.save_service(new_service)

    return {}


def remove_provider(request):
    """
    Remove a provider
    """
    store = servicestore_factory(request.registry, headers=request.headers)

    # TODO Validate param somehow
    provider_id = request.matchdict.get('provider_id')

    # TODO Exception handling in json please
    store.delete_service(provider_id)

    return {}


def get_capabilities(request):
    """
    GetCapabilities of a wps provider
    """

    store = servicestore_factory(request.registry, headers=request.headers)

    # TODO Validate param somehow
    provider_id = request.matchdict.get('provider_id')

    service = store.fetch_by_name(provider_id)
    wps = WebProcessingService(url=service.url)

    return dict(
        id=provider_id,
        title=wps.identification.title,
        abstract=wps.identification.abstract,
        url='{base_url}/providers/{provider_id}'.format(
                base_url=restapi_base_url(request),
                provider_id=provider_id),
        type='WPS',
        contact=wps.provider.contact.name)