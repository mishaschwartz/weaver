"""
Definitions of types used by tokens.
"""
from weaver.exceptions import ProcessInstanceError
from weaver.processes.types import PROCESS_WITH_MAPPING, PROCESS_WPS
from weaver.status import STATUS_UNKNOWN, job_status_values
from weaver.utils import localize_datetime  # for backward compatibility of previously saved jobs not time-locale-aware
from weaver.utils import fully_qualified_name, get_job_log_msg, get_log_date_fmt, get_log_fmt, now
from weaver.visibility import VISIBILITY_PRIVATE, visibility_values
from weaver.wps_restapi import swagger_definitions as sd

import six
from dateutil.parser import parse as dt_parse  # noqa
from owslib.wps import WPSException
from pywps import Process as ProcessWPS

import traceback
import uuid
from datetime import datetime, timedelta
from logging import ERROR, INFO, getLevelName, getLogger
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from weaver.typedefs import Number, LoggerType, CWL, JSON       # noqa: F401
    from typing import Any, AnyStr, Dict, List, Optional, Union     # noqa: F401

LOGGER = getLogger(__name__)


class Base(dict):
    """
    Dictionary with extended attributes auto-``getter``/``setter`` for convenience.
    Explicitly overridden ``getter``/``setter`` attributes are called instead of ``dict``-key ``get``/``set``-item
    to ensure corresponding checks and/or value adjustments are executed before applying it to the sub-``dict``.
    """
    def __setattr__(self, item, value):
        # use the existing property setter if defined
        prop = getattr(type(self), item)
        if isinstance(prop, property) and prop.fset is not None:
            prop.fset(self, value)  # noqa
        elif item in self:
            self[item] = value
        else:
            raise AttributeError("Can't set attribute '{}'.".format(item))

    def __getattr__(self, item):
        # use existing property getter if defined
        prop = getattr(type(self), item)
        if isinstance(prop, property) and prop.fget is not None:
            return prop.fget(self, item)  # noqa
        elif item in self:
            return self[item]
        else:
            raise AttributeError("Can't get attribute '{}'.".format(item))

    def __str__(self):
        # type: () -> AnyStr
        return "{0} <{1}>".format(type(self).__name__, self.id)

    def __repr__(self):
        # type: () -> AnyStr
        cls = type(self)
        repr_ = dict.__repr__(self)
        return "{0}.{1} ({2})".format(cls.__module__, cls.__name__, repr_)

    @property
    def id(self):
        raise NotImplementedError()


class Service(Base):
    """
    Dictionary that contains OWS services. It always has ``url`` key.
    """

    def __init__(self, *args, **kwargs):
        super(Service, self).__init__(*args, **kwargs)
        if "name" not in self:
            raise TypeError("Service 'name' is required")
        if "url" not in self:
            raise TypeError("Service 'url' is required")

    @property
    def id(self):
        return self.name

    @property
    def url(self):
        """Service URL."""
        return self["url"]

    @property
    def name(self):
        """Service name."""
        return self["name"]

    @property
    def type(self):
        """Service type."""
        return self.get("type", "WPS")

    @property
    def public(self):
        """Flag if service has public access."""
        # TODO: public access can be set via auth parameter.
        return self.get("public", False)

    @property
    def auth(self):
        """Authentication method: public, token, cert."""
        return self.get("auth", "token")

    @property
    def params(self):
        return {
            "url": self.url,
            "name": self.name,
            "type": self.type,
            "public": self.public,
            "auth": self.auth
        }


