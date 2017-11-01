import girder_worker
from girder_worker import logger
import traceback as tb
import celery
from celery import Celery, __version__
from distutils.version import LooseVersion
from celery.signals import (task_prerun, task_postrun,
                            task_failure, task_success,
                            worker_ready, before_task_publish, task_revoked)
from celery.result import AsyncResult

from celery.task.control import inspect

import json
from six.moves import configparser
import sys
from .utils import JobStatus, StateTransitionException
from girder_client import GirderClient


class GirderAsyncResult(AsyncResult):
    def __init__(self, *args, **kwargs):
        self._job = None
        super(GirderAsyncResult, self).__init__(*args, **kwargs)

    @property
    def job(self):
        if self._job is None:
            try:
                # GirderAsyncResult() objects may be instantiated in either a girder REST
                # request,  or in some other context (e.g. from a running girder_worker
                # instance if there is a chain).  If we are in a REST request we should
                # have access to the girder package and can directly access the database
                # If we are in a girder_worker context (or even in python console or a
                # testing context) then we should get an ImportError and we can make a REST
                # request to get the information we need.
                from girder.utility.model_importer import ModelImporter
                job_model = ModelImporter.model('job', 'jobs')

                try:
                    return job_model.findOne({'celeryTaskId': self.task_id})
                except IndexError:
                    return None

            except ImportError:
                # Make a rest request to get the job info
                return None

        return self._job


class Task(celery.Task):
    """Girder Worker Task object"""

    _girder_job_title = '<unnamed job>'
    _girder_job_type = 'celery'
    _girder_job_public = False
    _girder_job_handler = 'celery_handler'
    _girder_job_other_fields = {}

    # These keys will be removed from apply_async's kwargs or options and
    # transfered into the headers of the message.
    reserved_headers = [
        'girder_client_token',
        'girder_api_url',
        'girder_parent_id']

    # These keys will be available in the 'properties' dictionary inside
    # girder_before_task_publish() but will not be passed along in the message
    reserved_options = [
        'girder_user',
        'girder_job_title',
        'girder_job_type',
        'girder_job_public',
        'girder_job_handler',
        'girder_job_other_fields']

    def AsyncResult(self, task_id, **kwargs):
        return GirderAsyncResult(task_id, backend=self.backend,
                                 task_name=self.name, app=self.app, **kwargs)

    def apply_async(self, args=None, kwargs=None, task_id=None, producer=None,
                    link=None, link_error=None, shadow=None, **options):

        # Pass girder related job information through to
        # the signals by adding this information to options['headers']
        # This sets defaults for reserved_options based on the class defaults,
        # or values defined by the girder_job() dectorator
        headers = {
            'girder_job_title': self._girder_job_title,
            'girder_job_type': self._girder_job_type,
            'girder_job_public': self._girder_job_public,
            'girder_job_handler': self._girder_job_handler,
            'girder_job_other_fields': self._girder_job_other_fields,
        }

        # Certain keys may show up in either kwargs (e.g. via .delay(girder_token='foo')
        # or in options (e.g.  .apply_async(args=(), kwargs={}, girder_token='foo')
        # For those special headers,  pop them out of kwargs or options and put them
        # in headers so they can be picked up by the before_task_publish signal.
        for key in self.reserved_headers + self.reserved_options:
            if kwargs is not None and key in kwargs:
                headers[key] = kwargs.pop(key)
            if key in options:
                headers[key] = options.pop(key)

        if 'headers' in options:
            options['headers'].update(headers)
        else:
            options['headers'] = headers

        return super(Task, self).apply_async(
            args=args, kwargs=kwargs, task_id=task_id, producer=producer,
            link=link, link_error=link_error, shadow=shadow, **options)

    @property
    def canceled(self):
        """
        A property to indicate if a task has been canceled.

        :returns True is this task has been canceled, False otherwise.
        """
        return is_revoked(self)

    def describe(self):
        # The describe module indirectly depends on this module, so to
        # avoid circular imports, we import describe_function here.
        from describe import describe_function
        return describe_function(self.run)

    def call_item_task(self, inputs, outputs={}):
        return self.run.call_item_task(inputs, outputs)


