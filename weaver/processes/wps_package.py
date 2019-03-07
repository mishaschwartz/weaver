from weaver.processes import opensearch
from weaver.config import get_weaver_configuration, WEAVER_CONFIGURATION_EMS
from weaver.processes.constants import WPS_INPUT, WPS_OUTPUT, WPS_COMPLEX, WPS_BOUNDINGBOX, WPS_LITERAL, WPS_REFERENCE
from weaver.processes.wps1_process import Wps1Process
from weaver.processes.wps3_process import Wps3Process
from weaver.processes.wps_workflow import default_make_tool
from weaver.processes.types import PROCESS_APPLICATION, PROCESS_WORKFLOW
from weaver.processes.sources import retrieve_data_source_url
from weaver.exceptions import (
    PackageTypeError, PackageRegistrationError, PackageExecutionError,
    PackageNotFound, PayloadNotFound
)
from weaver.status import (STATUS_RUNNING, STATUS_SUCCEEDED, STATUS_EXCEPTION, STATUS_FAILED,
                           map_status, STATUS_COMPLIANT_PYWPS, STATUS_PYWPS_IDS)
from weaver.wps_restapi.swagger_definitions import process_uri
from weaver.utils import get_job_log_msg, get_log_fmt, get_log_datefmt, get_sane_name
from pywps.inout.basic import BasicIO
from pywps.inout.literaltypes import AnyValue, AllowedValue, ALLOWEDVALUETYPE
from pywps.validator.mode import MODE
from pywps.app.Common import Metadata
from pyramid.httpexceptions import HTTPOk
from pyramid_celery import celery_app as app
from collections import OrderedDict, Hashable
from six.moves.urllib.parse import urlparse
from typing import Dict, Tuple, Union, Any, Optional, AnyStr, List, Callable, TYPE_CHECKING
from yaml import safe_load
from yaml.scanner import ScannerError
from cwltool.context import LoadingContext
from cwltool.context import RuntimeContext
import cwltool.factory
import cwltool
import json
import tempfile
import shutil
import requests
import logging
import os
import six
from pywps import (
    Process,
    LiteralInput,
    LiteralOutput,
    ComplexInput,
    ComplexOutput,
    BoundingBoxInput,
    BoundingBoxOutput,
    Format,
)
if TYPE_CHECKING:
    from weaver.status import AnyStatusType, ToolPathObjectType
    from cwltool.process import Process as ProcessCWL

LOGGER = logging.getLogger("PACKAGE")

__all__ = [
    'WpsPackage',
    'get_process_from_wps_request',
    'get_process_location',
    'get_package_workflow_steps',
]

PACKAGE_EXTENSIONS = frozenset(['yaml', 'yml', 'json', 'cwl', 'job'])
PACKAGE_BASE_TYPES = frozenset(['string', 'boolean', 'float', 'int', 'integer', 'long', 'double'])
PACKAGE_LITERAL_TYPES = frozenset(list(PACKAGE_BASE_TYPES) + ['null', 'Any'])
PACKAGE_COMPLEX_TYPES = frozenset(['File', 'Directory'])
PACKAGE_ARRAY_BASE = 'array'
PACKAGE_ARRAY_MAX_SIZE = six.MAXSIZE  # pywps doesn't allow None, so use max size
PACKAGE_ARRAY_ITEMS = frozenset(list(PACKAGE_BASE_TYPES) + list(PACKAGE_COMPLEX_TYPES))
PACKAGE_ARRAY_TYPES = frozenset(['{}[]'.format(item) for item in PACKAGE_ARRAY_ITEMS])
PACKAGE_CUSTOM_TYPES = frozenset(['enum'])  # can be anything, but support 'enum' which is more common
PACKAGE_DEFAULT_FILE_NAME = 'package'
PACKAGE_LOG_FILE = 'package_log_file'

PACKAGE_PROGRESS_PREP_LOG = 1
PACKAGE_PROGRESS_LAUNCHING = 2
PACKAGE_PROGRESS_LOADING = 5
PACKAGE_PROGRESS_GET_INPUT = 6
PACKAGE_PROGRESS_CONVERT_INPUT = 8
PACKAGE_PROGRESS_RUN_CWL = 10
PACKAGE_PROGRESS_CWL_DONE = 95
PACKAGE_PROGRESS_PREP_OUT = 98
PACKAGE_PROGRESS_DONE = 100

# WPS object attribute -> all possible naming variations
WPS_FIELD_MAPPING = {
    'identifier': ['Identifier', 'ID', 'id', 'Id'],
    'title': ['Title'],
    'abstract': ['Abstract'],
    'metadata': ['Metadata', 'MetaData'],
    'keywords': ['Keywords'],
    'allowed_values': ['AllowedValues', 'allowedValues', 'allowedvalues', 'Allowed_Values', 'Allowedvalues'],
    'allowed_collections': ['AllowedCollections', 'allowedCollections', 'allowedcollections', 'Allowed_Collections',
                            'Allowedcollections'],
    'supported_formats': ['SupportedFormats', 'supportedFormats', 'supportedformats', 'Supported_Formats'],
    'additional_parameters': ['AdditionalParameters', 'additionalParameters', 'additionalparameters',
                              'Additional_Parameters'],
}


# typing shortcuts
wps_input_type = Union[LiteralInput, ComplexInput, BoundingBoxInput]
wps_output_type = Union[LiteralOutput, ComplexOutput, BoundingBoxOutput]
wps_io_type = Union[wps_input_type, wps_output_type]
any_key_type = Union[AnyStr, int]
any_io_type = Dict[any_key_type, Any]
cwl_input_type = any_io_type
cwl_output_type = any_io_type
cwl_io_type = Union[cwl_input_type, cwl_output_type]

# default format if missing (minimal requirement of one)
DefaultFormat = Format(mime_type='text/plain')


# noinspection PyClassHasNoInit
class NullType:
    pass


null = NullType()


def get_process_location(process_id_or_url, data_source=None):
    # type: (Union[Dict[AnyStr, Any], AnyStr], Optional[AnyStr]) -> AnyStr
    """
    Obtains the URL of a WPS REST DescribeProcess given the specified information.

    :param process_id_or_url: process 'identifier' or literal URL to DescribeProcess WPS-REST location.
    :param data_source: identifier of the data source to map to specific ADES, or map to localhost if ``None``.
    :return: URL of EMS or ADES WPS-REST DescribeProcess.
    """
    # if an URL was specified, return it as is
    if urlparse(process_id_or_url).scheme != "":
        return process_id_or_url
    data_source_url = retrieve_data_source_url(data_source)
    process_id = get_sane_name(process_id_or_url)
    process_url = process_uri.format(process_id=process_id)
    return '{host}{path}'.format(host=data_source_url, path=process_url)


