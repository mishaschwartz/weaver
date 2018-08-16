from pyramid.httpexceptions import *
from pyramid_celery import celery_app as app
from twitcher.adapter import jobstore_factory
from twitcher.exceptions import JobNotFound
from twitcher.wps_restapi import swagger_definitions as sd
from twitcher.wps_restapi.utils import wps_restapi_base_url
from twitcher.wps_restapi.status import *
from twitcher.wps_restapi.sort import *
from owslib.wps import WPSExecution
from lxml import etree
from celery.utils.log import get_task_logger
import requests

logger = get_task_logger(__name__)


def job_url(request, job):
    return '{base_url}/providers/{provider_id}/processes/{process_id}/jobs/{job_id}'.format(
        base_url=wps_restapi_base_url(request.registry.settings),
        provider_id=job['provider_id'],
        process_id=job['process_id'],
        job_id=job['task_id'])


def check_status(url=None, response=None, sleep_secs=2, verify=False):
    """
    Run owslib.wps check_status with additional exception handling.

    :param verify: Flag to enable SSL verification. Default: False
    :return: OWSLib.wps.WPSExecution object.
    """
    execution = WPSExecution()
    if response:
        logger.debug("using response document ...")
        xml = response
    elif url:
        logger.debug('using status_location url ...')
        xml = requests.get(url, verify=verify).content
    else:
        raise Exception("you need to provide a status-location url or response object.")
    if type(xml) is unicode:
        xml = xml.encode('utf8', errors='ignore')
    execution.checkStatus(response=xml, sleepSecs=sleep_secs)
    if execution.response is None:
        raise Exception("check_status failed!")
    # TODO: workaround for owslib type change of response
    if not isinstance(execution.response, etree._Element):
        execution.response = etree.fromstring(execution.response)
    return execution


def get_job(request):
    """
    :returns: Job information if found.
    :raises: HTTPNotFound with JSON body details on missing/non-matching job, process, provider IDs.
    """
    job_id = request.matchdict.get('job_id')
    store = jobstore_factory(request.registry)
    try:
        job = store.fetch_by_id(job_id)
    except JobNotFound:
        raise HTTPNotFound('Could not find job with specified `job_id`')

    provider_id = request.matchdict.get('provider_id', job['provider_id'])
    process_id = request.matchdict.get('process_id', job['process_id'])

    if job['provider_id'] != provider_id:
        raise HTTPNotFound('Could not find job with specified `provider_id`.')
    if job['process_id'] != process_id:
        raise HTTPNotFound('Could not find job with specified `process_id`.')
    return job


@sd.jobs_full_service.get(tags=[sd.jobs_tag, sd.providers_tag], renderer='json',
                          schema=sd.GetJobsRequest(), response_schemas=sd.get_all_jobs_responses)
@sd.jobs_short_service.get(tags=[sd.jobs_tag], renderer='json',
                           schema=sd.GetJobsRequest(), response_schemas=sd.get_all_jobs_responses)
def get_jobs(request):
    """
    Retrieve the list of jobs which can be filtered/sorted using queries.
    """

    page = int(request.params.get('page', '0'))
    limit = int(request.params.get('limit', '10'))
    filters = {
        'page': page,
        'limit': limit,
        'tag': request.params.get('tag', None),
        'access': request.params.get('access', None),
        'status': request.params.get('status', None),
        'sort': request.params.get('sort', SORT_CREATED),
        # provider and process can be specified by query (short route) or by path (full route)
        'process': request.params.get('process', None) or request.matchdict.get('process_id', None),
        'provider': request.params.get('provider', None) or request.matchdict.get('provider_id', None),
    }
    store = jobstore_factory(request.registry)
    items, count = store.find_jobs(request, **filters)
    return HTTPOk(json={
        'count': count,
        'page': page,
        'limit': limit,
        'jobs': [item['task_id'] for item in items]
    })


@sd.job_full_service.get(tags=[sd.jobs_tag, sd.status_tag, sd.providers_tag], renderer='json',
                         schema=sd.FullJobEndpoint(), response_schemas=sd.get_single_job_status_responses)
@sd.job_short_service.get(tags=[sd.jobs_tag, sd.status_tag], renderer='json',
                          schema=sd.ShortJobEndpoint(), response_schemas=sd.get_single_job_status_responses)