@before_task_publish.connect # noqa
def girder_before_task_publish(sender=None, body=None, exchange=None,
                               routing_key=None, headers=None, properties=None,
                               declare=None, retry_policy=None, **kwargs):

    def _setArgToChainedTasks(arg, val):
        # Helper function which sets a value to chained child tasks
        chainedTasks = body[2].get('chain')
        if chainedTasks:
            for i in chainedTasks:
                if arg not in i.kwargs.keys():
                    i.kwargs[arg] = val

    def _getToken():
        """Helper function to get the token"""
        from girder.utility.model_importer import ModelImporter
        from girder.api.rest import getCurrentUser
        token_model = ModelImporter.model('token')
        scope = 'jobs.rest.create_job'
        try:
            token = token_model.createToken(scope=scope, user=user)
        except NameError:
            token = token_model.createToken(scope=scope, user=getCurrentUser())

        return token

    if 'jobInfoSpec' not in headers:
        try:
            # Note: If we can import these objects from the girder packages we
            # assume our producer is in a girder REST request. This allows
            # us to create the job model's directly. Otherwise there will be an
            # ImportError and we can create the job via a REST request using
            # the jobInfoSpec in headers.
            from girder.utility.model_importer import ModelImporter
            from girder.plugins.worker import utils
            from girder.api.rest import getCurrentUser
            job_model = ModelImporter.model('job', 'jobs')

            user = headers.pop('girder_user', getCurrentUser())
            task_args, task_kwargs = body[0], body[1]

            job = job_model.createJob(
                **{'title': headers.pop('girder_job_title', Task._girder_job_title),
                   'type': headers.pop('girder_job_type', Task._girder_job_type),
                   'handler': headers.pop('girder_job_handler', Task._girder_job_handler),
                   'public': headers.pop('girder_job_public', Task._girder_job_public),
                   'user': user,
                   'args': task_args,
                   'kwargs': task_kwargs,
                   'otherFields': dict(celeryTaskId=headers['id'],
                                       **headers.pop('girder_job_other_fields',
                                                     Task._girder_job_other_fields))})
            headers['jobInfoSpec'] = utils.jobInfoSpec(job)

        except ImportError:
            gc = GirderClient(apiUrl=headers['girder_api_url'])
            gc.token = headers['girder_client_token']
            task_args, task_kwargs = body[0], body[1]
            parameters = {
                'title': headers.pop('girder_job_title', Task._girder_job_title),
                'type': headers.pop('girder_job_type', Task._girder_job_type),
                'handler': headers.pop('girder_job_handler', Task._girder_job_handler),
                'public': headers.pop('girder_job_public', Task._girder_job_public),
                'args': json.dumps(task_args),
                'kwargs': task_kwargs,
                'otherFields': json.dumps(dict(celeryTaskId=headers['id'],
                                               celeryParentTaskId=headers['parent_id'],
                                               **headers.pop('girder_job_other_fields',
                                                             Task._girder_job_other_fields)))
            }
            job = gc.post('job', parameters=parameters)
            headers['jobInfoSpec'] = job['jobInfoSpec']

    if 'girder_api_url' not in headers:
        try:
            from girder.plugins.worker import utils
            headers['girder_api_url'] = utils.getWorkerApiUrl()
            _setArgToChainedTasks('girder_api_url', headers['girder_api_url'])
        except ImportError:
            # TODO: handle situation where girder_worker is producing the message
            #       Note - this may not come up at all depending on how we pass
            #       girder_api_url through to the next task (e.g. in the context
            #       of chaining events)
            pass

    if 'girder_client_token' not in headers:
        try:
            token = _getToken()
            headers['girder_client_token'] = token['_id']
            _setArgToChainedTasks('girder_client_token', headers['girder_client_token'])
        except ImportError:
            # TODO: handle situation where girder_worker is producing the message
            #       Note - this may not come up at all depending on how we pass
            #       girder_token through to the next task (e.g. in the context
            #       of chaining events)
            pass

    # Finally,  remove all reserved_options from headers
    for key in Task.reserved_options:
        headers.pop(key, None)


@worker_ready.connect
def check_celery_version(*args, **kwargs):
    if LooseVersion(__version__) < LooseVersion('4.0.0'):
        sys.exit("""You are running Celery {}.

girder-worker requires celery>=4.0.0""".format(__version__))


def deserialize_job_info_spec(**kwargs):
    return girder_worker.utils.JobManager(**kwargs)


class JobSpecNotFound(Exception):
    pass