class Job(Base):
    """
    Dictionary that contains OWS service jobs. It always has ``id`` and ``task_id`` keys.
    """

    def __init__(self, *args, **kwargs):
        super(Job, self).__init__(*args, **kwargs)
        if "task_id" not in self:
            raise TypeError("Parameter 'task_id' is required for '{}' creation.".format(type(self)))
        if not isinstance(self.id, six.string_types):
            raise TypeError("Type 'str' is required for '{}.id'".format(type(self)))

    def _get_log_msg(self, msg=None):
        # type: (Optional[AnyStr]) -> AnyStr
        if not msg:
            msg = self.status_message
        return get_job_log_msg(duration=self.duration, progress=self.progress, status=self.status, message=msg)

    def save_log(self, errors=None, logger=None, message=None):
        # type: (Optional[Union[AnyStr, List[WPSException]]], Optional[LoggerType], Optional[AnyStr]) -> None
        if isinstance(errors, six.string_types):
            log_msg = [(ERROR, self._get_log_msg(message))]
            self.exceptions.append(errors)
        elif isinstance(errors, list):
            log_msg = [(ERROR, self._get_log_msg("{0.text} - code={0.code} - locator={0.locator}".format(error)))
                       for error in errors]
            self.exceptions.extend([{
                "Code": error.code,
                "Locator": error.locator,
                "Text": error.text
            } for error in errors])
        else:
            log_msg = [(INFO, self._get_log_msg(message))]
        for level, msg in log_msg:
            fmt_msg = get_log_fmt() % dict(asctime=now().strftime(get_log_date_fmt()),
                                           levelname=getLevelName(level),
                                           name=fully_qualified_name(self),
                                           message=msg)
            if len(self.logs) == 0 or self.logs[-1] != fmt_msg:
                self.logs.append(fmt_msg)
                if logger:
                    logger.log(level, msg)

    @property
    def id(self):
        # type: () -> AnyStr
        job_id = self.get("id")
        if not job_id:
            job_id = str(uuid.uuid4())
            self["id"] = job_id
        return job_id

    @property
    def task_id(self):
        # type: () -> AnyStr
        return self["task_id"]

    @task_id.setter
    def task_id(self, task_id):
        # type: (AnyStr) -> None
        if not isinstance(task_id, six.string_types):
            raise TypeError("Type 'str' is required for '{}.task_id'".format(type(self)))
        self["task_id"] = task_id

    @property
    def service(self):
        # type: () -> Optional[AnyStr]
        return self.get("service", None)

    @service.setter
    def service(self, service):
        # type: (Optional[AnyStr]) -> None
        if not isinstance(service, six.string_types) or service is None:
            raise TypeError("Type 'str' is required for '{}.service'".format(type(self)))
        self["service"] = service

    @property
    def process(self):
        # type: () -> Optional[AnyStr]
        return self.get("process", None)

    @process.setter
    def process(self, process):
        # type: (Optional[AnyStr]) -> None
        if not isinstance(process, six.string_types) or process is None:
            raise TypeError("Type 'str' is required for '{}.process'".format(type(self)))
        self["process"] = process

    def _get_inputs(self):
        # type: () -> List[Optional[Dict[AnyStr, Any]]]
        if self.get("inputs") is None:
            self["inputs"] = list()
        return self["inputs"]

    def _set_inputs(self, inputs):
        # type: (List[Optional[Dict[AnyStr, Any]]]) -> None
        if not isinstance(inputs, list):
            raise TypeError("Type 'list' is required for '{}.inputs'".format(type(self)))
        self["inputs"] = inputs

    # allows to correctly update list by ref using 'job.inputs.extend()'
    inputs = property(_get_inputs, _set_inputs)

    @property
    def user_id(self):
        # type: () -> Optional[AnyStr]
        return self.get("user_id", None)

    @user_id.setter
    def user_id(self, user_id):
        # type: (Optional[AnyStr]) -> None
        if not isinstance(user_id, int) or user_id is None:
            raise TypeError("Type 'int' is required for '{}.user_id'".format(type(self)))
        self["user_id"] = user_id

    @property
    def status(self):
        # type: () -> AnyStr
        return self.get("status", STATUS_UNKNOWN)

    @status.setter
    def status(self, status):
        # type: (AnyStr) -> None
        if status == "accepted" and self.status == "running":
            LOGGER.debug(traceback.extract_stack())
        if not isinstance(status, six.string_types):
            raise TypeError("Type 'str' is required for '{}.status'".format(type(self)))
        if status not in job_status_values:
            raise ValueError("Status '{0}' is not valid for '{1}.status', must be one of {2!s}'"
                             .format(status, type(self), list(job_status_values)))
        self["status"] = status

    @property
    def status_message(self):
        # type: () -> AnyStr
        return self.get("status_message", "no message")

    @status_message.setter
    def status_message(self, message):
        # type: (Optional[AnyStr]) -> None
        if message is None:
            return
        if not isinstance(message, six.string_types):
            raise TypeError("Type 'str' is required for '{}.status_message'".format(type(self)))
        self["status_message"] = message

    @property
    def status_location(self):
        # type: () -> Optional[AnyStr]
        return self.get("status_location", None)

    @status_location.setter
    def status_location(self, location_url):
        # type: (Optional[AnyStr]) -> None
        if not isinstance(location_url, six.string_types) or location_url is None:
            raise TypeError("Type 'str' is required for '{}.status_location'".format(type(self)))
        self["status_location"] = location_url

    @property
    def notification_email(self):
        # type: () -> Optional[AnyStr]
        return self.get("notification_email")

    @notification_email.setter
    def notification_email(self, email):
        # type: (Optional[Union[AnyStr]]) -> None
        if not isinstance(email, six.string_types):
            raise TypeError("Type 'str' is required for '{}.notification_email'".format(type(self)))
        self["notification_email"] = email

    @property
    def execute_async(self):
        # type: () -> bool
        return self.get("execute_async", True)

    @execute_async.setter
    def execute_async(self, execute_async):
        # type: (bool) -> None
        if not isinstance(execute_async, bool):
            raise TypeError("Type 'bool' is required for '{}.execute_async'".format(type(self)))
        self["execute_async"] = execute_async

    @property
    def is_workflow(self):
        # type: () -> bool
        return self.get("is_workflow", False)

    @is_workflow.setter
    def is_workflow(self, is_workflow):
        # type: (bool) -> None
        if not isinstance(is_workflow, bool):
            raise TypeError("Type 'bool' is required for '{}.is_workflow'".format(type(self)))
        self["is_workflow"] = is_workflow

    @property
    def created(self):
        # type: () -> datetime
        created = self.get("created", None)
        if not created:
            self["created"] = now()
        return localize_datetime(self.get("created"))

    @property
    def finished(self):
        # type: () -> Optional[AnyStr]
        return self.get("finished", None)

    def is_finished(self):
        # type: () -> bool
        return self.finished is not None

    def mark_finished(self):
        # type: () -> None
        self["finished"] = now()

    @property
    def duration(self):
        # type: () -> AnyStr
        final_time = self.finished or now()
        duration = localize_datetime(final_time) - localize_datetime(self.created)
        self["duration"] = str(duration).split('.')[0]
        return self["duration"]

    @property
    def progress(self):
        # type: () -> Number
        return self.get("progress", 0)

    @progress.setter
    def progress(self, progress):
        # type: (Number) -> None
        if not isinstance(progress, (int, float)):
            raise TypeError("Number is required for '{}.progress'".format(type(self)))
        if progress < 0 or progress > 100:
            raise ValueError("Value must be in range [0,100] for '{}.progress'".format(type(self)))
        self["progress"] = progress

    def _get_results(self):
        # type: () -> List[Optional[Dict[AnyStr, Any]]]
        if self.get("results") is None:
            self["results"] = list()
        return self["results"]

    def _set_results(self, results):
        # type: (List[Optional[Dict[AnyStr, Any]]]) -> None
        if not isinstance(results, list):
            raise TypeError("Type 'list' is required for '{}.results'".format(type(self)))
        self["results"] = results

    # allows to correctly update list by ref using 'job.results.extend()'
    results = property(_get_results, _set_results)

    def _get_exceptions(self):
        # type: () -> List[Optional[Dict[AnyStr, AnyStr]]]
        if self.get("exceptions") is None:
            self["exceptions"] = list()
        return self["exceptions"]

    def _set_exceptions(self, exceptions):
        # type: (List[Optional[Dict[AnyStr, AnyStr]]]) -> None
        if not isinstance(exceptions, list):
            raise TypeError("Type 'list' is required for '{}.exceptions'".format(type(self)))
        self["exceptions"] = exceptions

    # allows to correctly update list by ref using 'job.exceptions.extend()'
    exceptions = property(_get_exceptions, _set_exceptions)

    def _get_logs(self):
        # type: () -> List[Optional[Dict[AnyStr, AnyStr]]]
        if self.get("logs") is None:
            self["logs"] = list()
        return self["logs"]

    def _set_logs(self, logs):
        # type: (List[Optional[Dict[AnyStr, AnyStr]]]) -> None
        if not isinstance(logs, list):
            raise TypeError("Type 'list' is required for '{}.logs'".format(type(self)))
        self["logs"] = logs

    # allows to correctly update list by ref using 'job.logs.extend()'
    logs = property(_get_logs, _set_logs)

    def _get_tags(self):
        # type: () -> List[Optional[AnyStr]]
        if self.get("tags") is None:
            self["tags"] = list()
        return self["tags"]

    def _set_tags(self, tags):
        # type: (List[Optional[AnyStr]]) -> None
        if not isinstance(tags, list):
            raise TypeError("Type 'list' is required for '{}.tags'".format(type(self)))
        self["tags"] = tags

    # allows to correctly update list by ref using 'job.tags.extend()'
    tags = property(_get_tags, _set_tags)

    @property
    def access(self):
        # type: () -> AnyStr
        """Job visibility access from execution."""
        return self.get("access", VISIBILITY_PRIVATE)

    @access.setter
    def access(self, visibility):
        # type: (AnyStr) -> None
        """Job visibility access from execution."""
        if not isinstance(visibility, six.string_types):
            raise TypeError("Type 'str' is required for '{}.access'".format(type(self)))
        if visibility not in visibility_values:
            raise ValueError("Invalid 'visibility' value specified for '{}.access'".format(type(self)))
        self["access"] = visibility

    @property
    def request(self):
        # type: () -> Optional[AnyStr]
        """XML request for WPS execution submission as string."""
        return self.get("request", None)

    @request.setter
    def request(self, request):
        # type: (Optional[AnyStr]) -> None
        """XML request for WPS execution submission as string."""
        self["request"] = request

    @property
    def response(self):
        # type: () -> Optional[AnyStr]
        """XML status response from WPS execution submission as string."""
        return self.get("response", None)

    @response.setter
    def response(self, response):
        # type: (Optional[AnyStr]) -> None
        """XML status response from WPS execution submission as string."""
        self["response"] = response

    @property
    def params(self):
        # type: () -> Dict[AnyStr, Any]
        return {
            "id": self.id,
            "task_id": self.task_id,
            "service": self.service,
            "process": self.process,
            "inputs": self.inputs,
            "user_id": self.user_id,
            "status": self.status,
            "status_message": self.status_message,
            "status_location": self.status_location,
            "execute_async": self.execute_async,
            "is_workflow": self.is_workflow,
            "created": self.created,
            "finished": self.finished,
            "duration": self.duration,
            "progress": self.progress,
            "results": self.results,
            "exceptions": self.exceptions,
            "logs": self.logs,
            "tags": self.tags,
            "access": self.access,
            "request": self.request,
            "response": self.response,
            "notification_email": self.notification_email,
        }