def get_job_status(request):
    """
    Retrieve the status of a job.
    """
    job = get_job(request)
    response = {
        "status": job['status'],
        "message": "Job {}.".format(job['status']),
        "progress": job.get('progress', 0)
    }

    if job['status'] in status_categories[STATUS_FINISHED]:
        if job['status'] == STATUS_SUCCEEDED:
            resource = 'outputs'
        else:
            resource = 'exceptions'

        response[resource] = '{job_url}/{resource}'.format(job_url=job_url(request, job), resource=resource.lower())
        response['logs'] = '{job_url}/logs'.format(job_url=job_url(request, job))

    return HTTPOk(json=response)


@sd.job_full_service.delete(tags=[sd.jobs_tag, sd.dismiss_tag, sd.providers_tag], renderer='json',
                            schema=sd.FullJobEndpoint(), response_schemas=sd.delete_job_responses)
@sd.job_short_service.delete(tags=[sd.jobs_tag, sd.dismiss_tag], renderer='json',
                             schema=sd.ShortJobEndpoint(), response_schemas=sd.delete_job_responses)
def cancel_job(request):
    """
    Dismiss a job.
    Note: Will only stop tracking this particular process (WPS 1.0 doesn't allow to stop a process)
    """
    job = get_job(request)
    app.control.revoke(job['task_id'], terminate=True)

    return HTTPOk(json={
        'status': job.get('status', 'unknown'),
        'message': 'Job dismissed.',
        'progress': job.get('progress', 0),
    })


@sd.results_full_service.get(tags=[sd.jobs_tag, sd.results_tag, sd.providers_tag], renderer='json',
                             schema=sd.FullResultsEndpoint(), response_schemas=sd.get_job_results_responses)
@sd.results_short_service.get(tags=[sd.jobs_tag, sd.results_tag], renderer='json',
                              schema=sd.ShortResultsEndpoint(), response_schemas=sd.get_job_results_responses)
def get_job_results(request):
    """
    Retrieve the results of a job.
    """
    job = get_job(request)
    outputs = job['outputs']
    for output in outputs:
        output['url'] = '{job_url}/outputs/{result_id}'.format(job_url=job_url(request, job),
                                                               result_id=output['identifier'])
    return HTTPOk(json=outputs)


@sd.result_full_service.get(tags=[sd.jobs_tag, sd.results_tag, sd.providers_tag], renderer='json',
                            schema=sd.FullResultEndpoint(), response_schemas=sd.get_single_result_responses)
@sd.result_short_service.get(tags=[sd.jobs_tag, sd.results_tag], renderer='json',
                             schema=sd.ShortResultEndpoint(), response_schemas=sd.get_single_result_responses)
def get_job_result(request):
    """
    Retrieve a specific result of a particular job output.
    """
    job = get_job(request)
    result_id = request.matchdict.get('result_id')

    for output in job['outputs']:
        if output['identifier'] == result_id:
            output['url'] = '{job_url}/outputs/{result_id}'.format(job_url=job_url(request, job),
                                                                   result_id=output['identifier'])
            return HTTPOk(json=output)
    raise HTTPNotFound('Could not find job output.')


@sd.exceptions_full_service.get(tags=[sd.jobs_tag, sd.exceptions_tag, sd.providers_tag], renderer='json',
                                schema=sd.FullExceptionsEndpoint(), response_schemas=sd.get_exceptions_responses)
@sd.exceptions_short_service.get(tags=[sd.jobs_tag, sd.exceptions_tag], renderer='json',
                                 schema=sd.ShortExceptionsEndpoint(), response_schemas=sd.get_exceptions_responses)
def get_job_exceptions(request):
    """
    Retrieve the exceptions of a job.
    """
    job = get_job(request)
    return HTTPOk(json=job['exceptions'])


@sd.logs_full_service.get(tags=[sd.jobs_tag, sd.logs_tag, sd.providers_tag], renderer='json',
                          schema=sd.FullLogsEndpoint(), response_schemas=sd.get_logs_responses)
@sd.logs_short_service.get(tags=[sd.jobs_tag, sd.logs_tag], renderer='json',
                           schema=sd.ShortLogsEndpoint(), response_schemas=sd.get_logs_responses)
def get_job_logs(request):
    """
    Retrieve the logs of a job.
    """
    job = get_job(request)
    return HTTPOk(json=job['logs'])
