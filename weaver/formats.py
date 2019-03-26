from typing import TYPE_CHECKING
from six.moves.urllib.request import urlopen
from six.moves.urllib.error import HTTPError
import os
if TYPE_CHECKING:
    from weaver.typedefs import JSON
    from typing import AnyStr, Optional, Tuple, Union

# Content-Types
CONTENT_TYPE_APP_FORM = "application/x-www-form-urlencoded"
CONTENT_TYPE_APP_NETCDF = "application/x-netcdf"
CONTENT_TYPE_APP_HDF5 = "application/x-hdf5"
CONTENT_TYPE_TEXT_HTML = "text/html"
CONTENT_TYPE_TEXT_PLAIN = "text/plain"
CONTENT_TYPE_APP_JSON = "application/json"
CONTENT_TYPE_APP_XML = "application/xml"
CONTENT_TYPE_TEXT_XML = "text/xml"
CONTENT_TYPE_ANY_XML = {CONTENT_TYPE_APP_XML, CONTENT_TYPE_TEXT_XML}

CONTENT_TYPE_EXTENSION_MAPPING = {
    CONTENT_TYPE_APP_NETCDF: "nc",
    CONTENT_TYPE_APP_HDF5: "hdf5",
    CONTENT_TYPE_TEXT_PLAIN: "*",   # any for glob
}


def get_extension(mime_type):
    # type: (AnyStr) -> AnyStr
    """Retrieves the extension corresponding to ``mime_type`` if explicitly defined, or bt simple parsing otherwise."""
    return CONTENT_TYPE_EXTENSION_MAPPING.get(mime_type, mime_type.split('/')[-1])


# Mappings for "CWL->File->Format" (IANA corresponding Content-Type)
# search:
#   - IANA: https://www.iana.org/assignments/media-types/media-types.xhtml
#   - EDAM: https://www.ebi.ac.uk/ols/search
# IANA contains most standard MIME-types, but might not include special (application/x-hdf5, application/x-netcdf, etc.)
IANA_NAMESPACE = "iana"
IANA_NAMESPACE_DEFINITION = {IANA_NAMESPACE: "https://www.iana.org/assignments/media-types/"}
EDAM_NAMESPACE = "edam"
EDAM_NAMESPACE_DEFINITION = {EDAM_NAMESPACE: "http://edamontology.org/"}
EDAM_SCHEMA = "http://edamontology.org/EDAM_1.21.owl"
EDAM_MAPPING = {
    CONTENT_TYPE_APP_HDF5: "format_3590",
    CONTENT_TYPE_APP_JSON: "format_3464",
    CONTENT_TYPE_APP_NETCDF: "format_3650",
    CONTENT_TYPE_TEXT_PLAIN: "format_1964",
}
FORMAT_NAMESPACES = frozenset([IANA_NAMESPACE, EDAM_NAMESPACE])


def get_cwl_file_format(mime_type, make_reference=False):
    # type: (AnyStr, Optional[bool]) -> Union[Tuple[Union[JSON, None], Union[AnyStr, None]], Union[AnyStr, None]]
    """
    Obtains the corresponding IANA/EDAM ``format`` value to be applied under a CWL I/O ``File`` from the
    ``mime_type`` (`Content-Type` header) using the first matched one.

    If there is a match, returns ``tuple(dict<namespace-name: namespace-url>, <format>)``:
        - corresponding namespace mapping to be applied under ``$namespaces`` in the `CWL`.
        - value of ``format`` adjusted according to the namespace to be applied to ``File`` in the `CWL`.
    Otherwise, returns ``(None, None)``

    If ``make_reference=True``, the explicit format reference as ``<namespace-url>/<format>`` is returned instead
    of the ``tuple``. If ``make_reference=True`` and ``mime_type`` cannot be matched, a single ``None`` is returned.
    """
    def _make_if_ref(_map, _key, _fmt):
        return os.path.join(_map[_key], _fmt) if make_reference else (_map, "{}:{}".format(_key, _fmt))

    # FIXME: ConnectionRefused with `requests.get`, using `urllib` instead
    try:
        mime_type_url = "{}{}".format(IANA_NAMESPACE_DEFINITION[IANA_NAMESPACE], mime_type)
        resp = urlopen(mime_type_url)   # 404 on not implemented/referenced mime-type
        if resp.code == 200:
            return _make_if_ref(IANA_NAMESPACE_DEFINITION, IANA_NAMESPACE, mime_type)
    except HTTPError:
        pass
    if mime_type in EDAM_MAPPING:
        return _make_if_ref(EDAM_NAMESPACE_DEFINITION, EDAM_NAMESPACE, EDAM_MAPPING[mime_type])
    return None if make_reference else (None, None)


def clean_mime_type_format(mime_type):
    # type: (AnyStr) -> AnyStr
    """
    Removes any additional namespace key or URL from ``mime_type`` so that it corresponds to the generic
    representation (ex: `application/json`) instead of the ``<namespace-name>:<format>`` variant used
    in `CWL->inputs/outputs->File->format`.
    """
    for v in IANA_NAMESPACE_DEFINITION.values() + EDAM_NAMESPACE_DEFINITION.values():
        if v in mime_type:
            mime_type = mime_type.replace(v, "")
    for v in IANA_NAMESPACE_DEFINITION.keys() + EDAM_NAMESPACE_DEFINITION.keys():
        if mime_type.startswith(v + ":"):
            mime_type = mime_type.replace(v + ":", "")
    for v in EDAM_MAPPING.values():
        if v.endswith(mime_type):
            mime_type = [k for k in EDAM_MAPPING if v.endswith(EDAM_MAPPING[k])][0]
    return mime_type