def get_package_workflow_steps(package_dict_or_url):
    # type: (Union[Dict[AnyStr, Any], AnyStr]) -> List[Dict[AnyStr, AnyStr]]
    """
    :param package_dict_or_url: process package definition or literal URL to DescribeProcess WPS-REST location.
    :return: list of workflow steps as {'name': <name>, 'reference': <reference>}
        where `name` is the generic package step name, and `reference` is the id/url of a registered WPS package.
    """
    if isinstance(package_dict_or_url, six.string_types):
        package_dict_or_url = _get_process_package(package_dict_or_url)
    workflow_steps_ids = list()
    package_type = _get_package_type(package_dict_or_url)
    if package_type == PROCESS_WORKFLOW:
        workflow_steps = package_dict_or_url.get('steps')
        for step in workflow_steps:
            step_package_ref = workflow_steps[step].get('run')
            # if a local file reference was specified, convert it to process id
            if urlparse(step_package_ref).scheme == "" and step_package_ref.endswith('.cwl'):
                step_package_ref = step_package_ref[:-4]

            workflow_steps_ids.append({'name': step, 'reference': step_package_ref})
    return workflow_steps_ids


def _get_process_package(process_url):
    # type: (AnyStr) -> Tuple[Dict[AnyStr, Any], AnyStr]
    """
    Retrieves the WPS process package content from given process ID or literal URL.

    :param process_url: process literal URL to DescribeProcess WPS-REST location.
    :return: tuple of package body as dictionary and package reference name.
    """

    def _package_not_found_error(ref):
        return PackageNotFound("Could not find workflow step reference: `{}`".format(ref))

    if not isinstance(process_url, six.string_types):
        raise _package_not_found_error(str(process_url))

    package_url = '{}/package'.format(process_url)
    package_name = process_url.split('/')[-1]
    package_resp = requests.get(package_url, headers={'Accept': 'application/json'}, verify=False)
    if package_resp.status_code != HTTPOk.code:
        raise _package_not_found_error(package_url)
    package_body = package_resp.json()

    if not isinstance(package_body, dict) or not len(package_body):
        raise _package_not_found_error(str(process_url))

    return package_body, package_name


def _get_process_payload(process_url):
    # type: (AnyStr) -> Dict[AnyStr, Any]
    """
    Retrieves the WPS process payload content from given process ID or literal URL.

    :param process_url: process literal URL to DescribeProcess WPS-REST location.
    :return: payload body as dictionary.
    """

    def _payload_not_found_error(ref):
        return PayloadNotFound("Could not find workflow step reference: `{}`".format(ref))

    if not isinstance(process_url, six.string_types):
        raise _payload_not_found_error(str(process_url))

    payload_url = '{}/payload'.format(process_url)
    payload_resp = requests.get(payload_url, headers={'Accept': 'application/json'}, verify=False)
    if payload_resp.status_code != HTTPOk.code:
        raise _payload_not_found_error(payload_url)
    payload_body = payload_resp.json()

    if not isinstance(payload_body, dict) or not len(payload_body):
        raise _payload_not_found_error(str(process_url))

    return payload_body


def _get_package_type(package_dict):
    # type: (Dict[AnyStr, AnyStr]) -> Union[PROCESS_APPLICATION, PROCESS_WORKFLOW]
    return PROCESS_WORKFLOW if package_dict.get('class').lower() == 'workflow' else PROCESS_APPLICATION


def _check_package_file(cwl_file_path_or_url):
    # type: (AnyStr) -> Tuple[AnyStr, bool]
    """
    Validates that the specified CWL file path or URL points to an existing and allowed file format.
    :param cwl_file_path_or_url: one of allowed file types path on disk, or an URL pointing to one served somewhere.
    :return: absolute_path, is_url: absolute path or URL, and boolean indicating if it is a remote URL file.
    :raises: PackageRegistrationError in case of missing file, invalid format or invalid HTTP status code.
    """
    is_url = False
    if urlparse(cwl_file_path_or_url).scheme != "":
        cwl_path = cwl_file_path_or_url
        cwl_resp = requests.head(cwl_path)
        is_url = True
        if cwl_resp.status_code != HTTPOk.code:
            raise PackageRegistrationError("Cannot find CWL file at: `{}`.".format(cwl_path))
    else:
        cwl_path = os.path.abspath(cwl_file_path_or_url)
        if not os.path.isfile(cwl_path):
            raise PackageRegistrationError("Cannot find CWL file at: `{}`.".format(cwl_path))

    file_ext = os.path.splitext(cwl_path)[1].replace('.', '')
    if file_ext not in PACKAGE_EXTENSIONS:
        raise PackageRegistrationError("Not a valid CWL file type: `{}`.".format(file_ext))
    return cwl_path, is_url


def _load_package_file(file_path):
    # type: (AnyStr) -> Dict[AnyStr, Any]
    """Loads the package in YAML/JSON format specified by the file path."""

    file_path, is_url = _check_package_file(file_path)
    # if URL, get the content and validate it by loading, otherwise load file directly
    # yaml properly loads json as well, error can print out the parsing error location
    try:
        if is_url:
            cwl_resp = requests.get(file_path, headers={'Accept': 'text/plain'})
            return safe_load(cwl_resp.content)
        with open(file_path, 'r') as f:
            return safe_load(f)
    except ScannerError as ex:
        raise PackageRegistrationError("Package parsing generated an error: [{!s}]".format(ex))


def _load_package_content(package_dict,                             # type: Dict
                          package_name=PACKAGE_DEFAULT_FILE_NAME,   # type: Optional[AnyStr]
                          data_source=None,                         # type: Optional[AnyStr]
                          only_dump_file=False,                     # type: Optional[bool]
                          tmp_dir=None,                             # type: Optional[AnyStr]
                          loading_context=None,                     # type: Optional[LoadingContext]
                          runtime_context=None,                     # type: Optional[RuntimeContext]
                          ):  # type: (...) -> Union[Tuple[cwltool.factory.Factory, AnyStr, Dict], None]
    """
    Loads the package content to file in a temporary directory.
    Recursively processes sub-packages steps if the parent is of 'workflow' type (CWL class).

    :param package_dict: package content representation as a json dictionary.
    :param package_name: name to use to create the package file.
    :param data_source: identifier of the data source to map to specific ADES, or map to localhost if ``None``.
    :param only_dump_file: specify if the :class:`cwltool.factory.Factory` should be validated and returned.
    :param tmp_dir: location of the temporary directory to dump files (warning: will be deleted on exit).
    :param loading_context: cwltool context use to make the cwl package
    :param runtime_context: cwltool context use to make the cwl package
    :return:
        tuple of
        - instance of :class:`cwltool.factory.Factory`
        - package type (PROCESS_WORKFLOW or PROCESS_APPLICATION)
        - dict of each step with their package name that must be run
        if :param:`only_dump_file` is ``False``, ``None`` otherwise.
    """

    tmp_dir = tmp_dir or tempfile.mkdtemp()
    tmp_json_cwl = os.path.join(tmp_dir, package_name)

    # for workflows, retrieve each 'sub-package' file
    package_type = _get_package_type(package_dict)
    workflow_steps = get_package_workflow_steps(package_dict)
    step_packages = {}
    for step in workflow_steps:
        # generate sub-package file and update workflow step to point to created sub-package file
        step_process_url = get_process_location(step['reference'], data_source)
        package_body, package_name = _get_process_package(step_process_url)
        _load_package_content(package_body, package_name, data_source=data_source, only_dump_file=True, tmp_dir=tmp_dir)
        package_dict['steps'][step['name']]['run'] = package_name
        step_packages[step['name']] = package_name

    with open(tmp_json_cwl, 'w') as f:
        json.dump(package_dict, f)
    if only_dump_file:
        return

    cwl_factory = cwltool.factory.Factory(loading_context=loading_context, runtime_context=runtime_context)
    package = cwl_factory.make(tmp_json_cwl)
    shutil.rmtree(tmp_dir)
    return package, package_type, step_packages


