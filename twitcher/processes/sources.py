import os
import json
from typing import Union, Text
from six.moves.urllib.parse import urlparse
from pyramid.settings import asbool
from pyramid_celery import celery_app as app
from twitcher import TWITCHER_ROOT_DIR
from twitcher.wps_restapi.utils import wps_restapi_base_url

# Data source cache
from twitcher.processes.wps_process import OPENSEARCH_LOCAL_FILE_SCHEME

"""
Schema

{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Data Sources",
  "type": "object",
  "patternProperties": {
    ".*": {
      "type": "object",
      "required": [ "netloc", "ades" ],
      "additionalProperties": false,
      "properties": {
        "netloc": {
          "type": "string",
          "description": "Net location of a data source url use to match this data source."
        },
        "ades": {
          "type": "string",
          "description": "ADES endpoint where the processing of this data source can occur."
        },
        "default": {
          "type": "string",
          "description": "True indicate that if no data source match this one should be used (Use the first default)."
        }
      }
    }
  }
}
"""

DATA_SOURCES = {}


def fetch_data_sources():
    global DATA_SOURCES

    if DATA_SOURCES:
        return DATA_SOURCES

    registry = app.conf['PYRAMID_REGISTRY']
    data_source_cfg = registry.settings.get('twitcher.data_sources', None)
    if data_source_cfg:
        if not os.path.isabs(data_source_cfg):
            data_source_cfg = os.path.normpath(os.path.join(TWITCHER_ROOT_DIR, data_source_cfg))
        try:
            with open(data_source_cfg) as f:
                DATA_SOURCES = json.load(f)
        except Exception as exc:
            raise ValueError("Data sources file {0} cannot be loaded properly : {1}".format(data_source_cfg,
                                                                                            exc.message))
    if not DATA_SOURCES:
        raise ValueError("No data sources found in setting 'twitcher.data_sources'")
    return DATA_SOURCES


def get_default_data_source(data_sources):
    # Check for a data source with the default property
    for src, val in data_sources.items():
        if asbool(val.get('default', False)):
            return src

    # Use the first one if no default have been set
    return next(iter(data_sources))


def get_local_data_source():
    registry = app.conf['PYRAMID_REGISTRY']
    return wps_restapi_base_url(registry.settings)


def retrieve_data_source_url(data_source):
    # type: (Union[Text, None]) -> Text
    """Finds the data source URL using the provided data source identifier.
    :returns: found URL, 'default' data source if not found, or current Twitcher WPS Rest API base URL if `None`."""
    if data_source is None:
        return get_local_data_source()
    data_sources = fetch_data_sources()
    return data_sources[data_source if data_source in data_sources else get_default_data_source(data_sources)]['ades']


def get_data_source_from_url(data_url):
    data_sources = fetch_data_sources()
    try:
        parsed = urlparse(data_url)
        netloc, path, scheme = parsed.netloc, parsed.path, parsed.scheme
        if netloc:
            for src, val in data_sources.items():
                if val['netloc'] == netloc:
                    return src
        elif scheme == OPENSEARCH_LOCAL_FILE_SCHEME:
            # for file links, try to find if any rootdir matches in the file path
            for src, val in data_sources.items():
                if path.startswith(val['rootdir']):
                    return src

    # noinspection PyBroadException
    except Exception:
        pass
    return get_default_data_source(data_sources)
