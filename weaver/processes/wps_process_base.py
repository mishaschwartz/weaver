import glob
import logging
import os
from abc import abstractmethod
from time import sleep
from typing import TYPE_CHECKING

from pyramid.httpexceptions import HTTPBadGateway

from weaver.exceptions import PackageExecutionError
from weaver.formats import CONTENT_TYPE_APP_JSON
from weaver.processes.constants import OPENSEARCH_LOCAL_FILE_SCHEME
from weaver.status import STATUS_RUNNING, STATUS_SUCCEEDED
from weaver.utils import fetch_file, get_any_id, get_any_value, get_cookie_headers, get_settings, request_extra
from weaver.wps.utils import get_wps_output_dir, get_wps_output_url, map_wps_output_location

if TYPE_CHECKING:
    from typing import Any, Union

    from pywps.app import WPSRequest

    from weaver.typedefs import (
        CWL_RuntimeInputsMap,
        CWL_ExpectedOutputs,
        CWL_WorkflowInputs,
        JobInputs,
        JobOutputs,
        JobResults,
        JobMonitorReference,
        UpdateStatusPartialFunction
    )

LOGGER = logging.getLogger(__name__)

# NOTE:
#   Implementations can reuse the same progress values or intermediate ones within the range of the relevant sections.
REMOTE_JOB_PROGRESS_START = 1
REMOTE_JOB_PROGRESS_PREPARE = 2
REMOTE_JOB_PROGRESS_READY = 5
REMOTE_JOB_PROGRESS_STAGE_IN = 10
REMOTE_JOB_PROGRESS_FORMAT_IO = 12
REMOTE_JOB_PROGRESS_EXECUTE = 15
REMOTE_JOB_PROGRESS_MONITOR = 20
REMOTE_JOB_PROGRESS_RESULTS = 85
REMOTE_JOB_PROGRESS_STAGE_OUT = 90
REMOTE_JOB_PROGRESS_CLEANUP = 95
REMOTE_JOB_PROGRESS_COMPLETED = 100


