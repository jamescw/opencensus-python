# Copyright 2017, OpenCensus Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Django middleware helper to capture and trace a request."""
import six

import logging
import sys
import traceback

import django
import django.conf
from django.db import connection
from django.utils.deprecation import MiddlewareMixin
from google.rpc import code_pb2

from opencensus.common import configuration
from opencensus.trace import (
    attributes_helper,
    execution_context,
    integrations,
    print_exporter,
    samplers,
)
from opencensus.trace import span as span_module
from opencensus.trace import status as status_module
from opencensus.trace import tracer as tracer_module
from opencensus.trace import utils
from opencensus.trace.propagation import trace_context_http_header_format

HTTP_HOST = attributes_helper.COMMON_ATTRIBUTES['HTTP_HOST']
HTTP_METHOD = attributes_helper.COMMON_ATTRIBUTES['HTTP_METHOD']
HTTP_PATH = attributes_helper.COMMON_ATTRIBUTES['HTTP_PATH']
HTTP_ROUTE = attributes_helper.COMMON_ATTRIBUTES['HTTP_ROUTE']
HTTP_URL = attributes_helper.COMMON_ATTRIBUTES['HTTP_URL']
HTTP_STATUS_CODE = attributes_helper.COMMON_ATTRIBUTES['HTTP_STATUS_CODE']
ERROR_MESSAGE = attributes_helper.COMMON_ATTRIBUTES['ERROR_MESSAGE']
ERROR_NAME = attributes_helper.COMMON_ATTRIBUTES['ERROR_NAME']
STACKTRACE = attributes_helper.COMMON_ATTRIBUTES['STACKTRACE']

REQUEST_THREAD_LOCAL_KEY = 'django_request'
SPAN_THREAD_LOCAL_KEY = 'django_span'

EXCLUDELIST_PATHS = 'EXCLUDELIST_PATHS'
EXCLUDELIST_HOSTNAMES = 'EXCLUDELIST_HOSTNAMES'

log = logging.getLogger(__name__)


class _DjangoMetaWrapper(object):
    """
    Wrapper class which takes HTTP header name and retrieve the value from
    Django request.META
    """

    def __init__(self, meta=None):
        self.meta = meta or _get_django_request().META

    def get(self, key):
        return self.meta.get('HTTP_' + key.upper().replace('-', '_'))


def _get_django_request():
    """Get Django request from thread local.

    :rtype: str
    :returns: Django request.
    """
    return execution_context.get_opencensus_attr(REQUEST_THREAD_LOCAL_KEY)


def _get_django_span():
    """Get Django span from thread local.

    :rtype: str
    :returns: Django request.
    """
    return execution_context.get_opencensus_attr(SPAN_THREAD_LOCAL_KEY)


def _get_current_tracer():
    """Get the current request tracer."""
    return execution_context.get_opencensus_tracer()


def _set_django_attributes(span, request):
    """Set the django related attributes."""
    django_user = getattr(request, 'user', None)

    if django_user is None:
        return

    user_id = django_user.pk
    user_name = django_user.get_username()

    # User id is the django autofield for User model as the primary key
    if user_id is not None:
        span.add_attribute('django.user.id', str(user_id))

    if user_name is not None:
        span.add_attribute('django.user.name', str(user_name))


def _trace_db_call(execute, sql, params, many, context):
    tracer = _get_current_tracer()
    if not tracer:
        return execute(sql, params, many, context)

    vendor = context['connection'].vendor
    alias = context['connection'].alias

    span = tracer.start_span()
    span.name = '{}.query'.format(vendor)
    span.span_kind = span_module.SpanKind.CLIENT

    tracer.add_attribute_to_current_span('component', vendor)
    tracer.add_attribute_to_current_span('db.instance', alias)
    tracer.add_attribute_to_current_span('db.statement', sql)
    tracer.add_attribute_to_current_span('db.type', 'sql')

    try:
        result = execute(sql, params, many, context)
    except Exception:  # pragma: NO COVER
        status = status_module.Status(
            code=code_pb2.UNKNOWN, message='DB error'
        )
        span.set_status(status)
        raise
    else:
        return result
    finally:
        tracer.end_span()


