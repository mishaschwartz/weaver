import logging
import os
import re
import tempfile
from configparser import ConfigParser
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from beaker.cache import cache_region
from owslib.wps import WebProcessingService, WPSExecution
from pyramid.httpexceptions import HTTPNotFound, HTTPUnprocessableEntity
from pywps import configuration as pywps_config
from webob.acceptparse import create_accept_language_header

from weaver import xml_util
from weaver.config import get_weaver_configuration
from weaver.formats import ACCEPT_LANGUAGES
from weaver.utils import (
    get_header,
    get_no_cache_option,
    get_request_options,
    get_settings,
    get_ssl_verify_option,
    get_url_without_query,
    get_weaver_url,
    invalidate_region,
    is_uuid,
    make_dirs,
    request_extra,
    retry_on_cache_error
)

LOGGER = logging.getLogger(__name__)
if TYPE_CHECKING:
    from typing import Dict, Union, Optional

    from weaver.typedefs import AnyRequestType, AnySettingsContainer, HeadersType, ProcessOWS


def _get_settings_or_wps_config(container,                  # type: AnySettingsContainer
                                weaver_setting_name,        # type: str
                                config_setting_section,     # type: str
                                config_setting_name,        # type: str
                                default_not_found,          # type: str
                                message_not_found,          # type: str
                                ):                          # type: (...) -> str

    settings = get_settings(container)
    found = settings.get(weaver_setting_name)
    if not found:
        if not settings.get("weaver.wps_configured"):
            load_pywps_config(container)
        found = pywps_config.CONFIG.get(config_setting_section, config_setting_name)
    if not isinstance(found, str):
        LOGGER.warning("%s not set in settings or WPS configuration, using default value.", message_not_found)
        found = default_not_found
    return found.strip()


def get_wps_path(container):
    # type: (AnySettingsContainer) -> str
    """
    Retrieves the WPS path (without hostname).

    Searches directly in settings, then `weaver.wps_cfg` file, or finally, uses the default values if not found.
    """
    path = _get_settings_or_wps_config(container, "weaver.wps_path", "server", "url", "/ows/wps", "WPS path")
    return urlparse(path).path


def get_wps_url(container):
    # type: (AnySettingsContainer) -> str
    """
    Retrieves the full WPS URL (hostname + WPS path).

    Searches directly in settings, then `weaver.wps_cfg` file, or finally, uses the default values if not found.
    """
    return get_settings(container).get("weaver.wps_url") or get_weaver_url(container) + get_wps_path(container)


def get_wps_output_dir(container):
    # type: (AnySettingsContainer) -> str
    """
    Retrieves the WPS output directory path where to write XML and result files.

    Searches directly in settings, then `weaver.wps_cfg` file, or finally, uses the default values if not found.
    """
    tmp_dir = tempfile.gettempdir()
    return _get_settings_or_wps_config(container, "weaver.wps_output_dir",
                                       "server", "outputpath", tmp_dir, "WPS output directory")


def get_wps_output_path(container):
    # type: (AnySettingsContainer) -> str
    """
    Retrieves the WPS output path (without hostname) for staging XML status, logs and process outputs.

    Searches directly in settings, then `weaver.wps_cfg` file, or finally, uses the default values if not found.
    """
    return get_settings(container).get("weaver.wps_output_path") or urlparse(get_wps_output_url(container)).path


def get_wps_output_url(container):
    # type: (AnySettingsContainer) -> str
    """
    Retrieves the WPS output URL that maps to WPS output directory path.

    Searches directly in settings, then `weaver.wps_cfg` file, or finally, uses the default values if not found.
    """
    wps_output_default = get_weaver_url(container) + "/wpsoutputs"
    wps_output_config = _get_settings_or_wps_config(
        container, "weaver.wps_output_url", "server", "outputurl", wps_output_default, "WPS output url"
    )
    return wps_output_config or wps_output_default