class Process(Base):
    # pylint: disable=C0103,invalid-name
    """
    Dictionary that contains a process description for db storage.
    It always has ``identifier`` and ``processEndpointWPS1`` keys.
    """

    def __init__(self, *args, **kwargs):
        super(Process, self).__init__(*args, **kwargs)
        # use both 'id' and 'identifier' to support any call (WPS and recurrent 'id')
        if "id" not in self and "identifier" not in self:
            raise TypeError("'id' OR 'identifier' is required")
        if "id" not in self:
            self["id"] = self.pop("identifier")
        if "package" not in self:
            raise TypeError("'package' is required")
        setattr(self, "package", self.pop("package"))           # force encode
        setattr(self, "payload", self.pop("payload", None))     # force encode

    @property
    def id(self):
        # type: () -> AnyStr
        return self["id"]

    @property
    def identifier(self):
        # type: () -> AnyStr
        return self.id

    @identifier.setter
    def identifier(self, value):
        # type: (AnyStr) -> None
        self["id"] = value

    @property
    def title(self):
        # type: () -> AnyStr
        return self.get("title", self.id)

    @property
    def abstract(self):
        # type: () -> AnyStr
        return self.get("abstract", "")

    @property
    def keywords(self):
        # type: () -> List[AnyStr]
        return self.get("keywords", [])

    @property
    def metadata(self):
        # type: () -> List[AnyStr]
        return self.get("metadata", [])

    @property
    def version(self):
        # type: () -> Optional[AnyStr]
        return self.get("version")

    @property
    def inputs(self):
        # type: () -> Optional[List[Dict[AnyStr, Any]]]
        return self.get("inputs")

    @property
    def outputs(self):
        # type: () -> Optional[List[Dict[AnyStr, Any]]]
        return self.get("outputs")

    @property
    def jobControlOptions(self):  # noqa: N802
        # type: () -> Optional[List[AnyStr]]
        return self.get("jobControlOptions")

    @property
    def outputTransmission(self):  # noqa: N802
        # type: () -> Optional[List[AnyStr]]
        return self.get("outputTransmission")

    @property
    def processDescriptionURL(self):  # noqa: N802
        # type: () -> Optional[AnyStr]
        return self.get("processDescriptionURL")

    @property
    def processEndpointWPS1(self):  # noqa: N802
        # type: () -> Optional[AnyStr]
        return self.get("processEndpointWPS1")

    @property
    def executeEndpoint(self):  # noqa: N802
        # type: () -> Optional[AnyStr]
        return self.get("executeEndpoint")

    @property
    def owsContext(self):  # noqa: N802
        # type: () -> Optional[JSON]
        return self.get("owsContext")

    # wps, workflow, etc.
    @property
    def type(self):
        # type: () -> AnyStr
        return self.get("type", PROCESS_WPS)

    @property
    def package(self):
        # type: () -> Optional[CWL]
        pkg = self.get("package")
        return self._decode(pkg) if isinstance(pkg, dict) else pkg

    @package.setter
    def package(self, pkg):
        self["package"] = self._encode(pkg) if isinstance(pkg, dict) else pkg

    @property
    def payload(self):
        # type: () -> JSON
        body = self.get("payload", dict())
        return self._decode(body) if isinstance(body, dict) else body

    @payload.setter
    def payload(self, body):
        # type: (JSON) -> None
        self["payload"] = self._encode(body) if isinstance(body, dict) else dict()

    # encode(->)/decode(<-) characters that cannot be in a key during save to db
    _character_codes = [('$', "\uFF04"), ('.', "\uFF0E")]

    def _recursive_replace(self, pkg, index_from, index_to):
        # type: (CWL, int, int) -> CWL
        new = {}
        for k in pkg:
            # find modified key with replace matches
            c_k = k
            for c in self._character_codes:
                c_f = c[index_from]
                c_t = c[index_to]
                if c_f in k:
                    c_k = k.replace(c_f, c_t)
            # process recursive sub-items
            if isinstance(pkg[k], dict):
                pkg[k] = self._recursive_replace(pkg[k], index_from, index_to)
            if isinstance(pkg[k], list):
                for i, pkg_i in enumerate(pkg[k]):
                    if isinstance(pkg_i, dict):
                        pkg[k][i] = self._recursive_replace(pkg[k][i], index_from, index_to)
            # apply new key to obtained sub-items with replaced keys as needed
            new[c_k] = pkg[k]   # note: cannot use pop when using pkg keys iterator (python 3)
        return new

    def _encode(self, pkg):
        # type: (CWL) -> CWL
        return self._recursive_replace(pkg, 0, 1)

    def _decode(self, pkg):
        # type: (CWL) -> CWL
        return self._recursive_replace(pkg, 1, 0)

    @property
    def visibility(self):
        # type: () -> AnyStr
        return self.get("visibility", VISIBILITY_PRIVATE)

    @visibility.setter
    def visibility(self, visibility):
        # type: (AnyStr) -> None
        if not isinstance(visibility, six.string_types):
            raise TypeError("Type 'str' is required for '{}.visibility'".format(type(self)))
        if visibility not in visibility_values:
            raise ValueError("Status '{0}' is not valid for '{1}.visibility, must be one of {2!s}'"
                             .format(visibility, type(self), list(visibility_values)))
        self["visibility"] = visibility

    @property
    def params(self):
        # type: () -> Dict[AnyStr, Any]
        return {
            "identifier": self.identifier,
            "title": self.title,
            "abstract": self.abstract,
            "keywords": self.keywords,
            "metadata": self.metadata,
            "version": self.version,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "jobControlOptions": self.jobControlOptions,
            "outputTransmission": self.outputTransmission,
            "processEndpointWPS1": self.processEndpointWPS1,
            "processDescriptionURL": self.processDescriptionURL,
            "executeEndpoint": self.executeEndpoint,
            "owsContext": self.owsContext,
            "type": self.type,
            "package": self.package,  # deployment specification (json body)
            "payload": self.payload,
            "visibility": self.visibility,
        }

    @property
    def params_wps(self):
        # type: () -> Dict[AnyStr, Any]
        """Values applicable to PyWPS Process ``__init__``"""
        return {
            "identifier": self.identifier,
            "title": self.title,
            "abstract": self.abstract,
            "keywords": self.keywords,
            "metadata": self.metadata,
            "version": self.version,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "package": self.package,
            "payload": self.payload,
        }

    def json(self):
        # type: () -> JSON
        return sd.Process().deserialize(self)

    def process_offering(self):
        # type: () -> JSON
        process_offering = {"process": self}
        if self.version:
            process_offering.update({"processVersion": self.version})
        if self.jobControlOptions:
            process_offering.update({"jobControlOptions": self.jobControlOptions})
        if self.outputTransmission:
            process_offering.update({"outputTransmission": self.outputTransmission})
        return sd.ProcessOffering().deserialize(process_offering)

    def process_summary(self):
        # type: () -> JSON
        return sd.ProcessSummary().deserialize(self)

    @staticmethod
    def from_wps(wps_process, **extra_params):
        # type: (ProcessWPS, **Any) -> Process
        """
        Converts a PyWPS Process into a :class:`weaver.datatype.Process` using provided parameters.
        """
        # import here to avoid circular dependencies
        # noinspection PyProtectedMember
        from weaver.processes.wps_package import _wps2json_io

        assert isinstance(wps_process, ProcessWPS)
        process = wps_process.json
        process_type = getattr(wps_process, "type", wps_process.identifier)
        process.update({"type": process_type, "package": None, "reference": None,
                        "inputs": [_wps2json_io(i) for i in wps_process.inputs],
                        "outputs": [_wps2json_io(o) for o in wps_process.outputs]})
        process.update(**extra_params)
        return Process(process)

    def wps(self):
        # type: () -> ProcessWPS

        # import here to avoid circular dependencies
        from weaver.processes import process_mapping

        process_key = self.type
        if self.type == PROCESS_WPS:
            process_key = self.identifier
        if process_key not in process_mapping:
            ProcessInstanceError("Unknown process '{}' in mapping.".format(process_key))
        if process_key in PROCESS_WITH_MAPPING:
            return process_mapping[process_key](**self.params_wps)
        return process_mapping[process_key]()