class OpencensusMiddleware(MiddlewareMixin):
    """Saves the request in thread local"""

    def __init__(self, get_response=None):
        self.get_response = get_response
        settings = getattr(django.conf.settings, 'OPENCENSUS', {})
        settings = settings.get('TRACE', {})

        self.sampler = (settings.get('SAMPLER', None)
                        or samplers.ProbabilitySampler())
        if isinstance(self.sampler, six.string_types):
            self.sampler = configuration.load(self.sampler)

        self.exporter = settings.get('EXPORTER', None) or \
            print_exporter.PrintExporter()
        if isinstance(self.exporter, six.string_types):
            self.exporter = configuration.load(self.exporter)

        self.propagator = settings.get('PROPAGATOR', None) or \
            trace_context_http_header_format.TraceContextPropagator()
        if isinstance(self.propagator, six.string_types):
            self.propagator = configuration.load(self.propagator)

        self.excludelist_paths = settings.get(EXCLUDELIST_PATHS, None)

        self.excludelist_hostnames = settings.get(EXCLUDELIST_HOSTNAMES, None)

        if django.VERSION >= (2,):  # pragma: NO COVER
            connection.execute_wrappers.append(_trace_db_call)

        # pylint: disable=protected-access
        integrations.add_integration(integrations._Integrations.DJANGO)

    def process_request(self, request):
        """Called on each request, before Django decides which view to execute.

        :type request: :class:`~django.http.request.HttpRequest`
        :param request: Django http request.
        """
        # Do not trace if the url is excludelisted
        if utils.disable_tracing_url(request.path, self.excludelist_paths):
            return

        # Add the request to thread local
        execution_context.set_opencensus_attr(
            REQUEST_THREAD_LOCAL_KEY,
            request)

        execution_context.set_opencensus_attr(
            'excludelist_hostnames',
            self.excludelist_hostnames)

        try:
            # Start tracing this request
            span_context = self.propagator.from_headers(
                _DjangoMetaWrapper(_get_django_request().META))

            # Reload the tracer with the new span context
            tracer = tracer_module.Tracer(
                span_context=span_context,
                sampler=self.sampler,
                exporter=self.exporter,
                propagator=self.propagator)

            # Span name is being set at process_view
            span = tracer.start_span()
            span.span_kind = span_module.SpanKind.SERVER
            tracer.add_attribute_to_current_span(
                attribute_key=HTTP_HOST,
                attribute_value=request.get_host())
            tracer.add_attribute_to_current_span(
                attribute_key=HTTP_METHOD,
                attribute_value=request.method)
            tracer.add_attribute_to_current_span(
                attribute_key=HTTP_PATH,
                attribute_value=str(request.path))
            tracer.add_attribute_to_current_span(
                attribute_key=HTTP_ROUTE,
                attribute_value=str(request.path))
            tracer.add_attribute_to_current_span(
                attribute_key=HTTP_URL,
                attribute_value=str(request.build_absolute_uri()))

            # Add the span to thread local
            # in some cases (exceptions, timeouts) currentspan in
            # response event will be one of a child spans.
            # let's keep reference to 'django' span and
            # use it in response event
            execution_context.set_opencensus_attr(
                SPAN_THREAD_LOCAL_KEY,
                span)

        except Exception:  # pragma: NO COVER
            log.error('Failed to trace request', exc_info=True)

    def process_view(self, request, view_func, *args, **kwargs):
        """Process view is executed before the view function, here we get the
        function name add set it as the span name.
        """

        # Do not trace if the url is excludelisted
        if utils.disable_tracing_url(request.path, self.excludelist_paths):
            return

        try:
            # Get the current span and set the span name to the current
            # function name of the request.
            tracer = _get_current_tracer()
            span = tracer.current_span()
            span.name = utils.get_func_name(view_func)
        except Exception:  # pragma: NO COVER
            log.error('Failed to trace request', exc_info=True)

    def process_response(self, request, response):
        # Do not trace if the url is excludelisted
        if utils.disable_tracing_url(request.path, self.excludelist_paths):
            return response

        try:
            span = _get_django_span()
            span.add_attribute(
                attribute_key=HTTP_STATUS_CODE,
                attribute_value=response.status_code)

            _set_django_attributes(span, request)

            tracer = _get_current_tracer()
            tracer.end_span()
            tracer.finish()
        except Exception:  # pragma: NO COVER
            log.error('Failed to trace request', exc_info=True)
        finally:
            return response

    def process_exception(self, request, exception):
        # Do not trace if the url is excluded
        if utils.disable_tracing_url(request.path, self.excludelist_paths):
            return

        try:
            if hasattr(exception, '__traceback__'):
                tb = exception.__traceback__
            else:
                _, _, tb = sys.exc_info()

            span = _get_django_span()
            span.add_attribute(
                attribute_key=ERROR_NAME,
                attribute_value=exception.__class__.__name__)
            span.add_attribute(
                attribute_key=ERROR_MESSAGE,
                attribute_value=str(exception))
            span.add_attribute(
                attribute_key=STACKTRACE,
                attribute_value='\n'.join(traceback.format_tb(tb)))

            _set_django_attributes(span, request)
        except Exception:  # pragma: NO COVER
            log.error('Failed to trace request', exc_info=True)