def get_wps_output_context(request):
    # type: (AnyRequestType) -> Optional[str]
    """
    Obtains and validates allowed values for sub-directory context of WPS outputs in header ``X-WPS-Output-Context``.

    :raises HTTPUnprocessableEntity: if the header was provided an contains invalid or illegal value.
    :returns: validated context or None if not specified.
    """
    headers = getattr(request, "headers", {})
    ctx = get_header("X-WPS-Output-Context", headers)
    if not ctx:
        settings = get_settings(request)
        ctx_default = settings.get("weaver.wps_output_context", None)
        if not ctx_default:
            return None
        LOGGER.debug("Using default 'wps.wps_output_context': %s", ctx_default)
        ctx = ctx_default
    cxt_found = re.match(r"^(?=[\w-]+)([\w-]+/?)+$", ctx)
    if cxt_found and cxt_found[0] == ctx:
        ctx_matched = ctx[:-1] if ctx.endswith("/") else ctx
        LOGGER.debug("Using request 'X-WPS-Output-Context': %s", ctx_matched)
        return ctx_matched
    raise HTTPUnprocessableEntity(json={
        "code": "InvalidHeaderValue",
        "name": "X-WPS-Output-Context",
        "description": "Provided value for 'X-WPS-Output-Context' request header is invalid.",
        "cause": "Value must be an alphanumeric context directory or tree hierarchy of sub-directory names.",
        "value": str(ctx)
    })


def get_wps_local_status_location(url_status_location, container, must_exist=True):
    # type: (str, AnySettingsContainer, bool) -> Optional[str]
    """
    Attempts to retrieve the local XML file path corresponding to the WPS status location as URL.

    :param url_status_location: URL reference pointing to some WPS status location XML.
    :param container: any settings container to map configured local paths.
    :param must_exist: return only existing path if enabled, otherwise return the parsed value without validation.
    :returns: found local file path if it exists, ``None`` otherwise.
    """
    dir_path = get_wps_output_dir(container)
    if url_status_location and not urlparse(url_status_location).scheme in ["", "file"]:
        wps_out_url = get_wps_output_url(container)
        req_out_url = get_url_without_query(url_status_location)
        out_path = os.path.join(dir_path, req_out_url.replace(wps_out_url, "").lstrip("/"))
    else:
        out_path = url_status_location.replace("file://", "")
    found = os.path.isfile(out_path)
    if not found and "/jobs/" in url_status_location:
        job_uuid = url_status_location.rsplit("/jobs/", 1)[-1].split("/", 1)[0]
        if is_uuid(job_uuid):
            out_path_join = os.path.join(dir_path, "{}.xml".format(job_uuid))
            found = os.path.isfile(out_path_join)
            if found or not must_exist:
                out_path = out_path_join
    if not found and must_exist:
        out_path_join = os.path.join(dir_path, out_path[1:] if out_path.startswith("/") else out_path)
        if not os.path.isfile(out_path_join):
            LOGGER.debug("Could not map WPS status reference [%s] to input local file path [%s].",
                         url_status_location, out_path)
            return None
        out_path = out_path_join
    LOGGER.debug("Resolved WPS status reference [%s] as local file path [%s].", url_status_location, out_path)
    return out_path


def map_wps_output_location(reference, container, reverse=False, exists=True):
    # type: (str, AnySettingsContainer, bool, bool) -> Optional[str]
    """
    Obtains the mapped WPS output location of a file where applicable.

    :param reference: local file path (normal) or file URL (reverse) to be mapped.
    :param container: retrieve application settings.
    :param reverse: perform the reverse operation (local path -> URL endpoint), or process normally (URL -> local path).
    :param exists: ensure that the mapped file exists, otherwise don't map it.
    :returns: mapped reference that corresponds to the local WPS output location.
    """
    settings = get_settings(container)
    wps_out_dir = get_wps_output_dir(settings)
    wps_out_url = get_wps_output_url(settings)
    if reverse and reference.startswith(wps_out_dir):
        wps_out_ref = reference.replace(wps_out_dir, wps_out_url)
        if not exists or os.path.isfile(wps_out_ref):
            return wps_out_ref
    elif not reverse and reference.startswith(wps_out_url):
        wps_out_ref = reference.replace(wps_out_url, wps_out_dir)
        if not exists or os.path.isfile(wps_out_ref):
            return wps_out_ref
    return None