def _job_manager(request=None, headers=None, kwargs=None):
    if hasattr(request, 'jobInfoSpec'):
        jobSpec = request.jobInfoSpec

    # We are being called from revoked signal
    elif headers is not None and \
            'jobInfoSpec' in headers:
        jobSpec = headers['jobInfoSpec']

    # Deprecated: This method of passing job information
    # to girder_worker is deprecated. Newer versions of girder
    # pass this information automatically as apart of the
    # header metadata in the worker scheduler.
    elif kwargs and 'jobInfo' in kwargs:
        jobSpec = kwargs.pop('jobInfo', {})

    else:
        raise JobSpecNotFound

    return deserialize_job_info_spec(**jobSpec)


def _update_status(task, status):
    # For now,  only automatically update status if this is
    # not a child task. Otherwise child tasks completion will
    # update the parent task's jobModel in girder.
    task.job_manager.updateStatus(status)


@task_prerun.connect
def gw_task_prerun(task=None, sender=None, task_id=None,
                   args=None, kwargs=None, **rest):
    """Deserialize the jobInfoSpec passed in through the headers.

    This provides the a JobManager class as an attribute of the
    task before task execution.  decorated functions may bind to
    their task and have access to the job_manager for logging and
    updating their status in girder.
    """
    try:
        task.job_manager = _job_manager(task.request, task.request.headers)
        _update_status(task, JobStatus.RUNNING)

    except JobSpecNotFound:
        task.job_manager = None
        logger.warn('No jobInfoSpec. Setting job_manager to None.')
    except StateTransitionException:
        # Fetch the current status of the job
        status = task.job_manager.refreshStatus()
        # If we are canceling we want to stay in that state
        if status != JobStatus.CANCELING:
            raise

    try:
        task.girder_client = GirderClient(apiUrl=task.request.girder_api_url)
        task.girder_client.token = task.request.girder_client_token
    except AttributeError:
        task.girder_client = None


@task_success.connect
def gw_task_success(sender=None, **rest):
    try:

        if not is_revoked(sender):
            _update_status(sender, JobStatus.SUCCESS)

        # For tasks revoked directly
        else:
            _update_status(sender, JobStatus.CANCELED)
    except AttributeError:
        pass
    except StateTransitionException:
        # Fetch the current status of the job
        status = sender.job_manager.refreshStatus()
        # If we are in CANCELING move to CANCELED
        if status == JobStatus.CANCELING or is_revoked(sender):
            _update_status(sender, JobStatus.CANCELED)
        else:
            raise


@task_failure.connect
def gw_task_failure(sender=None, exception=None,
                    traceback=None, **rest):
    try:

        msg = '%s: %s\n%s' % (
            exception.__class__.__name__, exception,
            ''.join(tb.format_tb(traceback)))

        sender.job_manager.write(msg)

        _update_status(sender, JobStatus.ERROR)
    except AttributeError:
        pass


@task_postrun.connect
def gw_task_postrun(task=None, sender=None, task_id=None,
                    args=None, kwargs=None,
                    retval=None, state=None, **rest):
    try:
        task.job_manager._flush()
        task.job_manager._redirectPipes(False)
    except AttributeError:
        pass


@task_revoked.connect
def gw_task_revoked(sender=None, request=None, **rest):
    try:
        sender.job_manager = _job_manager(headers=request.message.headers,
                                          kwargs=request.kwargsrepr)
        _update_status(sender, JobStatus.CANCELED)
    except AttributeError:
        pass
    except JobSpecNotFound:
        logger.warn('No jobInfoSpec. Unable to move \'%s\' into CANCELED state.')


# Access to the correct "Inspect" instance for this worker
_inspector = None


def _worker_inspector(task):
    global _inspector
    if _inspector is None:
        _inspector = inspect([task.request.hostname])

    return _inspector


# Get this list of currently revoked tasks for this worker
def _revoked_tasks(task):
    _revoked = _worker_inspector(task).revoked()

    if _revoked is None:
        return []

    return _revoked.get(task.request.hostname, [])


def is_revoked(task):
    """
    Utility function to check is a task has been revoked.

    :param task: The task.
    :type task: celery.app.task.Task
    :return True, if this task is in the revoked list for this worker, False
            otherwise.
    """
    return task.request.id in _revoked_tasks(task)


class _CeleryConfig:
    CELERY_ACCEPT_CONTENT = ['json', 'pickle', 'yaml']


broker_uri = girder_worker.config.get('celery', 'broker')
try:
    backend_uri = girder_worker.config.get('celery', 'backend')
except configparser.NoOptionError:
    backend_uri = broker_uri

app = Celery(
    main=girder_worker.config.get('celery', 'app_main'),
    backend=backend_uri, broker=broker_uri,
    task_cls='girder_worker.app:Task')

app.config_from_object(_CeleryConfig)