class Quote(Base):
    # pylint: disable=C0103,invalid-name
    """
    Dictionary that contains quote information.
    It always has ``id`` and ``process`` keys.
    """

    def __init__(self, *args, **kwargs):
        super(Quote, self).__init__(*args, **kwargs)
        if "process" not in self:
            raise TypeError("Field 'Quote.process' is required")
        elif not isinstance(self.get("process"), six.string_types):
            raise ValueError("Field 'Quote.process' must be a string.")
        if "user" not in self:
            raise TypeError("Field 'Quote.user' is required")
        elif not isinstance(self.get("user"), six.string_types):
            raise ValueError("Field 'Quote.user' must be a string.")
        if "price" not in self:
            raise TypeError("Field 'Quote.price' is required")
        elif not isinstance(self.get("price"), float):
            raise ValueError("Field 'Quote.price' must be a float number.")
        if "currency" not in self:
            raise TypeError("Field 'Quote.currency' is required")
        elif not isinstance(self.get("currency"), six.string_types) or len(self.get("currency")) != 3:
            raise ValueError("Field 'Quote.currency' must be an ISO-4217 currency string code.")
        if "created" not in self:
            self["created"] = now()
        try:
            self["created"] = dt_parse(str(self.get("created"))).isoformat()
        except ValueError:
            raise ValueError("Field 'Quote.created' must be an ISO-8601 datetime string.")
        if "expire" not in self:
            self["expire"] = now() + timedelta(days=1)
        try:
            self["expire"] = dt_parse(str(self.get("expire"))).isoformat()
        except ValueError:
            raise ValueError("Field 'Quote.expire' must be an ISO-8601 datetime string.")
        if "id" not in self:
            self["id"] = str(uuid.uuid4())

    @property
    def id(self):
        """Quote ID."""
        return self["id"]

    @property
    def title(self):
        """Quote title."""
        return self.get("title")

    @property
    def description(self):
        """Quote description."""
        return self.get("description")

    @property
    def details(self):
        """Quote details."""
        return self.get("details")

    @property
    def user(self):
        """User ID requesting the quote"""
        return self["user"]

    @property
    def process(self):
        """WPS Process ID."""
        return self["process"]

    @property
    def estimatedTime(self):  # noqa: N802
        """Process estimated time."""
        return self.get("estimatedTime")

    @property
    def processParameters(self):  # noqa: N802
        """Process execution parameters for quote."""
        return self.get("processParameters")

    @property
    def location(self):
        """WPS Process URL."""
        return self.get("location", "")

    @property
    def price(self):
        """Price of the current quote"""
        return self.get("price", 0.0)

    @property
    def currency(self):
        """Currency of the quote price"""
        return self.get("currency")

    @property
    def expire(self):
        """Quote expiration datetime."""
        return self.get("expire")

    @property
    def created(self):
        """Quote creation datetime."""
        return self.get("created")

    @property
    def steps(self):
        """Sub-quote IDs if applicable"""
        return self.get("steps", [])

    @property
    def params(self):
        return {
            "id": self.id,
            "price": self.price,
            "currency": self.currency,
            "user": self.user,
            "process": self.process,
            "location": self.location,
            "steps": self.steps,
            "title": self.title,
            "description": self.description,
            "details": self.details,
            "created": self.created,
            "expire": self.expire,
            "estimatedTime": self.estimatedTime,
            "processParameters": self.processParameters,
        }

    def json(self):
        return self.params