@cache_region("request")
def _describe_process_cached(self, identifier, xml=None):
    # type: (WebProcessingService, str, Optional[xml_util.XML]) -> ProcessOWS
    LOGGER.debug("Request WPS DescribeProcess to [%s] with [id: %s]", self.url, identifier)
    return self.describeprocess_method(identifier, xml=xml)  # noqa  # method created by '_get_wps_client_cached'


@cache_region("request")
def _get_wps_client_cached(url, headers, verify, language):
    # type: (str, HeadersType, bool, Optional[str]) -> WebProcessingService
    LOGGER.debug("Request WPS GetCapabilities to [%s]", url)
    # cannot preset language because capabilities must be fetched to find best match
    wps = WebProcessingService(url=url, headers=headers, verify=verify, timeout=5)
    set_wps_language(wps, accept_language=language)
    setattr(wps, "describeprocess_method", wps.describeprocess)  # backup real method, them override with cached
    setattr(wps, "describeprocess", lambda *_, **__: _describe_process_cached(wps, *_, **__))
    return wps


@retry_on_cache_error
def get_wps_client(url, container=None, verify=None, headers=None, language=None):
    # type: (str, Optional[AnySettingsContainer], bool, Optional[HeadersType], Optional[str]) -> WebProcessingService
    """
    Obtains a :class:`WebProcessingService` with pre-configured request options for the given URL.

    :param url: WPS URL location.
    :param container: request or settings container to retrieve headers and other request options.
    :param verify: flag to enable SSL verification (overrides request options from container).
    :param headers: specific headers to apply (overrides retrieved ones from container).
    :param language: preferred response language if supported by the service.
    :returns: created WPS client object with configured request options.
    """
    if headers is None and hasattr(container, "headers"):
        headers = container.headers
    else:
        headers = headers or {}
    # remove invalid values that should be recomputed by the client as needed
    # employ the provided headers instead of making new ones in order to forward any language/authorization definition
    # copy to avoid modify original headers for sub-requests for next steps that could use them
    # employ dict() rather than deepcopy since headers that can be an instance of EnvironHeaders cannot be serialized
    headers = dict(headers)
    for header in ["Accept", "Content-Length", "Content-Type", "Content-Transfer-Encoding"]:
        hdr_low = header.lower()
        for hdr in [header, hdr_low, header.replace("-", "_"), hdr_low.replace("-", "_")]:
            headers.pop(hdr, None)
    opts = get_request_options("get", url, container)
    if verify is None:
        verify = get_ssl_verify_option("get", url, container, request_options=opts)
    # convert objects to allow caching keys against values (object instances always different)
    language = language or getattr(container, "accept_language", None) or get_header("Accept-Language", headers)
    if language is not None and not isinstance(language, str):
        language = str(language)
    if headers is not None and not isinstance(headers, dict):
        headers = dict(headers)
    request_args = (url, headers, verify, language)
    if get_no_cache_option(headers, request_options=opts):
        for func in (_get_wps_client_cached, _describe_process_cached):
            caching_args = (func, "request", *request_args)
            invalidate_region(caching_args)
    wps = _get_wps_client_cached(*request_args)
    return wps