def _is_cwl_array_type(io_info):
    # type: (any_io_type) -> Tuple[bool, AnyStr]
    """Verifies if the specified input/output corresponds to one of various CWL array type definitions.

    :return is_array: specifies if the input/output is of array type
    :return io_type: array element type if ``is_array`` is True, type of ``io_info`` otherwise.
    :raises PackageTypeError: if the array element is not supported.
    """
    is_array = False
    io_type = io_info['type']

    # array type conversion when defined as dict of {'type': 'array', 'items': '<type>'}
    # validate against Hashable instead of 'dict' since 'OrderedDict'/'CommentedMap' can result in `isinstance()==False`
    if not isinstance(io_type, six.string_types) and not isinstance(io_type, Hashable) \
            and 'items' in io_type and 'type' in io_type:
        if not io_type['type'] == PACKAGE_ARRAY_BASE or io_type['items'] not in PACKAGE_ARRAY_ITEMS:
            raise PackageTypeError("Unsupported I/O 'array' definition: `{}`.".format(repr(io_info)))
        io_type = io_type['items']
        is_array = True
    # array type conversion when defined as string '<type>[]'
    elif isinstance(io_type, six.string_types) and io_type in PACKAGE_ARRAY_TYPES:
        io_type = io_type[:-2]  # remove []
        if io_type not in PACKAGE_ARRAY_ITEMS:
            raise PackageTypeError("Unsupported I/O 'array' definition: `{}`.".format(repr(io_info)))
        is_array = True
    return is_array, io_type


def _is_cwl_enum_type(io_info):
    # type: (any_io_type) -> Tuple[bool, AnyStr, Union[List[AnyStr], None]]
    """Verifies if the specified input/output corresponds to a CWL enum definition.

    :return is_enum: specifies if the input/output is of enum type
    :return io_type: enum base type if ``is_enum`` is True, type of ``io_info`` otherwise.
    :return io_allow: permitted values of the enum
    :raises PackageTypeError: if the enum doesn't have required parameters to be valid.
    """
    io_type = io_info['type']
    if not isinstance(io_type, dict) or 'type' not in io_type or io_type['type'] not in PACKAGE_CUSTOM_TYPES:
        return False, io_type, None

    if 'symbols' not in io_type:
        raise PackageTypeError("Unsupported I/O 'enum' definition: `{}`.".format(repr(io_info)))
    io_allow = io_type['symbols']
    if not isinstance(io_allow, list) or len(io_allow) < 1:
        raise PackageTypeError("Invalid I/O 'enum.symbols' definition: `{}`.".format(repr(io_info)))

    # validate matching types in allowed symbols and convert to supported CWL type
    first_allow = io_allow[0]
    for e in io_allow:
        if type(e) is not type(first_allow):
            raise PackageTypeError("Ambiguous types in I/O 'enum.symbols' definition: `{}`.".format(repr(io_info)))
    if isinstance(first_allow, six.string_types):
        io_type = 'string'
    elif isinstance(first_allow, float):
        io_type = 'float'
    elif isinstance(first_allow, six.integer_types):
        io_type = 'int'
    else:
        raise PackageTypeError("Unsupported I/O 'enum' base type: `{0}`, from definition: `{1}`."
                               .format(str(type(first_allow)), repr(io_info)))

    return True, io_type, io_allow


# noinspection PyUnusedLocal
def _cwl2wps_io(io_info, io_select):
    # type:(any_io_type, AnyStr) -> wps_io_type
    """Converts input/output parameters from CWL types to WPS types.
    :param io_info: parsed IO of a CWL file
    :param io_select: ``WPS_INPUT`` or ``WPS_OUTPUT`` to specify desired WPS type conversion.
    :returns: corresponding IO in WPS format
    """
    is_input = False                    # noqa: F841
    is_output = False
    if io_select == WPS_INPUT:
        is_input = True                 # noqa: F841
        io_literal = LiteralInput
        io_complex = ComplexInput
        io_bbox = BoundingBoxInput
    elif io_select == WPS_OUTPUT:
        is_output = True
        io_literal = LiteralOutput
        io_complex = ComplexOutput
        io_bbox = BoundingBoxOutput     # noqa: F841
    else:
        raise PackageTypeError("Unsupported I/O info definition: `{0}` with `{1}`.".format(repr(io_info), io_select))

    io_name = io_info['name']
    io_type = io_info['type']
    io_min_occurs = 1
    io_max_occurs = 1
    io_allow = AnyValue
    io_mode = MODE.NONE

    # convert array types
    is_array, array_elem = _is_cwl_array_type(io_info)
    if is_array:
        io_type = array_elem
        io_max_occurs = PACKAGE_ARRAY_MAX_SIZE

    # convert enum types
    is_enum, enum_type, enum_allow = _is_cwl_enum_type(io_info)
    if is_enum:
        io_type = enum_type
        io_allow = enum_allow
        io_mode = MODE.SIMPLE  # allowed value validator must be set for input

    # debug info for unhandled types conversion
    if not isinstance(io_type, six.string_types):
        LOGGER.debug('is_array:      `{}`'.format(repr(is_array)))
        LOGGER.debug('array_elem:    `{}`'.format(repr(array_elem)))
        LOGGER.debug('is_enum:       `{}`'.format(repr(is_enum)))
        LOGGER.debug('enum_type:     `{}`'.format(repr(enum_type)))
        LOGGER.debug('enum_allow:    `{}`'.format(repr(enum_allow)))
        LOGGER.debug('io_info:       `{}`'.format(repr(io_info)))
        LOGGER.debug('io_type:       `{}`'.format(repr(io_type)))
        LOGGER.debug('type(io_type): `{}`'.format(type(io_type)))
        raise TypeError("I/O type has not been properly decoded. Should be a string, got:`{!r}`".format(io_type))

    # literal types
    if is_enum or io_type in PACKAGE_LITERAL_TYPES:
        if io_type == 'Any':
            io_type = 'anyvalue'
        if io_type == 'null':
            io_type = 'novalue'
        if io_type in ['int', 'integer', 'long']:
            io_type = 'integer'
        if io_type in ['float', 'double']:
            io_type = 'float'
        return io_literal(identifier=io_name,
                          title=io_info.get('label', ''),
                          abstract=io_info.get('doc', ''),
                          data_type=io_type,
                          default=io_info.get('default', None),
                          min_occurs=io_min_occurs,
                          max_occurs=io_max_occurs,
                          # unless extended by custom types, no value validation for literals
                          mode=io_mode,
                          allowed_values=io_allow)
    # complex types
    else:
        kw = {
            'identifier': io_name,
            'title': io_info.get('label', io_name),
            'abstract': io_info.get('doc', ''),
        }
        if 'format' in io_info:
            kw['supported_formats'] = [Format(io_info['format'])]
            kw['mode'] = MODE.SIMPLE
        else:
            # we need to minimally add 1 format, otherwise empty list is evaluated as None by pywps
            # when 'supported_formats' is None, the process's json property raises because of it cannot iterate formats
            kw['supported_formats'] = [DefaultFormat]
            kw['mode'] = MODE.NONE
        if is_output:
            if io_type == 'Directory':
                kw['as_reference'] = True
            if io_type == 'File':
                has_contents = io_info.get('contents') is not None
                kw['as_reference'] = False if has_contents else True
        else:
            kw.update({
                'min_occurs': io_min_occurs,
                'max_occurs': io_max_occurs,
            })
        return io_complex(**kw)


