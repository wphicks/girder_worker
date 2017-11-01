import unittest
import mock
import requests

from girder_worker.app import (
    girder_before_task_publish,
    gw_task_prerun,
    gw_task_success,
    gw_task_failure,
    gw_task_postrun,
    gw_task_revoked)

from contextlib import contextmanager
from girder_worker.utils import JobStatus


@contextmanager
def mock_worker_plugin_utils():
    """girder.plugins is not available unless you are
    working within the context of a rest request. This
    context manager allows us to mock girder.plugins.worker.utils
    without seting up a whole server.
    """

    girder_plugins = mock.MagicMock()
    with mock.patch.dict('sys.modules',
                         **{'girder.plugins': mock.MagicMock(),
                            'girder.plugins.worker': girder_plugins}):
        with mock.patch.object(girder_plugins, 'utils') as utils:
            yield utils


class MockHTTPError(requests.HTTPError):
    def __init__(self, status_code, json_response):
        self.response = mock.MagicMock()
        self.response.status_code = status_code
        self.response.json.return_value = json_response


class TestSignals(unittest.TestCase):
    def setUp(self):
        self.headers = {
            'jobInfoSpec': {
                'method': 'PUT',
                'url': 'http://girder:8888/api/v1',
                'reference': 'JOBINFOSPECREFERENCE',
                'headers': {'Girder-Token': 'GIRDERTOKEN'},
                'logPrint': True
            }
        }

    @mock.patch('girder.utility.model_importer.ModelImporter')
    @mock.patch('girder.api.rest.getCurrentUser')
    def test_girder_before_task_publish_with_jobinfospec_no_job_created(self, gcu, mi):
        inputs = dict(sender='test.task',
                      # args, kwargs, options
                      body=[(), {}, {}],
                      headers=self.headers)

        create_job = mi.model.return_value.createJob
        with mock_worker_plugin_utils():
            girder_before_task_publish(**inputs)
        gcu.assert_called_once()
        self.assertTrue(not create_job.called)

    @mock.patch('girder.utility.model_importer.ModelImporter')
    @mock.patch('girder.api.rest.getCurrentUser')
    def test_girder_before_task_publish_called_with_no_headers_create_job_inputs(self, gcu, mi):

        inputs = dict(sender='test.task',
                      # args, kwargs, options
                      body=[(), {}, {}],
                      headers={'id': 'CELERY-ASYNCRESULT-ID'})

        create_job = mi.model.return_value.createJob

        with mock_worker_plugin_utils():
            girder_before_task_publish(**inputs)

        gcu.assert_called_once()
        create_job.assert_called_once_with(
            **{'title': '<unnamed job>',
               'type': 'celery',
               'handler': 'celery_handler',
               'public': False,
               'user': gcu.return_value,
               'args': (),
               'kwargs': {},
               'otherFields': {'celeryTaskId': 'CELERY-ASYNCRESULT-ID'}})

    @mock.patch('girder.utility.model_importer.ModelImporter')
    @mock.patch('girder.api.rest.getCurrentUser')
    def test_girder_before_task_publish_called_with_headers_create_job_inputs(self, gcu, mi):

        inputs = dict(sender='test.task',
                      # args, kwargs, options
                      body=[(), {}, {}],
                      headers={'id': 'CELERY-ASYNCRESULT-ID',
                               'girder_job_title': 'GIRDER_JOB_TITLE',
                               'girder_job_type': 'GIRDER_JOB_TYPE',
                               'girder_job_handler': 'GIRDER_JOB_HANDLER',
                               'girder_job_public': 'GIRDER_JOB_PUBLIC',
                               'girder_job_other_fields': {'SOME_OTHER': 'FIELD'}})

        create_job = mi.model.return_value.createJob

        with mock_worker_plugin_utils():
            girder_before_task_publish(**inputs)

        gcu.assert_called_once()
        create_job.assert_called_once_with(
            **{'title': 'GIRDER_JOB_TITLE',
               'type': 'GIRDER_JOB_TYPE',
               'handler': 'GIRDER_JOB_HANDLER',
               'public': 'GIRDER_JOB_PUBLIC',
               'user': gcu.return_value,
               'args': (),
               'kwargs': {},
               'otherFields': {'celeryTaskId': 'CELERY-ASYNCRESULT-ID',
                               'SOME_OTHER': 'FIELD'}})

    @mock.patch('girder.utility.model_importer.ModelImporter')
    @mock.patch('girder.api.rest.getCurrentUser')
    def test_girder_before_task_publish_jobinfospec_called(self, gcu, mi):

        inputs = dict(sender='test.task',
                      # args, kwargs, options
                      body=[(), {}, {}],
                      headers={'id': 'CELERY-ASYNCRESULT-ID'})

        with mock_worker_plugin_utils() as utils:
            girder_before_task_publish(**inputs)

            utils.jobInfoSpec.asset_called_once()
            utils.getWorkerApiUrl.assert_called_once()

    @mock.patch('girder_worker.utils.JobManager')
    def test_task_prerun(self, jm):
        task = mock.MagicMock()
        task.request.jobInfoSpec = self.headers['jobInfoSpec']
        task.request.parent_id = None

        gw_task_prerun(task=task)

        task.job_manager.updateStatus.assert_called_once_with(
            JobStatus.RUNNING)

    @mock.patch('girder_worker.app.is_revoked')
    @mock.patch('girder_worker.utils.JobManager')
    def test_task_success(self, jm,  is_revoked):
        task = mock.MagicMock()
        task.request.parent_id = None
        task.job_manager = jm(**self.headers)
        is_revoked.return_value = False

        gw_task_success(sender=task)

        task.job_manager.updateStatus.assert_called_once_with(
            JobStatus.SUCCESS)

    @mock.patch('girder_worker.utils.JobManager')
    def test_task_failure(self, jm):
        task = mock.MagicMock()
        task.request.parent_id = None
        task.job_manager = jm(**self.headers)

        exc, tb = mock.MagicMock(), mock.MagicMock()
        exc.__str__.return_value = 'MOCKEXCEPTION'

        with mock.patch('girder_worker.app.tb') as traceback:
            traceback.format_tb.return_value = 'TRACEBACK'

            gw_task_failure(sender=task, exception=exc, traceback=tb)

            task.job_manager.write.assert_called_once_with(
                'MagicMock: MOCKEXCEPTION\nTRACEBACK')
            task.job_manager.updateStatus(JobStatus.ERROR)

    @mock.patch('girder_worker.utils.JobManager')
    def test_task_postrun(self, jm):
        task = mock.MagicMock()
        task.request.parent_id = None
        task.job_manager = jm(**self.headers)

        gw_task_postrun(task=task)

        task.job_manager._flush.assert_called_once()
        task.job_manager._redirectPipes.assert_called_once_with(False)

    @mock.patch('girder_worker.utils.requests.request')
    @mock.patch('girder_worker.utils.JobManager.refreshStatus')
    def test_task_prerun_canceling(self, refreshStatus, request):
        task = mock.MagicMock()
        task.request.jobInfoSpec = self.headers['jobInfoSpec']
        task.request.parent_id = None

        validation_error = {
            'field': 'status',
            'message': 'invalid state'
        }
        r = request.return_value
        r.raise_for_status.side_effect = [MockHTTPError(400, validation_error)]

        refreshStatus.return_value = JobStatus.CANCELING

        gw_task_prerun(task=task)

        refreshStatus.assert_called_once()
        self.assertEqual(request.call_args_list[0][1]['data']['status'], JobStatus.RUNNING)

        # Now try with QUEUED
        request.reset_mock()
        refreshStatus.reset_mock()
        r.raise_for_status.side_effect = [None]
        refreshStatus.return_value = JobStatus.QUEUED

        gw_task_prerun(task=task)

        refreshStatus.assert_not_called()
        self.assertEqual(request.call_args_list[0][1]['data']['status'], JobStatus.RUNNING)

    @mock.patch('girder_worker.app.is_revoked')
    @mock.patch('girder_worker.utils.requests.request')
    @mock.patch('girder_worker.utils.JobManager.refreshStatus')
    def test_task_success_canceling(self, refreshStatus, request, is_revoked):
        task = mock.MagicMock()
        task.request.jobInfoSpec = self.headers['jobInfoSpec']
        task.request.parent_id = None

        validation_error = {
            'field': 'status',
            'message': 'invalid'
        }
        r = request.return_value
        r.raise_for_status.side_effect = [None, MockHTTPError(400, validation_error)]
        refreshStatus.return_value = JobStatus.CANCELING
        is_revoked.return_value = True

        gw_task_prerun(task)
        gw_task_success(sender=task)

        # We where in the canceling state so we should move into CANCELED
        refreshStatus.assert_called_once()
        self.assertEqual(request.call_args_list[1][1]['data']['status'], JobStatus.CANCELED)

        # Now try with RUNNING
        request.reset_mock()
        is_revoked.return_value = False
        refreshStatus.reset_mock()
        r.raise_for_status.side_effect = [None, None]

        gw_task_prerun(task)
        gw_task_success(sender=task)

        # We should move into SUCCESS
        refreshStatus.assert_not_called()
        self.assertEqual(request.call_args_list[1][1]['data']['status'], JobStatus.SUCCESS)

    @mock.patch('girder_worker.utils.JobManager')
    def test_task_revoke(self, jm):
        task = mock.MagicMock()
        request = mock.MagicMock()
        request.message.headers = {
            'jobInfoSpec': self.headers['jobInfoSpec']
        }
        task.request.parent_id = None

        gw_task_revoked(sender=task, request=request)

        task.job_manager.updateStatus.assert_called_once_with(JobStatus.CANCELED)