class Bill(Base):
    """
    Dictionary that contains bill information.
    It always has ``id``, ``user``, ``quote`` and ``job`` keys.
    """

    def __init__(self, *args, **kwargs):
        super(Bill, self).__init__(*args, **kwargs)
        if "quote" not in self:
            raise TypeError("Field 'Bill.quote' is required")
        elif not isinstance(self.get("quote"), six.string_types):
            raise ValueError("Field 'Bill.quote' must be a string.")
        if "job" not in self:
            raise TypeError("Field 'Bill.job' is required")
        elif not isinstance(self.get("job"), six.string_types):
            raise ValueError("Field 'Bill.job' must be a string.")
        if "user" not in self:
            raise TypeError("Field 'Bill.user' is required")
        elif not isinstance(self.get("user"), six.string_types):
            raise ValueError("Field 'Bill.user' must be a string.")
        if "price" not in self:
            raise TypeError("Field 'Bill.price' is required")
        elif not isinstance(self.get("price"), float):
            raise ValueError("Field 'Bill.price' must be a float number.")
        if "currency" not in self:
            raise TypeError("Field 'Bill.currency' is required")
        elif not isinstance(self.get("currency"), six.string_types) or len(self.get("currency")) != 3:
            raise ValueError("Field 'Bill.currency' must be an ISO-4217 currency string code.")
        if "created" not in self:
            self["created"] = now()
        try:
            self["created"] = dt_parse(str(self.get("created"))).isoformat()
        except ValueError:
            raise ValueError("Field 'Bill.created' must be an ISO-8601 datetime string.")
        if "id" not in self:
            self["id"] = str(uuid.uuid4())

    @property
    def id(self):
        """Bill ID."""
        return self["id"]

    @property
    def user(self):
        """User ID"""
        return self["user"]

    @property
    def quote(self):
        """Quote ID."""
        return self["quote"]

    @property
    def job(self):
        """Job ID."""
        return self["job"]

    @property
    def price(self):
        """Price of the current quote"""
        return self.get("price", 0.0)

    @property
    def currency(self):
        """Currency of the quote price"""
        return self.get("currency")

    @property
    def created(self):
        """Quote creation datetime."""
        return self.get("created")

    @property
    def title(self):
        """Quote title."""
        return self.get("title")

    @property
    def description(self):
        """Quote description."""
        return self.get("description")

    @property
    def params(self):
        return {
            "id": self.id,
            "user": self.user,
            "quote": self.quote,
            "job": self.job,
            "price": self.price,
            "currency": self.currency,
            "created": self.created,
            "title": self.title,
            "description": self.description,
        }

    def json(self):
        return self.params