def _json2wps_type(type_info, type_category):
    # type: (any_io_type, AnyStr) -> Any
    if type_category == 'allowed_values' and isinstance(type_info, dict):
        type_info.pop('type', None)
        return AllowedValue(**type_info)
    if type_category == 'allowed_values' and isinstance(type_info, six.string_types):
        return AllowedValue(value=type_info, allowed_type=ALLOWEDVALUETYPE.VALUE)
    if type_category == 'allowed_values' and isinstance(type_info, list):
        return AllowedValue(minval=min(type_info), maxval=max(type_info), allowed_type=ALLOWEDVALUETYPE.RANGE)
    if type_category == 'supported_formats' and isinstance(type_info, dict):
        return Format(**type_info)
    if type_category == 'supported_formats' and isinstance(type_info, six.string_types):
        return Format(type_info)
    if type_category == 'metadata' and isinstance(type_info, dict):
        return Metadata(**type_info)
    if type_category == 'metadata' and isinstance(type_info, six.string_types):
        return Metadata(type_info)
    if type_category == 'keywords' and isinstance(type_info, list):
        return type_info
    if type_category in ['identifier', 'title', 'abstract'] and isinstance(type_info, six.string_types):
        return type_info
    return None


def _json2wps_io(io_info, io_select):
    # type: (any_io_type, Union[WPS_INPUT, WPS_OUTPUT]) -> wps_io_type
    """Converts input/output parameters from a JSON dict to WPS types.
    :param io_info: IO in JSON dict format.
    :param io_select: ``WPS_INPUT`` or ``WPS_OUTPUT`` to specify desired WPS type conversion.
    :return: corresponding IO in WPS format.
    """

    io_info["identifier"] = _get_field(io_info, "identifier", search_variations=True, pop_found=True)

    rename = {
        'formats': 'supported_formats',
        'minOccurs': 'min_occurs',
        'maxOccurs': 'max_occurs',
    }
    remove = [
        'id',
        'workdir',
        'any_value',
        'data_format',
        'data',
        'file',
        'mimetype',
        'encoding',
        'schema',
        'asreference',
        'additionalParameters',
    ]
    replace_values = {'unbounded': PACKAGE_ARRAY_MAX_SIZE}

    transform_json(io_info, rename=rename, remove=remove, replace_values=replace_values)

    # convert allowed value objects
    values = _get_field(io_info, 'allowed_values', search_variations=True, pop_found=True)
    if values is not null:
        if isinstance(values, list) and len(values) > 0:
            io_info['allowed_values'] = list()
            for allow_value in values:
                io_info['allowed_values'].append(_json2wps_type(allow_value, 'allowed_values'))
        else:
            io_info['allowed_values'] = AnyValue

    # convert supported format objects
    formats = _get_field(io_info, 'supported_formats', search_variations=True, pop_found=True)
    if formats is not null:
        for fmt in formats:
            fmt["mime_type"] = fmt.pop("mimeType")
            fmt.pop("maximumMegabytes", None)
            fmt.pop("default", None)
        io_info['supported_formats'] = [_json2wps_type(fmt, 'supported_formats') for fmt in formats]

    # convert metadata objects
    metadata = _get_field(io_info, 'metadata', search_variations=True, pop_found=True)
    if metadata is not null:
        io_info['metadata'] = [_json2wps_type(meta, 'metadata') for meta in metadata]

    # convert literal fields specified as is
    for field in ['identifier', 'title', 'abstract', 'keywords']:
        value = _get_field(io_info, field, search_variations=True, pop_found=True)
        if value is not null:
            io_info[field] = _json2wps_type(value, field)

    # convert by type
    io_type = io_info.pop('type', WPS_COMPLEX)  # only ComplexData doesn't have 'type'
    if io_select == WPS_INPUT:
        if io_type in (WPS_REFERENCE, WPS_COMPLEX):
            io_info.pop('data_type', None)
            if 'supported_formats' not in io_info:
                io_info['supported_formats'] = [DefaultFormat]
            if ('max_occurs', 'unbounded') in io_info.items():
                io_info['max_occurs'] = PACKAGE_ARRAY_MAX_SIZE
            return ComplexInput(**io_info)
        if io_type == WPS_BOUNDINGBOX:
            io_info.pop('supported_formats', None)
            io_info.pop('supportedCRS', None)
            return BoundingBoxInput(**io_info)
        if io_type == WPS_LITERAL:
            io_info.pop('supported_formats', None)
            io_info.pop('literalDataDomains', None)
            return LiteralInput(**io_info)
    elif io_select == WPS_OUTPUT:
        # extra params to remove for outputs
        io_info.pop('min_occurs', None)
        io_info.pop('max_occurs', None)
        if io_type in (WPS_REFERENCE, WPS_COMPLEX):
            return ComplexOutput(**io_info)
        if io_type == WPS_BOUNDINGBOX:
            return BoundingBoxOutput(**io_info)
        if io_type == WPS_LITERAL:
            return LiteralOutput(**io_info)
    raise PackageTypeError("Unknown conversion from dict to WPS type (type={0}, mode={1}).".format(io_type, io_select))


