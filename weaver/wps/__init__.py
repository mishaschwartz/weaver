#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Views for WPS-XML endpoint implemented with :mod:`pywps`.
"""
import logging

from cornice.service import Service
from pyramid.settings import asbool

from weaver.formats import OUTPUT_FORMAT_XML
from weaver.utils import get_settings
from weaver.wps.utils import get_wps_path
from weaver.wps_restapi import swagger_definitions as sd


def includeme(config):
    from weaver.wps.app import pywps_view

    settings = get_settings(config)
    logger = logging.getLogger(__name__)
    if not asbool(settings.get("weaver.wps", True)):
        logger.debug("Weaver WPS disable. WPS KVP/XML endpoint will not be available.")
    else:
        logger.debug("Weaver WPS enabled.")
        wps_path = get_wps_path(settings)
        wps_service = Service(name="wps", path=wps_path)
        logger.debug("Adding WPS KVP/XML schemas.")
        wps_tags = [sd.TAG_GETCAPABILITIES, sd.TAG_DESCRIBEPROCESS, sd.TAG_EXECUTE, sd.TAG_WPS]
        wps_service.add_view("GET", pywps_view, tags=wps_tags, renderer=OUTPUT_FORMAT_XML,
                             schema=sd.WPSEndpoint(), response_schemas=sd.wps_responses)
        wps_service.add_view("POST", pywps_view, tags=wps_tags, renderer=OUTPUT_FORMAT_XML,
                             schema=sd.WPSEndpoint(), response_schemas=sd.wps_responses)
        logger.debug("Adding WPS KVP/XML view.")
        config.add_route(**sd.service_api_route_info(wps_service, settings))
        config.add_view(pywps_view, route_name=wps_service.name)
