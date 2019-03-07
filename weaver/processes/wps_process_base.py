from weaver.wps import get_wps_output_path, get_wps_output_url
from pyramid_celery import celery_app as app
from pyramid.settings import asbool
from pyramid.httpexceptions import HTTPBadGateway
from time import sleep
from typing import TYPE_CHECKING
import requests
if TYPE_CHECKING:
    from weaver.typedefs import ExpectedOutputType
    from typing import Any, AnyStr, Dict, List, Union


class WpsProcessInterface(object):
    """
    Common interface for WpsProcess to be used is cwl jobs
    """

    def execute(self,
                workflow_inputs,        # type: Union[Dict[AnyStr, Any], List[Dict[AnyStr, Any]]]
                out_dir,                # type: AnyStr
                expected_outputs,       # type: List[ExpectedOutputType]
                ):
        """
        Execute a remote process using the given inputs.
        The function is expected to monitor the process and update the status.
        Retrieve the expected outputs and store them in the out_dir.

        :param workflow_inputs: cwl job dict
        :param out_dir: [string] directory where the outputs must be written
        :param expected_outputs: array of expected output ids
        """
        raise NotImplementedError

    def __init__(self, cookies):
        self.cookies = cookies
        self.headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}

        registry = app.conf['PYRAMID_REGISTRY']
        self.settings = registry.settings
        self.verify = asbool(self.settings.get('weaver.ows_proxy_ssl_verify', True))

    def make_request(self, method, url, retry, status_code_mock=None, **kwargs):
        response = requests.request(method,
                                    url=url,
                                    headers=self.headers,
                                    cookies=self.cookies,
                                    verify=self.verify,
                                    **kwargs)
        # TODO: Remove patch for Geomatys unreliable server
        if response.status_code == HTTPBadGateway.code and retry:
            sleep(10)
            response = self.make_request(method, url, False, **kwargs)
        if response.status_code == HTTPBadGateway.code and status_code_mock:
            response.status_code = status_code_mock
        return response

    @staticmethod
    def host_file(fn):
        registry = app.conf['PYRAMID_REGISTRY']
        weaver_output_url = get_wps_output_url(registry.settings)
        weaver_output_path = get_wps_output_path(registry.settings)
        fn = fn.replace('file://', '')

        if not fn.startswith(weaver_output_path):
            raise Exception('Cannot host files outside of the output path : {0}'.format(fn))
        return fn.replace(weaver_output_path, weaver_output_url)

    @staticmethod
    def map_progress(progress, range_min, range_max):
        return range_min + (progress * (range_max - range_min)) / 100