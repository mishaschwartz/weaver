import contextlib
import os
import tempfile

import pytest
from owslib.wps import ComplexDataInput, WPSExecution

from tests.functional.utils import WpsConfigBase
from tests.utils import mocked_execute_celery, mocked_sub_requests, mocked_wps_output
from weaver import WEAVER_ROOT_DIR, xml_util
from weaver.execute import ExecuteMode, ExecuteResponse, ExecuteTransmissionMode
from weaver.formats import ContentType
from weaver.processes.wps_package import CWL_REQUIREMENT_APP_DOCKER
from weaver.utils import fetch_file, get_any_value, load_file, str2bytes
from weaver.wps.utils import get_wps_url
from weaver.wps_restapi.utils import get_wps_restapi_base_url


@pytest.mark.functional
class WpsPackageDockerAppTest(WpsConfigBase):
    @classmethod
    def setUpClass(cls):
        cls.settings = {
            "weaver.url": "https://localhost",
            "weaver.wps": True,
            "weaver.wps_output": True,
            "weaver.wps_output_url": "http://hosted-output.com/wpsoutputs",
            "weaver.wps_output_dir": "/tmp",  # nosec: B108 # don't care hardcoded for test
            "weaver.wps_path": "/ows/wps",
            "weaver.wps_restapi_path": "/",
        }
        super(WpsPackageDockerAppTest, cls).setUpClass()
        cls.out_key = "output"
        # use default file generated by Weaver/CWL
        # command 'cat' within docker application will dump file contents to standard output captured by it
        cls.out_file = "stdout.log"
        cls.process_id = cls.__name__
        cls.deploy_docker_process()

    @classmethod
    def get_package(cls):
        return {
            "cwlVersion": "v1.0",
            "class": "CommandLineTool",
            "baseCommand": "cat",
            "requirements": {
                CWL_REQUIREMENT_APP_DOCKER: {
                    "dockerPull": "debian:stretch-slim"
                }
            },
            "inputs": [
                {"id": "file", "type": "File", "inputBinding": {"position": 1}},
            ],
            "outputs": [
                {"id": cls.out_key, "type": "File", "outputBinding": {"glob": cls.out_file}},
            ]
        }

    @classmethod
    def get_deploy_body(cls):
        cwl = cls.get_package()
        body = {
            "processDescription": {
                "process": {"id": cls.process_id}
            },
            "deploymentProfileName": "http://www.opengis.net/profiles/eoc/dockerizedApplication",
            "executionUnit": [{"unit": cwl}],
        }
        return body

    @classmethod
    def deploy_docker_process(cls):
        body = cls.get_deploy_body()
        info = cls.deploy_process(body)
        return info

    def validate_outputs(self, job_id, result_payload, outputs_payload, result_file_content):
        # get generic details
        wps_uuid = str(self.job_store.fetch_by_id(job_id).wps_id)
        wps_out_url = self.settings["weaver.wps_output_url"]
        wps_output = f"{wps_out_url}/{wps_uuid}/{self.out_key}/{self.out_file}"

        # --- validate /results path format ---
        assert len(result_payload) == 1
        assert isinstance(result_payload, dict)
        assert isinstance(result_payload[self.out_key], dict)
        result_values = {out_id: get_any_value(result_payload[out_id]) for out_id in result_payload}
        assert result_values[self.out_key] == wps_output

        # --- validate /outputs path format ---

        # check that output is HTTP reference to file
        output_values = {out["id"]: get_any_value(out) for out in outputs_payload["outputs"]}
        assert len(output_values) == 1
        assert output_values[self.out_key] == wps_output

        # check that actual output file was created in expected location along with XML job status
        wps_outdir = self.settings["weaver.wps_output_dir"]
        wps_out_file = os.path.join(wps_outdir, job_id, self.out_key, self.out_file)
        assert not os.path.exists(os.path.join(wps_outdir, self.out_file)), (
            "File is expected to be created in sub-directory of Job ID, not directly in WPS output directory."
        )
        # job log, XML status and output directory can be retrieved with both Job UUID and underlying WPS UUID reference
        assert os.path.isfile(os.path.join(wps_outdir, f"{wps_uuid}.log"))
        assert os.path.isfile(os.path.join(wps_outdir, f"{wps_uuid}.xml"))
        assert os.path.isfile(os.path.join(wps_outdir, wps_uuid, self.out_key, self.out_file))
        assert os.path.isfile(os.path.join(wps_outdir, f"{job_id}.log"))
        assert os.path.isfile(os.path.join(wps_outdir, f"{job_id}.xml"))
        assert os.path.isfile(wps_out_file)

        # validate content
        with open(wps_out_file, mode="r", encoding="utf-8") as res_file:
            assert res_file.read() == result_file_content

    def test_deployed_process_schemas(self):
        """
        Validate that resulting schemas from deserialization correspond to original package and process definitions.
        """
        # process already deployed by setUpClass
        body = self.get_deploy_body()
        process = self.process_store.fetch_by_id(self.process_id)
        assert process.package == body["executionUnit"][0]["unit"]
        assert process.payload == body

    def test_execute_wps_rest_resp_json(self):
        """
        Test validates that basic Docker application runs successfully, fetching the reference as needed.

        The job execution is launched using the WPS-REST endpoint for this test.
        Both the request body and response content are JSON.

        .. seealso::
            - :meth:`test_execute_wps_kvp_get_resp_xml`
            - :meth:`test_execute_wps_kvp_get_resp_json`
            - :meth:`test_execute_wps_xml_post_resp_xml`
            - :meth:`test_execute_wps_xml_post_resp_json`
        """

        test_content = "Test file in Docker - WPS-REST job endpoint"
        with contextlib.ExitStack() as stack_exec:
            # setup
            dir_name = tempfile.gettempdir()
            tmp_file = stack_exec.enter_context(tempfile.NamedTemporaryFile(dir=dir_name, mode="w", suffix=".txt"))
            tmp_file.write(test_content)
            tmp_file.seek(0)
            exec_body = {
                "mode": ExecuteMode.ASYNC,
                "response": ExecuteResponse.DOCUMENT,
                "inputs": [
                    {"id": "file", "href": tmp_file.name},
                ],
                "outputs": [
                    {"id": self.out_key, "transmissionMode": ExecuteTransmissionMode.VALUE},
                ]
            }
            for mock_exec in mocked_execute_celery():
                stack_exec.enter_context(mock_exec)

            # execute
            proc_url = f"/processes/{self.process_id}/jobs"
            resp = mocked_sub_requests(self.app, "post_json", proc_url,
                                       data=exec_body, headers=self.json_headers, only_local=True)
            assert resp.status_code in [200, 201], f"Failed with: [{resp.status_code}]\nReason:\n{resp.json}"
            status_url = resp.json["location"]
            job_id = resp.json["jobID"]

            # job monitoring
            results = self.monitor_job(status_url)
            outputs = self.get_outputs(status_url)

        self.validate_outputs(job_id, results, outputs, test_content)

    def wps_execute(self, version, accept, url=None):
        if url:
            wps_url = url
        else:
            wps_url = get_wps_url(self.settings)
        if version == "1.0.0":
            test_content = "Test file in Docker - WPS KVP"
            wps_method = "GET"
        elif version == "2.0.0":
            test_content = "Test file in Docker - WPS XML"
            wps_method = "POST"
        else:
            raise ValueError(f"Invalid WPS version: {version}")
        accept_type = accept.split("/")[-1].upper()
        test_content += f" {wps_method} request - Accept {accept_type}"

        with contextlib.ExitStack() as stack_exec:
            # setup
            dir_name = tempfile.gettempdir()
            tmp_file = stack_exec.enter_context(tempfile.NamedTemporaryFile(dir=dir_name, mode="w", suffix=".txt"))
            tmp_file.write(test_content)
            tmp_file.seek(0)
            for mock_exec in mocked_execute_celery():
                stack_exec.enter_context(mock_exec)

            # execute
            if version == "1.0.0":
                wps_inputs = [f"file={tmp_file.name}@mimeType={ContentType.TEXT_PLAIN}"]
                wps_params = {
                    "service": "WPS",
                    "request": "Execute",
                    "version": version,
                    "identifier": self.process_id,
                    "DataInputs": wps_inputs,
                }
                wps_headers = {"Accept": accept}
                wps_data = None
            else:
                wps_inputs = [("file", ComplexDataInput(tmp_file.name, mimeType=ContentType.TEXT_PLAIN))]
                wps_outputs = [(self.out_key, True)]  # as reference
                wps_exec = WPSExecution(version=version, url=wps_url)
                wps_req = wps_exec.buildRequest(self.process_id, wps_inputs, wps_outputs)
                wps_data = xml_util.tostring(wps_req)
                wps_headers = {"Accept": accept, "Content-Type": ContentType.APP_XML}
                wps_params = None
            resp = mocked_sub_requests(self.app, wps_method, wps_url,
                                       params=wps_params, data=wps_data, headers=wps_headers, only_local=True)
            assert resp.status_code in [200, 201], (
                f"Failed with: [{resp.status_code}]\nTest: [{test_content}]\nReason:\n{resp.text}"
            )

            # parse response status
            if accept == ContentType.APP_XML:
                assert resp.content_type in ContentType.ANY_XML, test_content
                xml_body = xml_util.fromstring(str2bytes(resp.text))
                status_url = xml_body.get("statusLocation")
                job_id = status_url.split("/")[-1].split(".")[0]
            elif accept == ContentType.APP_JSON:
                assert resp.content_type == ContentType.APP_JSON, test_content
                status_url = resp.json["location"]
                job_id = resp.json["jobID"]
            assert status_url
            assert job_id

            if accept == ContentType.APP_XML:
                wps_out_url = self.settings["weaver.wps_output_url"]
                weaver_url = self.settings["weaver.url"]
                assert status_url == f"{wps_out_url}/{job_id}.xml", "Status URL should be XML file for WPS-1 request"
                # remap to employ JSON monitor method (could be done with XML parsing otherwise)
                status_url = f"{weaver_url}/jobs/{job_id}"

            # job monitoring
            results = self.monitor_job(status_url)
            outputs = self.get_outputs(status_url)

            # validate XML status is updated accordingly
            wps_xml_status = os.path.join(self.settings["weaver.wps_output_dir"], job_id + ".xml")
            assert os.path.isfile(wps_xml_status)
            with open(wps_xml_status, mode="r", encoding="utf-8") as status_file:
                assert "ProcessSucceeded" in status_file.read()

        self.validate_outputs(job_id, results, outputs, test_content)

    def test_execute_wps_kvp_get_resp_xml(self):
        """
        Test validates that basic Docker application runs successfully, fetching the reference as needed.

        The job is launched using the WPS Execute request with Key-Value Pairs (KVP) and GET method.
        The request is done with query parameters, and replies by default with response XML content.

        .. seealso::
            - :meth:`test_execute_wps_rest_resp_json`
            - :meth:`test_execute_wps_kvp_get_resp_json`
            - :meth:`test_execute_wps_xml_post_resp_xml`
            - :meth:`test_execute_wps_xml_post_resp_json`
        """
        self.wps_execute("1.0.0", ContentType.APP_XML)

    def test_execute_wps_kvp_get_resp_json(self):
        """
        Test validates that basic Docker application runs successfully, fetching the reference as needed.

        Does the same operation as :meth:`test_execute_wps_kvp_get_resp_xml`, but use ``Accept`` header of JSON
        which should return a response with the same contents as if called directly via WPS-REST endpoint.

        .. seealso::
            - :meth:`test_execute_wps_rest_resp_json`
            - :meth:`test_execute_wps_kvp_get_resp_xml`
            - :meth:`test_execute_wps_xml_post_resp_xml`
            - :meth:`test_execute_wps_xml_post_resp_json`
        """
        self.wps_execute("1.0.0", ContentType.APP_JSON)

    def test_execute_wps_xml_post_resp_xml(self):
        """
        Test validates that basic Docker application runs successfully, fetching the reference as needed.

        The job is launched using the WPS Execute request with POST request method and XML content.

        .. seealso::
            - :meth:`test_execute_wps_rest_resp_json`
            - :meth:`test_execute_wps_kvp_get_resp_xml`
            - :meth:`test_execute_wps_kvp_get_resp_json`
            - :meth:`test_execute_wps_xml_post_resp_json`
        """
        self.wps_execute("2.0.0", ContentType.APP_XML)

    def test_execute_wps_xml_post_resp_json(self):
        """
        Test validates that basic Docker application runs successfully, fetching the reference as needed.

        Does the same operation as :meth:`test_execute_wps_xml_post_resp_xml`, but use ``Accept`` header of JSON
        which should return a response with the same contents as if called directly via WPS-REST endpoint.

        .. seealso::
            - :meth:`test_execute_wps_rest_resp_json`
            - :meth:`test_execute_wps_kvp_get_resp_xml`
            - :meth:`test_execute_wps_kvp_get_resp_json`
            - :meth:`test_execute_wps_xml_post_resp_json`
        """
        self.wps_execute("2.0.0", ContentType.APP_JSON)

    def test_execute_rest_xml_post_resp_json(self):
        """
        Test :term:`XML` content using :term:`WPS` format submitted to REST endpoint gets redirected automatically.
        """
        base = get_wps_restapi_base_url(self.settings)
        url = f"{base}/processes/{self.process_id}/execution"
        self.wps_execute("2.0.0", ContentType.APP_JSON, url=url)

    def test_execute_docker_embedded_python_script(self):
        test_proc = "test-docker-python-script"
        cwl = load_file(os.path.join(WEAVER_ROOT_DIR, "docs/examples/docker-python-script-report.cwl"))
        body = {
            "processDescription": {
                "process": {
                    "id": test_proc
                }
            },
            "executionUnit": [{"unit": cwl}],
            "deploymentProfileName": "http://www.opengis.net/profiles/eoc/dockerizedApplication"
        }
        self.deploy_process(body)

        with contextlib.ExitStack() as stack:
            for mock_exec in mocked_execute_celery():
                stack.enter_context(mock_exec)

            path = f"/processes/{test_proc}/execution"
            cost = 2.45
            amount = 3
            body = {
                "mode": ExecuteMode.ASYNC,
                "response": ExecuteResponse.DOCUMENT,
                "inputs": [
                    {"id": "amount", "value": amount},
                    {"id": "cost", "value": cost}
                ],
                "outputs": [
                    {"id": "quote", "transmissionMode": ExecuteTransmissionMode.VALUE},
                ]
            }
            resp = mocked_sub_requests(self.app, "POST", path, json=body, headers=self.json_headers, only_local=True)
            status_url = resp.headers["Location"]
            results = self.monitor_job(status_url)

            assert results["quote"]["href"].startswith("http")
            stack.enter_context(mocked_wps_output(self.settings))
            tmpdir = stack.enter_context(tempfile.TemporaryDirectory())
            report_file = fetch_file(results["quote"]["href"], tmpdir, settings=self.settings)
            report_data = load_file(report_file, text=True)
            assert report_data == f"Order Total: {amount * cost:0.2f}$\n"