def _wps2json_io(io_wps):
    # type: (wps_io_type) -> any_io_type
    """Converts a PyWPS I/O into a dictionary based version with keys corresponding to standard names (WPS 2.0)."""

    if not isinstance(io_wps, BasicIO):
        raise PackageTypeError("Invalid type, expected `BasicIO`, got: `[{0!r}] {1!r}`".format(type(io_wps), io_wps))
    # in some cases (Complex I/O), 'as_reference=True' causes 'type' to be overwritten, revert it back
    # noinspection PyUnresolvedReferences
    io_wps_json = io_wps.json

    rename = {
        u"identifier": u"id",
        u"supported_formats": u"formats",
        u"mime_type": u"mimeType",
        u"min_occurs": u"minOccurs",
        u"max_occurs": u"maxOccurs",
    }
    replace_values = {
        PACKAGE_ARRAY_MAX_SIZE: "unbounded",
    }
    replace_func = {
        "maxOccurs": str,
        "minOccurs": str,
    }

    transform_json(io_wps_json, rename=rename, replace_values=replace_values, replace_func=replace_func)

    if 'type' in io_wps_json and io_wps_json['type'] == 'reference':
        io_wps_json['type'] = WPS_COMPLEX

    # minimum requirement of 1 format object which defines mime-type
    if 'formats' not in io_wps_json or not len(io_wps_json['formats']):
        io_wps_json['formats'] = [DefaultFormat.json]

    for io_format in io_wps_json['formats']:
        transform_json(io_format, rename=rename, replace_values=replace_values, replace_func=replace_func)

    return io_wps_json


def _get_field(io_object, field, search_variations=False, pop_found=False):
    # type: (Union[BasicIO, Dict[str, Any]], str, bool, bool) -> Any
    """Gets a field by name from various I/O object types."""
    if isinstance(io_object, dict):
        value = io_object.get(field, null)
        if value is not null:
            if pop_found:
                io_object.pop(field)
            return value
    else:
        value = getattr(io_object, field, null)
        if value is not null:
            return value
    if search_variations and field in WPS_FIELD_MAPPING:
        for var in WPS_FIELD_MAPPING[field]:
            value = _get_field(io_object, var, pop_found=pop_found)
            if value is not null:
                return value
    return null


def _set_field(io_object, field, value):
    # type: (Union[BasicIO, Dict[str, Any]], str, Any) -> None
    """Sets a field by name into various I/O object types."""
    if not isinstance(value, NullType):
        if isinstance(io_object, dict):
            io_object[field] = value
            return
        setattr(io_object, field, value)


def _merge_package_io(wps_io_list, cwl_io_list, io_select):
    # type: (List[any_io_type], List[cwl_io_type], Union[WPS_INPUT, WPS_OUTPUT]) -> List
    """
    Update I/O definitions to use for process creation and returned by GetCapabilities, DescribeProcess.
    If WPS I/O definitions where provided during deployment, update them with CWL-to-WPS converted I/O and
    preserve their optional WPS fields. Otherwise, provide minimum field requirements from CWL.
    Removes any deployment WPS I/O definitions that don't match any CWL I/O by id.
    Adds missing deployment WPS I/O definitions using expected CWL I/O ids.

    :param wps_io_list: list of WPS I/O (as json) passed during process deployment.
    :param cwl_io_list: list of CWL I/O converted to WPS-like I/O for counter-validation.
    :param io_select: ``WPS_INPUT`` or ``WPS_OUTPUT`` to specify desired WPS type conversion.
    :returns: list of validated/updated WPS I/O for the process.
    """
    if not isinstance(cwl_io_list, list):
        raise PackageTypeError("CWL I/O definitions must be provided, empty list if none required.")
    if not wps_io_list:
        wps_io_list = list()
    wps_io_dict = OrderedDict((_get_field(wps_io, 'identifier', search_variations=True), wps_io)
                              for wps_io in wps_io_list)
    cwl_io_dict = OrderedDict((_get_field(cwl_io, 'identifier', search_variations=True), cwl_io)
                              for cwl_io in cwl_io_list)
    missing_io_list = set(cwl_io_dict) - set(wps_io_dict)
    updated_io_list = list()
    # missing WPS I/O are inferred only using CWL->WPS definitions
    for cwl_id in missing_io_list:
        updated_io_list.append(cwl_io_dict[cwl_id])
    # evaluate provided WPS I/O definitions
    for wps_io_json in wps_io_list:
        wps_id = _get_field(wps_io_json, 'identifier', search_variations=True)
        # WPS I/O by id not matching any CWL->WPS I/O are discarded, otherwise merge details
        if wps_id not in cwl_io_dict:
            continue
        cwl_io = cwl_io_dict[wps_id]
        cwl_io_json = cwl_io.json
        updated_io_list.append(cwl_io)
        # enforce expected CWL->WPS I/O type and append required parameters if missing
        cwl_identifier = _get_field(cwl_io_json, 'identifier', search_variations=True)
        cwl_title = _get_field(wps_io_json, 'title', search_variations=True)
        wps_io_json.update({'type': _get_field(cwl_io_json, 'type'),
                            'identifier': cwl_identifier,
                            'title': cwl_title if cwl_title is not null else cwl_identifier})
        wps_io = _json2wps_io(wps_io_json, io_select)
        # retrieve any complementing fields (metadata, keywords, etc.) passed as WPS input
        for field_type in WPS_FIELD_MAPPING:
            cwl_field = _get_field(cwl_io, field_type)
            wps_field = _get_field(wps_io, field_type)
            # override if CWL->WPS was missing but is provided by WPS
            if cwl_field is null:
                continue
            if type(cwl_field) != type(wps_field) or (cwl_field is not None and wps_field is None):
                continue
            _set_field(updated_io_list[-1], field_type, wps_field)
    return updated_io_list


def transform_json(json_data,               # type: any_io_type
                   rename=None,             # type: Optional[Dict[any_key_type, Any]]
                   remove=None,             # type: Optional[List[any_key_type]]
                   add=None,                # type: Optional[Dict[any_key_type, Any]]
                   replace_values=None,     # type: Optional[Dict[any_key_type, Any]]
                   replace_func=None,       # type: Optional[Dict[AnyStr, Callable[[Any], Any]]]
                   ):                       # type: (...) -> any_io_type
    """
    Transforms the input json_data with different methods.
    The transformations are applied in the same order as the arguments.
    """
    rename = rename or {}
    remove = remove or []
    add = add or {}
    replace_values = replace_values or {}
    replace_func = replace_func or {}

    # rename
    for k, v in rename.items():
        if k in json_data:
            json_data[v] = json_data.pop(k)

    # remove
    for r in remove:
        json_data.pop(r, None)

    # add
    for k, v in add.items():
        json_data[k] = v

    # replace values
    for key, value in json_data.items():
        for old_value, new_value in replace_values.items():
            if value == old_value:
                json_data[key] = new_value

    # replace with function call
    for k, func in replace_func.items():
        if k in json_data:
            json_data[k] = func(json_data[k])

    # also rename if the type of the value is a list of dicts
    for key, value in json_data.items():
        if isinstance(value, list):
            for nested_item in value:
                if isinstance(nested_item, dict):
                    for k, v in rename.items():
                        if k in nested_item:
                            nested_item[v] = nested_item.pop(k)
                    for k, func in replace_func.items():
                        if k in nested_item:
                            nested_item[k] = func(nested_item[k])
    return json_data