def check_wps_status(location=None,     # type: Optional[str]
                     response=None,     # type: Optional[xml_util.XML]
                     sleep_secs=2,      # type: int
                     verify=True,       # type: bool
                     settings=None,     # type: Optional[AnySettingsContainer]
                     ):                 # type: (...) -> WPSExecution
    """
    Run :func:`owslib.wps.WPSExecution.checkStatus` with additional exception handling.

    :param location: job URL or file path where to look for job status.
    :param response: WPS response document of job status.
    :param sleep_secs: number of seconds to sleep before returning control to the caller.
    :param verify: flag to enable SSL verification.
    :param settings: application settings to retrieve any additional request parameters as applicable.
    :returns: OWSLib.wps.WPSExecution object.
    """
    def _retry_file():
        LOGGER.warning("Failed retrieving WPS status-location, attempting with local file.")
        out_path = get_wps_local_status_location(location, settings)
        if not out_path:
            raise HTTPNotFound("Could not find file resource from [{}].".format(location))
        LOGGER.info("Resolved WPS status-location using local file reference.")
        with open(out_path, "r") as f:
            return f.read()

    execution = WPSExecution()
    if response:
        LOGGER.debug("Retrieving WPS status from XML response document...")
        xml_data = response
    elif location:
        xml_resp = HTTPNotFound()
        try:
            LOGGER.debug("Attempt to retrieve WPS status-location from URL [%s]...", location)
            xml_resp = request_extra("get", location, verify=verify, settings=settings)
            xml_data = xml_resp.content
        except Exception as ex:
            LOGGER.debug("Got exception during get status: [%r]", ex)
            xml_data = _retry_file()
        if xml_resp.status_code == HTTPNotFound.code:
            LOGGER.debug("Got not-found during get status: [%r]", xml_data)
            xml_data = _retry_file()
    else:
        raise Exception("Missing status-location URL/file reference or response with XML object.")
    if isinstance(xml_data, str):
        xml_data = xml_data.encode("utf8", errors="ignore")
    execution.checkStatus(response=xml_data, sleepSecs=sleep_secs)
    if execution.response is None:
        raise Exception("Missing response, cannot check status.")
    if not isinstance(execution.response, xml_util.XML):
        execution.response = xml_util.fromstring(execution.response)
    return execution


def load_pywps_config(container, config=None):
    # type: (AnySettingsContainer, Optional[Union[str, Dict[str, str]]]) -> ConfigParser
    """
    Loads and updates the PyWPS configuration using Weaver settings.
    """
    settings = get_settings(container)
    if settings.get("weaver.wps_configured"):
        LOGGER.debug("Using preloaded internal Weaver WPS configuration.")
        return pywps_config.CONFIG

    LOGGER.info("Initial load of internal Weaver WPS configuration.")
    pywps_config.load_configuration([])  # load defaults
    pywps_config.CONFIG.set("logging", "db_echo", "false")
    if logging.getLevelName(pywps_config.CONFIG.get("logging", "level")) <= logging.DEBUG:
        pywps_config.CONFIG.set("logging", "level", "INFO")

    # update metadata
    LOGGER.debug("Updating WPS metadata configuration.")
    for setting_name, setting_value in settings.items():
        if setting_name.startswith("weaver.wps_metadata"):
            pywps_setting = setting_name.replace("weaver.wps_metadata_", "")
            pywps_config.CONFIG.set("metadata:main", pywps_setting, setting_value)
    # add weaver configuration keyword if not already provided
    wps_keywords = pywps_config.CONFIG.get("metadata:main", "identification_keywords")
    weaver_mode = get_weaver_configuration(settings)
    if weaver_mode not in wps_keywords:
        wps_keywords += ("," if wps_keywords else "") + weaver_mode
        pywps_config.CONFIG.set("metadata:main", "identification_keywords", wps_keywords)
    # add additional config passed as dictionary of {'section.key': 'value'}
    if isinstance(config, dict):
        for key, value in config.items():
            section, key = key.split(".")
            pywps_config.CONFIG.set(section, key, value)
        # cleanup alternative dict "PYWPS_CFG" which is not expected elsewhere
        if isinstance(settings.get("PYWPS_CFG"), dict):
            del settings["PYWPS_CFG"]

    # set accepted languages aligned with values provided by REST API endpoints
    # otherwise, execute request could fail due to languages considered not supported
    languages = ", ".join(ACCEPT_LANGUAGES)
    LOGGER.debug("Setting WPS languages: [%s]", languages)
    pywps_config.CONFIG.set("server", "language", languages)

    LOGGER.debug("Updating WPS output configuration.")
    # find output directory from app config or wps config
    if "weaver.wps_output_dir" not in settings:
        output_dir = pywps_config.get_config_value("server", "outputpath")
        settings["weaver.wps_output_dir"] = output_dir
    # ensure the output dir exists if specified
    output_dir = get_wps_output_dir(settings)
    make_dirs(output_dir, exist_ok=True)
    # find output url from app config (path/url) or wps config (url only)
    # note: needs to be configured even when using S3 bucket since XML status is provided locally
    if "weaver.wps_output_url" not in settings:
        output_path = settings.get("weaver.wps_output_path", "")
        if isinstance(output_path, str):
            output_url = os.path.join(get_weaver_url(settings), output_path.strip("/"))
        else:
            output_url = pywps_config.get_config_value("server", "outputurl")
        settings["weaver.wps_output_url"] = output_url
    # apply workdir if provided, otherwise use default
    if "weaver.wps_workdir" in settings:
        make_dirs(settings["weaver.wps_workdir"], exist_ok=True)
        pywps_config.CONFIG.set("server", "workdir", settings["weaver.wps_workdir"])

    # configure S3 bucket if requested, storage of all process outputs
    # note:
    #   credentials and default profile are picked up automatically by 'boto3' from local AWS configs or env vars
    #   region can also be picked from there unless explicitly provided by weaver config
    # warning:
    #   if we set `(server, storagetype, s3)`, ALL status (including XML) are stored to S3
    #   to preserve status locally, we set 'file' and override the storage instance during output rewrite in WpsPackage
    #   we can still make use of the server configurations here to make this overridden storage auto-find its configs
    s3_bucket = settings.get("weaver.wps_output_s3_bucket")
    pywps_config.CONFIG.set("server", "storagetype", "file")
    # pywps_config.CONFIG.set("server", "storagetype", "s3")
    if s3_bucket:
        LOGGER.debug("Updating WPS S3 bucket configuration.")
        import boto3
        from botocore.exceptions import ClientError
        s3 = boto3.client("s3")
        s3_region = settings.get("weaver.wps_output_s3_region", s3.meta.region_name)
        LOGGER.info("Validating that S3 [Bucket=%s, Region=%s] exists or creating it.", s3_bucket, s3_region)
        try:
            s3.create_bucket(Bucket=s3_bucket, CreateBucketConfiguration={"LocationConstraint": s3_region})
            LOGGER.info("S3 bucket for WPS output created.")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "BucketAlreadyExists":
                LOGGER.error("Failed setup of S3 bucket for WPS output: [%s]", exc)
                raise
            LOGGER.info("S3 bucket for WPS output already exists.")
        pywps_config.CONFIG.set("s3", "region", s3_region)
        pywps_config.CONFIG.set("s3", "bucket", s3_bucket)
        pywps_config.CONFIG.set("s3", "public", "false")  # don't automatically push results as publicly accessible
        pywps_config.CONFIG.set("s3", "encrypt", "true")  # encrypts data server-side, transparent from this side

    # enforce back resolved values onto PyWPS config
    pywps_config.CONFIG.set("server", "setworkdir", "true")
    pywps_config.CONFIG.set("server", "sethomedir", "true")
    pywps_config.CONFIG.set("server", "outputpath", settings["weaver.wps_output_dir"])
    pywps_config.CONFIG.set("server", "outputurl", settings["weaver.wps_output_url"])
    settings["weaver.wps_configured"] = True
    return pywps_config.CONFIG


