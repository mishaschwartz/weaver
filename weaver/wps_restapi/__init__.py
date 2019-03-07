from pyramid.settings import asbool
from weaver.wps_restapi import api
import logging
logger = logging.getLogger(__name__)


def includeme(config):
    from weaver.wps_restapi import swagger_definitions as sd
    settings = config.registry.settings
    if asbool(settings.get('weaver.wps_restapi', True)):
        logger.info('Adding WPS REST API...')
        config.registry.settings['handle_exceptions'] = False  # avoid cornice conflicting views
        config.include('cornice')
        config.include('cornice_swagger')
        config.include('weaver.wps_restapi.jobs')
        config.include('weaver.wps_restapi.providers')
        config.include('weaver.wps_restapi.processes')
        config.include('weaver.wps_restapi.quotation')
        config.include('pyramid_mako')
        config.add_route(**sd.service_api_route_info(sd.api_frontpage_service, settings))
        config.add_route(**sd.service_api_route_info(sd.api_swagger_json_service, settings))
        config.add_route(**sd.service_api_route_info(sd.api_swagger_ui_service, settings))
        config.add_route(**sd.service_api_route_info(sd.api_versions_service, settings))
        config.add_view(api.api_frontpage, route_name=sd.api_frontpage_service.name,
                        request_method='GET', renderer='json')
        config.add_view(api.api_swagger_json, route_name=sd.api_swagger_json_service.name,
                        request_method='GET', renderer='json')
        config.add_view(api.api_swagger_ui, route_name=sd.api_swagger_ui_service.name,
                        request_method='GET', renderer='templates/swagger_ui.mako')
        config.add_view(api.api_versions, route_name=sd.api_versions_service.name,
                        request_method='GET', renderer='json')
        config.add_notfound_view(api.not_found_or_method_not_allowed)
        config.add_forbidden_view(api.unauthorized_or_forbidden)