def _merge_package_inputs_outputs(wps_inputs_list,      # type: List[any_io_type]
                                  cwl_inputs_list,      # type: List[cwl_input_type]
                                  wps_outputs_list,     # type: List[any_io_type]
                                  cwl_outputs_list      # type: List[cwl_output_type]
                                  ):                    # type: (...) -> Tuple[List[any_io_type], List[any_io_type]]
    """Merges I/O definitions to use for process creation and returned by GetCapabilities, DescribeProcess
    using the WPS specifications (from request POST) and CWL specifications (extracted from file)."""
    wps_inputs_merged = _merge_package_io(wps_inputs_list, cwl_inputs_list, WPS_INPUT)
    wps_outputs_merged = _merge_package_io(wps_outputs_list, cwl_outputs_list, WPS_OUTPUT)
    return [_wps2json_io(i) for i in wps_inputs_merged], [_wps2json_io(o) for o in wps_outputs_merged]


def _get_package_io(package, io_select, as_json):
    if io_select == WPS_OUTPUT:
        io_attrib = 'outputs_record_schema'
    elif io_select == WPS_INPUT:
        io_attrib = 'inputs_record_schema'
    else:
        raise PackageTypeError("Unknown I/O selection: `{}`.".format(io_select))
    cwl_package_io = getattr(package.t, io_attrib)
    wps_package_io = [_cwl2wps_io(io, io_select) for io in cwl_package_io['fields']]
    if as_json:
        return [_wps2json_io(io) for io in wps_package_io]
    return wps_package_io


def _get_package_inputs(package, as_json=False):
    """Generates WPS-like inputs using parsed CWL package input definitions."""
    return _get_package_io(package, io_select=WPS_INPUT, as_json=as_json)


def _get_package_outputs(package, as_json=False):
    """Generates WPS-like outputs using parsed CWL package output definitions."""
    return _get_package_io(package, io_select=WPS_OUTPUT, as_json=as_json)


def _get_package_inputs_outputs(package, as_json=False):
    """Generates WPS-like (inputs,outputs) tuple using parsed CWL package output definitions."""
    return (_get_package_io(package, io_select=WPS_INPUT, as_json=as_json),
            _get_package_io(package, io_select=WPS_OUTPUT, as_json=as_json))


def _update_package_metadata(wps_package_metadata, cwl_package_package):
    """Updates the package WPS metadata dictionary from extractable CWL package definition."""
    wps_package_metadata['title'] = wps_package_metadata.get('title', cwl_package_package.get('label', ''))
    wps_package_metadata['abstract'] = wps_package_metadata.get('abstract', cwl_package_package.get('doc', ''))

    if '$schemas' in cwl_package_package and isinstance(cwl_package_package['$schemas'], list) \
            and '$namespaces' in cwl_package_package and isinstance(cwl_package_package['$namespaces'], dict):
        metadata = wps_package_metadata.get('metadata', list())
        namespaces_inv = {v: k for k, v in cwl_package_package['$namespaces']}
        for schema in cwl_package_package['$schemas']:
            for namespace_url in namespaces_inv:
                if schema.startswith(namespace_url):
                    metadata.append({'title': namespaces_inv[namespace_url], 'href': schema})
        wps_package_metadata['metadata'] = metadata

    if 's:keywords' in cwl_package_package and isinstance(cwl_package_package['s:keywords'], list):
        wps_package_metadata['keywords'] = list(set(wps_package_metadata.get('keywords', list)) |
                                                set(cwl_package_package.get('s:keywords')))


def get_process_from_wps_request(process_offering, reference=None, package=None, data_source=None):
    # type: (Dict, Optional[AnyStr], Optional[AnyStr], Optional[AnyStr]) -> Dict
    """
    Returns an updated process information dictionary ready for storage using provided WPS ``process_offering``
    and a package definition passed by ``reference`` or ``package`` JSON content.
    The returned process information can be used later on to load an instance of :class:`weaver.wps_package.Package`.

    :param process_offering: WPS REST-API process offering as JSON.
    :param reference: URL to an existing package definition.
    :param package: literal package definition as JSON.
    :param data_source: where to resolve process IDs (default: localhost if ``None``).
    :return: process information dictionary ready for saving to data store.
    """

    def try_or_raise_package_error(call, reason):
        try:
            LOGGER.debug("Attempting: [{}].".format(reason))
            return call()
        except Exception as exc:
            # re-raise any exception already handled by a 'package' error as is, but with a more detailed message
            # handle any other sub-exception that wasn't processed by a 'package' error as a registration error
            package_errors = (PackageRegistrationError, PackageTypeError, PackageRegistrationError, PackageNotFound)
            exc_type = type(exc) if isinstance(exc, package_errors) else PackageRegistrationError
            LOGGER.exception(exc.message)
            raise exc_type(
                "Invalid package/reference definition. {0} generated error: [{1}].".format(reason, repr(exc))
            )

    if not (isinstance(package, dict) or isinstance(reference, six.string_types)):
        raise PackageRegistrationError(
            "Invalid parameters amongst one of [package, reference].")
    if package and reference:
        raise PackageRegistrationError(
            "Simultaneous parameters [package, reference] not allowed.")

    if reference:
        package = _load_package_file(reference)
    if not isinstance(package, dict):
        raise PackageRegistrationError("Cannot decode process package contents.")
    if 'class' not in package:
        raise PackageRegistrationError("Cannot obtain process type from package class.")

    LOGGER.debug('Using data source: `{}`'.format(data_source))
    package_factory, process_type, _ = try_or_raise_package_error(
        lambda: _load_package_content(package, data_source=data_source),
        reason="Loading package content")

    package_inputs, package_outputs = try_or_raise_package_error(
        lambda: _get_package_inputs_outputs(package_factory),
        reason="Definition of package/process inputs/outputs")
    process_inputs = process_offering.get('inputs', list())
    process_outputs = process_offering.get('outputs', list())

    try_or_raise_package_error(
        lambda: _update_package_metadata(process_offering, package),
        reason="Metadata update")

    package_inputs, package_outputs = try_or_raise_package_error(
        lambda: _merge_package_inputs_outputs(process_inputs, package_inputs,
                                              process_outputs, package_outputs),
        reason="Merging of inputs/outputs")

    process_offering.update({
        'package': package,
        'type': process_type,
        'inputs': package_inputs,
        'outputs': package_outputs
    })
    return process_offering