def set_wps_language(wps, accept_language=None, request=None):
    # type: (WebProcessingService, Optional[str], Optional[AnyRequestType]) -> Optional[str]
    """
    Applies the best match between requested accept languages and supported ones by the WPS server.

    Given the `Accept-Language` header value, match the best language to the supported languages retrieved from WPS.
    By default, and if no match is found, sets :attr:`WebProcessingService.language` property to ``None``.

    .. seealso::
        Details about the format of the ``Accept-Language`` header:
        https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Accept-Language

    .. note::
        This function considers quality-factor weighting and parsing resolution of ``Accept-Language`` header
        according to :rfc:`RFC 7231, section 5.3.2 <7231#section-5.3.2>`.

    :param wps: service for which to apply a supported language if matched.
    :param str accept_language: value of the Accept-Language header.
    :param request: request from which to extract Accept-Language header if not provided directly.
    :returns: language that has been set, or ``None`` if no match could be found.
    """
    if not accept_language and request and hasattr(request, "accept_language"):
        accept_language = request.accept_language.header_value

    if not accept_language:
        return

    if not hasattr(wps, "languages"):
        # owslib version doesn't support setting a language
        return

    supported_languages = wps.languages.supported or ACCEPT_LANGUAGES
    language = create_accept_language_header(accept_language).best_match(supported_languages)
    if language:
        wps.language = language
    return language
