"""
Utility methods for various TestCase setup operations.
"""
from weaver.datatype import Service
from weaver.database import get_db
from weaver.store.mongodb import MongodbServiceStore, MongodbProcessStore, MongodbJobStore
from weaver.wps_restapi.processes.processes import execute_process
from weaver.config import WEAVER_CONFIGURATION_DEFAULT
from weaver.utils import null
from weaver.wps import get_wps_url, get_wps_output_url, get_wps_output_dir
from weaver.warning import MissingParameterWarning, UnsupportedOperationWarning
from six.moves.configparser import ConfigParser
from typing import Any, AnyStr, Optional, TYPE_CHECKING
from pyramid import testing
from pyramid.registry import Registry
from pyramid.config import Configurator
# noinspection PyPackageRequirements
from webtest import TestApp
import pyramid_celery
import warnings
# noinspection PyPackageRequirements
import mock
import uuid
import os
if TYPE_CHECKING:
    from weaver.typedefs import SettingsType


def ignore_wps_warnings(func):
    """Wrapper that eliminates WPS related warnings during testing logging.

    **NOTE**:
        Wrapper should be applied on method (not directly on :class:`unittest.TestCase`
        as it can disable the whole test suite.
    """
    def do_test(self, *args, **kwargs):
        with warnings.catch_warnings():
            for warn in [MissingParameterWarning, UnsupportedOperationWarning]:
                for msg in ["Parameter 'request*", "Parameter 'service*", "Request type '*", "Service '*"]:
                    warnings.filterwarnings(action="ignore", message=msg, category=warn)
            func(self, *args, **kwargs)
    return do_test


def ignore_deprecated_nested_warnings(func):
    """Wrapper that eliminates :function:`contextlib.nested` related warnings during testing logging.

    **NOTE**:
        Wrapper should be applied on method (not directly on :class:`unittest.TestCase`
        as it can disable the whole test suite.
    """
    def do_test(self, *args, **kwargs):
        with warnings.catch_warnings():
            warnings.filterwarnings(action="ignore", category=DeprecationWarning,
                                    message="With-statements now directly support multiple context managers")
            func(self, *args, **kwargs)
    return do_test


def get_settings_from_config_ini(config_ini_path=None, ini_section_name="app:main"):
    # type: (Optional[AnyStr], Optional[AnyStr]) -> SettingsType
    parser = ConfigParser()
    parser.read([config_ini_path or get_default_config_ini_path()])
    settings = dict(parser.items(ini_section_name))
    return settings


def get_default_config_ini_path():
    # type: (...) -> AnyStr
    return os.path.expanduser("~/birdhouse/etc/weaver/weaver.ini")


def setup_config_from_settings(settings=None):
    # type: (Optional[SettingsType]) -> Configurator
    settings = settings or {}
    config = testing.setUp(settings=settings)
    return config


def setup_config_from_ini(config_ini_file_path=None):
    # type: (Optional[AnyStr]) -> Configurator
    config_ini_file_path = config_ini_file_path or get_default_config_ini_path()
    settings = get_settings_from_config_ini(config_ini_file_path, "app:main")
    settings.update(get_settings_from_config_ini(config_ini_file_path, "celery"))
    config = testing.setUp(settings=settings)
    return config


def setup_config_with_mongodb(config=None, settings=None):
    # type: (Optional[Configurator], Optional[SettingsType]) -> Configurator
    settings = settings or {}
    settings.update({
        "mongodb.host":     os.getenv("WEAVER_TEST_DB_HOST", "127.0.0.1"),      # noqa: E241
        "mongodb.port":     os.getenv("WEAVER_TEST_DB_PORT", "27017"),          # noqa: E241
        "mongodb.db_name":  os.getenv("WEAVER_TEST_DB_NAME", "weaver-test"),    # noqa: E241
    })
    if config:
        config.registry.settings.update(settings)
    else:
        config = get_test_weaver_config(settings=settings)
    return config


def setup_mongodb_servicestore(config=None):
    # type: (Optional[Configurator]) -> MongodbServiceStore
    """Setup store using mongodb, will be enforced if not configured properly."""
    config = setup_config_with_mongodb(config)
    store = get_db(config).get_store(MongodbServiceStore)
    store.clear_services()
    # noinspection PyTypeChecker
    return store


def setup_mongodb_processstore(config=None):
    # type: (Optional[Configurator]) -> MongodbProcessStore
    """Setup store using mongodb, will be enforced if not configured properly."""
    config = setup_config_with_mongodb(config)
    store = get_db(config).get_store(MongodbProcessStore)
    store.clear_processes()
    # store must be recreated after clear because processes are added automatically on __init__
    # noinspection PyProtectedMember
    get_db(config)._stores.pop(MongodbProcessStore.type)
    store = get_db(config).get_store(MongodbProcessStore)
    # noinspection PyTypeChecker
    return store


def setup_mongodb_jobstore(config=None):
    # type: (Optional[Configurator]) -> MongodbJobStore
    """Setup store using mongodb, will be enforced if not configured properly."""
    config = setup_config_with_mongodb(config)
    store = get_db(config).get_store(MongodbJobStore)
    store.clear_jobs()
    # noinspection PyTypeChecker
    return store