class WpsPackage(Process):
    package = None
    job_file = None
    log_file = None
    log_level = logging.INFO
    logger = None
    tmp_dir = None
    percent = None

    def __init__(self, **kw):
        """
        Creates a WPS Process instance to execute a CWL package definition.
        Process parameters should be loaded from an existing :class:`weaver.datatype.Process`
        instance generated using method `get_process_from_wps_request`.

        :param kw: dictionary corresponding to method :class:`weaver.datatype.Process.params_wps`
        """
        self.payload = kw.pop("payload")
        self.package = kw.pop('package')
        if not self.package:
            raise PackageRegistrationError("Missing required package definition for package process.")
        if not isinstance(self.package, dict):
            raise PackageRegistrationError("Unknown parsing of package definition for package process.")

        inputs = kw.pop('inputs', [])

        # handle EOImage inputs
        inputs = opensearch.replace_inputs_describe_process(inputs=inputs, payload=self.payload)

        inputs = [_json2wps_io(i, WPS_INPUT) for i in inputs]
        outputs = [_json2wps_io(o, WPS_OUTPUT) for o in kw.pop('outputs', list())]
        metadata = [_json2wps_type(meta_kw, 'metadata') for meta_kw in kw.pop('metadata', list())]

        super(WpsPackage, self).__init__(
            self._handler,
            inputs=inputs,
            outputs=outputs,
            metadata=metadata,
            store_supported=True,
            status_supported=True,
            **kw
        )

    def setup_logger(self):
        # file logger for output
        self.log_file = self.status_location + '.log'
        log_file_handler = logging.FileHandler(self.log_file)
        log_file_formatter = logging.Formatter(fmt=get_log_fmt(), datefmt=get_log_datefmt())
        log_file_handler.setFormatter(log_file_formatter)

        # prepare package logger
        self.logger = logging.getLogger('wps_package.{}'.format(self.package_id))
        self.logger.addHandler(log_file_handler)
        self.logger.setLevel(self.log_level)

        # add CWL job and CWL runner logging to current package logger
        job_logger = logging.getLogger('job {}'.format(PACKAGE_DEFAULT_FILE_NAME))
        job_logger.addHandler(log_file_handler)
        job_logger.setLevel(self.log_level)
        cwl_logger = logging.getLogger('cwltool')
        cwl_logger.addHandler(log_file_handler)
        cwl_logger.setLevel(self.log_level)

        # add weaver Tweens logger to current package logger
        weaver_tweens_logger = logging.getLogger('weaver.tweens')
        weaver_tweens_logger.addHandler(log_file_handler)
        weaver_tweens_logger.setLevel(self.log_level)

    def update_status(self, message, progress, status):
        # type: (AnyStr, int, AnyStatusType) -> None
        """Updates the PyWPS real job status from a specified parameters."""
        self.percent = progress or self.percent or 0

        # find the enum PyWPS status matching the given one as string
        pywps_status = map_status(status, STATUS_COMPLIANT_PYWPS)
        pywps_status_id = STATUS_PYWPS_IDS[pywps_status]

        # pywps overrides 'status' by 'accepted' in 'update_status', so use the '_update_status' to enforce the status
        # using protected method also avoids weird overrides of progress percent on failure and final 'success' status
        # noinspection PyProtectedMember
        self.response._update_status(pywps_status_id, message, self.percent)
        self.log_message(status=status, message=message, progress=progress)

    # TODO
    #  Les callback d'update status ont ete brassees pas mal dans wps1/wps3 process
    #  ca se peut que les arguments passent tous croche
    def step_update_status(self, message, progress, start_step_progress, end_step_progress, step_name,
                           target_host, status):
        # type: (AnyStr, int, int, int, AnyStr, AnyValue, AnyStr) -> None
        self.update_status(
            message="{0} [{1}] - {2}".format(target_host, step_name, str(message).strip()),
            progress=self.map_progress(progress, start_step_progress, end_step_progress),
            status=status,
        )

    def log_message(self, status, message, progress=None, level=logging.INFO):
        message = get_job_log_msg(status=map_status(status), message=message, progress=progress)
        self.logger.log(level, message, exc_info=level > logging.INFO)

    def exception_message(self, exception_type, exception=None, message='no message'):
        exception_msg = ' [{}]'.format(repr(exception)) if isinstance(exception, Exception) else ''
        self.log_message(status=STATUS_EXCEPTION,
                         message='{0}: {1}{2}'.format(exception_type.__name__, message, exception_msg),
                         level=logging.ERROR)
        return exception_type('{0}{1}'.format(message, exception_msg))

    @staticmethod
    def map_progress(progress, range_min, range_max):
        return range_min + (progress * (range_max - range_min)) / 100

    @classmethod
    def map_step_progress(cls, step_index, steps_total):
        return cls.map_progress(100 * step_index / steps_total, PACKAGE_PROGRESS_RUN_CWL, PACKAGE_PROGRESS_CWL_DONE)

    def _handler(self, request, response):
        LOGGER.debug("HOME=%s, Current Dir=%s", os.environ.get('HOME'), os.path.abspath(os.curdir))
        self.request = request
        self.response = response
        self.package_id = self.request.identifier

        try:
            try:
                self.setup_logger()
                # self.response.outputs[PACKAGE_LOG_FILE].file = self.log_file
                # self.response.outputs[PACKAGE_LOG_FILE].as_reference = True
                self.update_status("Preparing package logs done.", PACKAGE_PROGRESS_PREP_LOG, STATUS_RUNNING)
            except Exception as exc:
                raise self.exception_message(PackageExecutionError, exc, "Failed preparing package logging.")

            self.update_status("Launching package ...", PACKAGE_PROGRESS_LAUNCHING, STATUS_RUNNING)

            registry = app.conf['PYRAMID_REGISTRY']
            if get_weaver_configuration(registry.settings) == WEAVER_CONFIGURATION_EMS:
                # EMS dispatch the execution to the ADES
                loading_context = LoadingContext()
                loading_context.construct_tool_object = self.make_tool
            else:
                # ADES execute the cwl locally
                loading_context = None

            runtime_context = RuntimeContext(kwargs={'no_read_only': True, 'outdir': self.workdir})
            try:
                self.package_inst, _, self.step_packages = _load_package_content(self.package,
                                                                                 package_name=self.package_id,
                                                                                 # no data source for local package
                                                                                 data_source=None,
                                                                                 loading_context=loading_context,
                                                                                 runtime_context=runtime_context)
                self.step_launched = []

            except Exception as ex:
                raise PackageRegistrationError("Exception occurred on package instantiation: `{}`".format(repr(ex)))
            self.update_status("Loading package content done.", PACKAGE_PROGRESS_LOADING, STATUS_RUNNING)

            try:
                cwl_input_info = dict([(i['name'], i) for i in self.package_inst.t.inputs_record_schema['fields']])
                self.update_status("Retrieve package inputs done.", PACKAGE_PROGRESS_GET_INPUT, STATUS_RUNNING)
            except Exception as exc:
                raise self.exception_message(PackageExecutionError, exc, "Failed retrieving package input types.")
            try:
                # identify EOImages from payload
                request.inputs = opensearch.get_original_collection_id(self.payload, request.inputs)
                eoimage_data_sources = opensearch.get_eo_images_data_sources(self.payload, request.inputs)
                if eoimage_data_sources:
                    accept_mime_types = opensearch.get_eo_images_mime_types(self.payload)
                    opensearch.insert_max_occurs(self.payload, request.inputs)
                    request.inputs = opensearch.query_eo_images_from_wps_inputs(request.inputs,
                                                                                eoimage_data_sources,
                                                                                accept_mime_types)

                cwl_inputs = dict()
                for input_id in request.inputs:
                    # skip empty inputs (if that is even possible...)
                    input_occurs = request.inputs[input_id]
                    if len(input_occurs) <= 0:
                        continue
                    # process single occurrences
                    input_i = input_occurs[0]
                    # handle as reference/data
                    input_data = input_i.url if input_i.as_reference else input_i.data
                    input_type = cwl_input_info[input_id]['type']
                    is_array, elem_type = _is_cwl_array_type(cwl_input_info[input_id])
                    if is_array:
                        # extend array data that allow max_occur > 1
                        input_data = [i.url if i.as_reference else i.data for i in input_occurs]
                        input_type = elem_type
                    if isinstance(input_i, ComplexInput) or elem_type == "File":
                        if isinstance(input_data, list):
                            cwl_inputs[input_id] = [{'location': data, 'class': input_type} for data in input_data]
                        else:
                            cwl_inputs[input_id] = {'location': input_data, 'class': input_type}
                    elif isinstance(input_i, (LiteralInput, BoundingBoxInput)):
                        cwl_inputs[input_id] = input_data
                    else:
                        raise self.exception_message(PackageTypeError, None,
                                                     "Undefined package input for execution: {}.".format(type(input_i)))
                self.update_status("Convert package inputs done.", PACKAGE_PROGRESS_CONVERT_INPUT, STATUS_RUNNING)
            except Exception as exc:
                raise self.exception_message(PackageExecutionError, exc, "Failed to load package inputs.")

            try:
                self.update_status("Running package ...", PACKAGE_PROGRESS_RUN_CWL, STATUS_RUNNING)

                # Inputs starting with file:// will be interpreted as ems local files
                # If OpenSearch obtain file:// references that must be passed to the ADES use an uri starting
                # with OPENSEARCH_LOCAL_FILE_SCHEME://
                result = self.package_inst(**cwl_inputs)
                self.update_status("Package execution done.", PACKAGE_PROGRESS_CWL_DONE, STATUS_RUNNING)
            except Exception as exc:
                raise self.exception_message(PackageExecutionError, exc, "Failed package execution.")
            try:
                for output in request.outputs:
                    if 'location' in result[output]:
                        self.response.outputs[output].as_reference = True
                        self.response.outputs[output].file = result[output]['location'].replace('file://', '')
                    else:
                        self.response.outputs[output].data = result[output]
                self.update_status("Generate package outputs done.", PACKAGE_PROGRESS_PREP_OUT, STATUS_RUNNING)
            except Exception as exc:
                raise self.exception_message(PackageExecutionError, exc, "Failed to save package outputs.")
        # noinspection PyBroadException
        except Exception:
            # return log file location by status message since outputs are not obtained by WPS failed process
            error_msg = "Package completed with errors. Server logs: {}".format(self.log_file)
            self.update_status(error_msg, self.percent, STATUS_FAILED)
            raise
        else:
            self.update_status("Package complete.", PACKAGE_PROGRESS_DONE, STATUS_SUCCEEDED)
        return self.response

    def make_tool(self, toolpath_object, loading_context):
        # type: (ToolPathObjectType, LoadingContext) -> ProcessCWL
        return default_make_tool(toolpath_object, loading_context, self.get_job_process_definition)

    def get_job_process_definition(self, jobname, joborder, tool):
        """
        This function is called before running an ADES job (either from a workflow step or a simple EMS dispatch).
        It must return a WpsProcess instance configured with the proper package, ADES target and cookies.

        :param jobname: The workflow step or the package id that must be launch on an ADES :class:`string`
        :param joborder: The params for the job :class:`dict {input_name: input_value }`
                         input_value is one of `input_object` or `array [input_object]`
                         input_object is one of `string` or `dict {class: File, location: string}`
                         in our case input are expected to be File object
        :param tool: Whole cwl config including hints requirement
        """

        if jobname == self.package_id:
            # A step is the package itself only for non-workflow package being executed on the EMS
            # default action requires ADES dispatching but hints can indicate also WPS1 or ESGF-CWT provider
            step_payload = self.payload
            process = self.package_id
            jobtype = 'package'
        else:
            # Here we got a step part of a workflow (self is the workflow package)
            step_process_url = get_process_location(self.step_packages[jobname])
            step_payload = _get_process_payload(step_process_url)
            process = self.step_packages[jobname]
            jobtype = 'step'

        # Progress made with steps presumes that they are done sequentially and have the same progress weight
        start_step_progress = self.map_step_progress(len(self.step_launched), max(1, len(self.step_packages)))
        end_step_progress = self.map_step_progress(len(self.step_launched) + 1, max(1, len(self.step_packages)))

        self.step_launched.append(jobname)
        self.update_status("Preparing to launch {type} {name}.".format(
            type=jobtype,
            name=jobname),
            start_step_progress, STATUS_RUNNING)

        # TODO:
        #   Le parametre 'tool' devrait contenir la structure hint du cwl (reste a valider)
        #   La structure tool['hints']['WPS1Requirement']['provider'] n'est donc que pure invention a l'heure actuelle
        if 'WPS1Requirement' in tool['hints']:
            provider = tool['hints']['WPS1Requirement']['provider']
            # The process id of the provider isn't required to be the same as the one use in the EMS
            process = tool['hints']['WPS1Requirement']['process']
            return Wps1Process(provider=provider,
                               process=process,
                               cookies=self.request.http_request.cookies,
                               update_status=lambda _provider, _message, _progress, _status: self.step_update_status(
                                   _message, _progress, start_step_progress, end_step_progress, jobname,
                                   _provider, _status
                               ))
        elif 'ESGF-CWTRequirement' in tool['hints']:
            raise NotImplementedError('ESGF-CWTRequirement not implemented')
        else:
            return Wps3Process(step_payload=step_payload,
                               joborder=joborder,
                               process=process,
                               cookies=self.request.http_request.cookies,
                               update_status=lambda _provider, _message, _progress, _status: self.step_update_status(
                                   _message, _progress, start_step_progress, end_step_progress, jobname,
                                   _provider, _status
                               ))