class WpsProcessInterface(object):
    """
    Common interface for :term:`WPS` :term:`Process` to be used for dispatching :term:`CWL` jobs.

    Multiple convenience methods are provide.
    Processes inheriting from this base should provide abstract method implementation as needed or required.

    The operations are accomplished in the following order:

    .. list-table::
        * - Step Method
          - Requirements
          - Description
        * - :meth:`prepare`
          - I*
          - Setup any prerequisites for the :term:`Process` or :term:`Job`.
        * - :meth:`stage_inputs`
          - R
          - Retrieve input locations (considering remote files and workflow previous-step staging).
        * - :meth:`format_inputs`
          - I*
          - Perform operations on staged inputs to obtain desired format.
        * - :meth:`format_outputs`
          - I*
          - Perform operations on expected outputs to obtain desired format.
        * - :meth:`dispatch`
          - R,I
          - Perform requests for remote execution of the :term:`Process`.
        * - :meth:`monitor`
          - R,I
          - Perform monitoring of the :term:`Job` status until completion.
        * - :meth:`get_results`
          - R,I
          - Perform operations to obtain results location in the expected format.
        * - :meth:`stage_results`
          - R
          - Retrieve results from remote :term:`Job` for local storage using output locations.
        * - :meth:`cleanup`
          - I*
          - Perform any final steps before completing the execution or after failed execution.

    .. note::
        - Steps marked by ``*`` are optional.
        - Steps marked by ``R`` are required.
        - Steps marked by ``I`` are implementation dependant.

    .. seealso::
        :meth:`execute` for complete details of the operations and ordering.
    """

    def __init__(self, request, update_status):
        # type: (WPSRequest, UpdateStatusPartialFunction) -> None
        self.request = request
        if self.request.http_request:
            self.cookies = get_cookie_headers(self.request.http_request.headers)
        else:
            self.cookies = {}
        self.headers = {"Accept": CONTENT_TYPE_APP_JSON, "Content-Type": CONTENT_TYPE_APP_JSON}
        self.settings = get_settings()
        self.update_status = update_status  # type: UpdateStatusPartialFunction

    def execute(self, workflow_inputs, out_dir, expected_outputs):
        # type: (CWL_RuntimeInputsMap, str, CWL_ExpectedOutputs) -> None
        """
        Execute the core operation of the remote :term:`Process` using the given inputs.

        The function is expected to monitor the process and update the status.
        Retrieve the expected outputs and store them in the ``out_dir``.

        :param workflow_inputs: `CWL` job dict
        :param out_dir: directory where the outputs must be written
        :param expected_outputs: expected value outputs as `{'id': 'value'}`
        """
        self.update_status("Preparing process for remote execution.",
                           REMOTE_JOB_PROGRESS_PREPARE, STATUS_RUNNING)
        self.prepare()
        self.update_status("Process ready for execute remote process.",
                           REMOTE_JOB_PROGRESS_READY, STATUS_RUNNING)

        self.update_status("Staging inputs for remote execution.",
                           REMOTE_JOB_PROGRESS_STAGE_IN, STATUS_RUNNING)
        staged_inputs = self.stage_inputs(workflow_inputs)

        self.update_status("Preparing inputs/outputs for remote execution.",
                           REMOTE_JOB_PROGRESS_FORMAT_IO, STATUS_RUNNING)
        expect_outputs = [{"id": output} for output in expected_outputs]
        process_inputs = self.format_inputs(staged_inputs)
        process_outputs = self.format_outputs(expect_outputs)

        try:
            self.update_status("Executing remote process job.",
                               REMOTE_JOB_PROGRESS_EXECUTE, STATUS_RUNNING)
            monitor_ref = self.dispatch(process_inputs, process_outputs)
            self.update_status("Monitoring remote process job until completion.",
                               REMOTE_JOB_PROGRESS_MONITOR, STATUS_RUNNING)
            job_success = self.monitor(monitor_ref)
            if not job_success:
                raise PackageExecutionError("Failed dispatch and monitoring of remote process execution.")
        except Exception as exc:
            raise PackageExecutionError("Dispatch and monitoring of remote process caused an unhandled error.") from exc
        finally:
            self.update_status("Running final cleanup operations following failed execution.",
                               REMOTE_JOB_PROGRESS_CLEANUP, STATUS_RUNNING)

        self.update_status("Retrieving job results definitions.",
                           REMOTE_JOB_PROGRESS_RESULTS, STATUS_RUNNING)
        results = self.get_results(monitor_ref)
        self.update_status("Staging job outputs from remote process.",
                           REMOTE_JOB_PROGRESS_STAGE_OUT, STATUS_RUNNING)
        self.stage_results(results, expected_outputs, out_dir)

        self.update_status("Running final cleanup operations before completion.",
                           REMOTE_JOB_PROGRESS_CLEANUP, STATUS_RUNNING)
        self.update_status("Execution of remote process execution completed successfully.",
                           REMOTE_JOB_PROGRESS_COMPLETED, STATUS_SUCCEEDED)

    def prepare(self):
        """
        Implementation dependent operations to prepare the :term:`Process` for :term:`Job` execution.

        This is an optional step that can be omitted entirely if not needed.
        """

    def format_inputs(self, workflow_inputs):
        # type: (JobInputs) -> Union[JobInputs, Any]
        """
        Implementation dependent operations to configure input values for :term:`Job` execution.

        This is an optional step that will simply pass down the inputs as is if no formatting is required.
        Otherwise, the implementing :term:`Process` can override the step to reorganize workflow step inputs into the
        necessary format required for their :meth:`dispatch` call.
        """
        return workflow_inputs

    def format_outputs(self, workflow_outputs):
        # type: (JobOutputs) -> JobOutputs
        """
        Implementation dependent operations to configure expected outputs for :term:`Job` execution.

        This is an optional step that will simply pass down the outputs as is if no formatting is required.
        Otherwise, the implementing :term:`Process` can override the step to reorganize workflow step outputs into the
        necessary format required for their :meth:`dispatch` call.
        """
        return workflow_outputs

    @abstractmethod
    def dispatch(self, process_inputs, process_outputs):
        # type: (JobInputs, JobOutputs) -> JobMonitorReference
        """
        Implementation dependent operations to dispatch the :term:`Job` execution to the remote :term:`Process`.

        :returns: reference details that will be passed to :meth:`monitor`.
        """
        raise NotImplementedError

    @abstractmethod
    def monitor(self, monitor_reference):
        # type: (JobMonitorReference) -> bool
        """
        Implementation dependent operations to monitor the status of the :term:`Job` execution that was dispatched.

        This step should block :meth:`execute` until the final status of the remote :term:`Job` (failed/success)
        can be obtained.

        :returns: success status
        """
        raise NotImplementedError

    @abstractmethod
    def get_results(self, monitor_reference):
        # type: (JobMonitorReference) -> JobResults
        """
        Implementation dependent operations to retrieve the results following a successful :term:`Job` execution.

        The operation should **NOT** fetch (stage) results, but only obtain the locations where they can be retrieved,
        based on the monitoring reference that was generated from the execution.

        :returns: results locations
        """
        raise NotImplementedError

    def cleanup(self):
        """
        Implementation dependent operations to clean the :term:`Process` or :term:`Job` execution.

        This is an optional step that can be omitted entirely if not needed.
        """

    def make_request(self, method, url, retry, status_code_mock=None, **kwargs):
        response = request_extra(method, url=url, settings=self.settings,
                                 headers=self.headers, cookies=self.cookies, **kwargs)
        # TODO: Remove patch for Geomatys unreliable server
        if response.status_code == HTTPBadGateway.code and retry:
            sleep(10)
            response = self.make_request(method, url, False, **kwargs)
        if response.status_code == HTTPBadGateway.code and status_code_mock:
            response.status_code = status_code_mock
        return response

    def host_file(self, file_name):
        weaver_output_url = get_wps_output_url(self.settings)
        weaver_output_dir = get_wps_output_dir(self.settings)
        file_name = os.path.realpath(file_name.replace("file://", ""))  # in case CWL->WPS outputs link was made

        if not file_name.startswith(weaver_output_dir):
            raise Exception("Cannot host files outside of the output path : {0}".format(file_name))
        return file_name.replace(weaver_output_dir, weaver_output_url)

    def stage_results(self, results, expected_outputs, out_dir):
        # type: (JobResults, CWL_ExpectedOutputs, str) -> None
        """
        Retrieves the remote execution :term:`Job` results for staging locally into the specified output directory.

        This operation should be called by the implementing remote :term:`Process` definition after :meth:`execute`.

        .. note::
            The :term:`CWL` runner expects the output file(s) to be written matching definition in ``expected_outputs``,
            but this definition could be a glob pattern to match multiple file and/or nested directories.
            We cannot rely on specific file names to be mapped, since glob can match many (eg: ``"*.txt"``).
        """
        for result in results:
            res_id = get_any_id(result)
            if res_id not in expected_outputs:
                continue

            # plan ahead when list of multiple output values could be supported
            result_values = get_any_value(result)
            if not isinstance(result_values, list):
                result_values = [result_values]
            cwl_out_dir = out_dir.rstrip("/")
            for value in result_values:
                src_name = value.split("/")[-1]
                dst_path = "/".join([cwl_out_dir, src_name])
                # performance improvement:
                #   Bypass download if file can be resolved as local resource (already fetched or same server).
                #   Because CWL expects the file to be in specified 'out_dir', make a link for it to be found
                #   even though the file is stored in the full job output location instead (already staged by step).
                map_path = map_wps_output_location(value, self.settings)
                as_link = False
                if map_path:
                    LOGGER.info("Detected result [%s] from [%s] as local reference to this instance. "
                                "Skipping fetch and using local copy in output destination: [%s]",
                                res_id, value, dst_path)
                    LOGGER.debug("Mapped result [%s] to local reference: [%s]", value, map_path)
                    src_path = map_path
                    as_link = True
                else:
                    LOGGER.info("Fetching result [%s] from [%s] to CWL output destination: [%s]",
                                res_id, value, dst_path)
                    src_path = value
                fetch_file(src_path, cwl_out_dir, settings=self.settings, link=as_link)

            # patch expected output sub-dir by following workflow steps if employed in outputBindings
            # https://www.commonwl.org/v1.0/CommandLineTool.html#CommandOutputBinding
            # res_loc = expected_outputs[res_id]
            # if not isinstance(res_loc, list):
            #     res_loc = [res_loc]
            # for res_path in res_loc:
            #     if res_path.startswith("./"):
            #         res_path = res_path.split("/", 1)[-1]
            #     if not res_path:
            #         continue
            #     if any(res_path.startswith(part) for part in ["..", "/", "~"]):
            #         LOGGER.debug("Skipping forbidden path for outputBinding: [%s]", res_path)
            #         continue
            #     res_parts = res_path.split("/")
            #     res_base = cwl_out_dir
            #     res_name = res_parts[-1]
            #     for part in res_parts[:-1]:
            #         res_base = os.path.join(res_base, part)
            #         os.makedirs(res_base, exist_ok=True)
            #     if res_base != cwl_out_dir:
            #         for file_match in glob.glob(cwl_out_dir):
            #             file_stage = os.path.join(cwl_out_dir, os.path.split(file_match)[-1])
            #             if not os.path.exists(file_match) and os.path.isfile(file_stage):
            #                 LOGGER.debug("Resolved outputBinding [%s]->[%s]", file_stage, file_match)
            #                 os.symlink(file_stage, file_match, target_is_directory=False)

    def stage_inputs(self, workflow_inputs):
        # type: (CWL_WorkflowInputs) -> JobInputs
        """
        Retrieves inputs for local staging if required for the following :term:`Job` execution.
        """
        execute_body_inputs = []
        for workflow_input_key, workflow_input_value in workflow_inputs.items():
            if not isinstance(workflow_input_value, list):
                workflow_input_value = [workflow_input_value]
            for workflow_input_value_item in workflow_input_value:
                if isinstance(workflow_input_value_item, dict) and "location" in workflow_input_value_item:
                    location = workflow_input_value_item["location"]
                    execute_body_inputs.append({"id": workflow_input_key, "href": location})
                else:
                    execute_body_inputs.append({"id": workflow_input_key, "data": workflow_input_value_item})

        for exec_input in execute_body_inputs:
            if "href" in exec_input and isinstance(exec_input["href"], str):
                LOGGER.debug("Original input location [%s] : [%s]", exec_input["id"], exec_input["href"])
                if exec_input["href"].startswith("{0}://".format(OPENSEARCH_LOCAL_FILE_SCHEME)):
                    exec_input["href"] = "file{0}".format(exec_input["href"][len(OPENSEARCH_LOCAL_FILE_SCHEME):])
                    LOGGER.debug("OpenSearch intermediate input [%s] : [%s]", exec_input["id"], exec_input["href"])
                elif exec_input["href"].startswith("file://"):
                    exec_input["href"] = self.host_file(exec_input["href"])
                    LOGGER.debug("Hosting intermediate input [%s] : [%s]", exec_input["id"], exec_input["href"])
        return execute_body_inputs