def setup_config_with_pywps(config):
    # type: (Configurator) -> Configurator
    settings = config.get_settings()
    settings.update({
        "PYWPS_CFG": {
            "server.url": get_wps_url(settings),
            "server.outputurl": get_wps_output_url(settings),
            "server.outputpath": get_wps_output_dir(settings),
        },
    })
    config.registry.settings.update(settings)
    config.include("weaver.wps")
    return config


def setup_config_with_celery(config):
    # type: (Configurator) -> Configurator
    settings = config.get_settings()

    # override celery loader to specify configuration directly instead of ini file
    celery_settings = {
        "CELERY_BROKER_URL": "mongodb://{}:{}/celery".format(settings.get("mongodb.host"), settings.get("mongodb.port"))
    }
    pyramid_celery.loaders.INILoader.read_configuration = mock.MagicMock(return_value=celery_settings)
    config.include("pyramid_celery")
    config.configure_celery("")  # value doesn't matter because overloaded
    return config


def get_test_weaver_config(config=None, settings=None):
    # type: (Optional[Configurator], Optional[SettingsType]) -> Configurator
    if not config:
        # default db required if none specified by config
        config = setup_config_from_settings(settings=settings)
    if "weaver.configuration" not in config.registry.settings:
        config.registry.settings["weaver.configuration"] = WEAVER_CONFIGURATION_DEFAULT
    if "weaver.url" not in config.registry.settings:
        config.registry.settings["weaver.url"] = "https://localhost"
    if settings:
        config.registry.settings.update(settings)
    # create the test application
    config.include("weaver")
    return config


def get_test_weaver_app(config=None, settings=None):
    # type: (Optional[Configurator], Optional[SettingsType]) -> TestApp
    config = get_test_weaver_config(config=config, settings=settings)
    config.scan()
    return TestApp(config.make_wsgi_app())


def get_settings_from_testapp(testapp):
    # type: (TestApp) -> SettingsType
    settings = {}
    if hasattr(testapp.app, "registry"):
        settings = testapp.app.registry.settings or {}
    return settings


def get_setting(env_var_name, app=None, setting_name=None):
    # type: (AnyStr, Optional[TestApp], Optional[AnyStr]) -> Any
    val = os.getenv(env_var_name, null)
    if val != null:
        return val
    if app:
        val = app.extra_environ.get(env_var_name, null)
        if val != null:
            return val
        if setting_name:
            val = app.extra_environ.get(setting_name, null)
            if val != null:
                return val
            settings = get_settings_from_testapp(app)
            if settings:
                val = settings.get(setting_name, null)
                if val != null:
                    return val
    return null


def init_weaver_service(registry):
    # type: (Registry) -> None
    service_store = registry.db.get_store(MongodbServiceStore)
    service_store.save_service(Service({
        "type": "",
        "name": "weaver",
        "url": "http://localhost/ows/proxy/weaver",
        "public": True
    }))


def mocked_sub_requests(app, function="get", *args, **kwargs):
    """
    Executes ``app.function(*args, **kwargs)`` with a mock of every underlying :function:`requests.request` call
    to relay their execution to the :class:`webTest.TestApp`.
    """
    # noinspection PyUnusedLocal
    def mocked_request(method, url_base, headers=None, verify=None, cert=None, **req_kwargs):
        """
        Request corresponding to :function:`requests.request` that instead gets executed by :class:`webTest.TestApp`.
        """
        method = method.lower()
        req = getattr(app, method)
        url = url_base
        qs = req_kwargs.get("params")
        if qs:
            url = url + "?" + qs
        resp = req(url, params=req_kwargs.get("data"), headers=headers)
        setattr(resp, "content", resp.body)
        return resp

    # noinspection PyDeprecation
    with mock.patch("requests.request", side_effect=mocked_request), \
         mock.patch("requests.sessions.Session.request", side_effect=mocked_request):   # noqa
        request_func = getattr(app, function)
        return request_func(*args, **kwargs)


def mocked_execute_process():
    """
    Provides a mock to call :function:`weaver.wps_restapi.processes.processes.execute_process` safely within
    a test employing a :class:`webTest.TestApp` without a running ``Celery`` app.
    This avoids connection error from ``Celery`` during a job execution request.

    Bypasses the ``execute_process.delay`` call by directly invoking the ``execute_process``.

    **Note**: since ``delay`` and ``Celery`` are bypassed, the process execution becomes blocking (not asynchronous).
    """
    class MockTask(object):
        """
        Mocks call ``self.request.id`` in :function:`weaver.wps_restapi.processes.processes.execute_process` and
        call ``result.id`` in :function:`weaver.wps_restapi.processes.processes.submit_job_handler`.
        """
        _id = str(uuid.uuid4())

        @property
        def id(self):
            return self._id

    task = MockTask()

    def mock_execute_process(job_id, url, headers, notification_email):
        execute_process(job_id, url, headers, notification_email)
        return task

    return (
        mock.patch("weaver.wps_restapi.processes.processes.execute_process.delay", side_effect=mock_execute_process),
        mock.patch("celery.app.task.Context", return_value=task)
    )
