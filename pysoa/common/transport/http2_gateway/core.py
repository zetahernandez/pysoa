from __future__ import (
    absolute_import,
    unicode_literals,
)

import collections
import logging
import random
import threading
import time

import attr
import six

from pysoa.common.logging import RecursivelyCensoredDictWrapper
from pysoa.common.metrics import (
    MetricsRecorder,
    NoOpMetricsRecorder,
    TimerResolution,
)
from pysoa.common.serializer.base import Serializer
from pysoa.common.serializer.msgpack_serializer import MsgpackSerializer
from pysoa.common.transport.exceptions import (
    InvalidMessageError,
    MessageReceiveError,
    MessageReceiveTimeout,
    MessageSendError,
)
from pysoa.common.transport.http2_gateway.protocol import H2Connection as Http2Connection
from pysoa.common.transport.http2_gateway.socketserver import SocketServer
from pysoa.common.transport.redis_gateway.constants import DEFAULT_MAXIMUM_MESSAGE_BYTES_CLIENT
from hyper.http20.connection import HTTP20Connection


EXPONENTIAL_BACK_OFF_FACTOR = 4.0

RequestData = collections.namedtuple('RequestData', ['headers', 'data'])

_oversized_message_logger = logging.getLogger('pysoa.transport.oversized_message')


def run_socket_server(request_queue, response_queue, **kwargs):

    socket_server = SocketServer(
        server_address=(
            kwargs['http_host'],
            int(kwargs['http_port']),
        ),
        protocol_class=Http2Connection,
        request_queue=request_queue,
        response_queue=response_queue,
    )
    socket_server.init_selector()


@attr.s()
class Http2ServerTransportCore(object):

    backend_layer_kwargs = attr.ib(
        # Keyword args for the backend layer (Standard Redis and Sentinel Redis modes)
        default={},
        validator=attr.validators.instance_of(dict),
    )

    log_messages_larger_than_bytes = attr.ib(
        default=DEFAULT_MAXIMUM_MESSAGE_BYTES_CLIENT,
        converter=int,
    )

    message_expiry_in_seconds = attr.ib(
        # How long after a message is sent before it's considered "expired" and not received by default, unless
        # overridden in the send_message argument `message_expiry_in_seconds`
        default=60,
        converter=int,
    )

    metrics = attr.ib(
        default=NoOpMetricsRecorder(),
        validator=attr.validators.instance_of(MetricsRecorder),
    )

    metrics_prefix = attr.ib(
        default='',
        validator=attr.validators.instance_of(six.text_type),
    )

    queue_capacity = attr.ib(
        # The capacity for queues to which messages are sent
        default=10000,
        converter=int,
    )

    queue_full_retries = attr.ib(
        # Number of times to retry when the send queue is full
        default=10,
        converter=int,
    )

    receive_timeout_in_seconds = attr.ib(
        # How long to block when waiting to receive a message by default, unless overridden in the receive_message
        # argument `receive_timeout_in_seconds`
        default=5,
        converter=int,
    )

    default_serializer_config = attr.ib(
        # Configuration for which serializer should be used by this transport
        default={'object': MsgpackSerializer, 'kwargs': {}},
        converter=dict,
    )

    service_name = attr.ib(
        # Service name used for error messages
        default='',
        validator=attr.validators.instance_of(six.text_type),
    )

    def __attrs_post_init__(self):
        self.requests_queue = six.moves.queue.Queue(maxsize=self.queue_capacity)
        self.responses_queue = six.moves.queue.Queue(maxsize=self.queue_capacity)

        self.socker_server_daemon = threading.Thread(
            target=run_socket_server,
            args=(self.requests_queue, self.responses_queue),
            kwargs=self.backend_layer_kwargs
        )
        self.socker_server_daemon.setDaemon(True)
        self.socker_server_daemon.start()

        self._default_serializer = None

    # noinspection PyAttributeOutsideInit
    @property
    def default_serializer(self):
        if self._default_serializer is None:
            self._default_serializer = self.default_serializer_config['object'](
                **self.default_serializer_config.get('kwargs', {})
            )

        return self._default_serializer

    def send_message(self, request_id, meta, body, message_expiry_in_seconds=None):

        protocol_key = meta.get('protocol_key')
        stream_id = meta.get('stream_id')

        if request_id is None or protocol_key is None or stream_id is None:
            raise InvalidMessageError('No request ID')

        if message_expiry_in_seconds:
            message_expiry = time.time() + message_expiry_in_seconds
        else:
            message_expiry = time.time() + self.message_expiry_in_seconds

        meta['__expiry__'] = message_expiry

        message = {'request_id': request_id, 'meta': meta, 'body': body}

        with self._get_timer('send.serialize'):
            serializer = self.default_serializer
            if 'serializer' in meta:
                # TODO: Breaking change: Assume a MIME type is always specified. This should not be done until all
                # TODO servers and clients have Step 2 code. This will be a Step 3 breaking change.
                serializer = meta.pop('serializer')
            serialized_message = (
                'content-type:{};'.format(serializer.mime_type).encode('utf-8') + serializer.dict_to_blob(message)
            )

        message_size_in_bytes = len(serialized_message)

        response_headers = [
            (':status', '200'),
            ('content-type', 'application/json'),
            ('content-length', str(message_size_in_bytes)),
            ('server', 'pysoa-h2'),
        ]

        if self.log_messages_larger_than_bytes and message_size_in_bytes > self.log_messages_larger_than_bytes:
            _oversized_message_logger.warning(
                'Oversized message sent for PySOA service {}'.format(self.service_name),
                extra={'data': {
                    'message': RecursivelyCensoredDictWrapper(message),
                    'serialized_length_in_bytes': message_size_in_bytes,
                    'threshold': self.log_messages_larger_than_bytes,
                }},
            )
        for i in range(-1, self.queue_full_retries):
            if i >= 0:
                time.sleep((2 ** i + random.random()) / self.EXPONENTIAL_BACK_OFF_FACTOR)
                self._get_counter('send.responses_queue_full_retry').increment()
                self._get_counter('send.responses_queue_full_retry.retry_{}'.format(i + 1)).increment()
            try:
                with self._get_timer('send.send_message_response_http2_queue'):
                    self.responses_queue.put((
                        protocol_key,
                        stream_id,
                        request_id,
                        serialized_message,
                        response_headers,
                    ))
                return
            except six.moves.queue.Full:
                continue
            except Exception as e:
                self._get_counter('send.error.unknown').increment()
                raise MessageSendError(
                    'Unknown error sending message for service {}'.format(self.service_name),
                    six.text_type(type(e).__name__),
                    *e.args
                )

        self._get_counter('send.error.responses_queue_full').increment()
        raise MessageSendError(
            'Http2 responses queue  was full after {retries} retries'.format(
                retries=self.queue_full_retries,
            )
        )

    def receive_message(self, receive_timeout_in_seconds=None):
        try:
            with self._get_timer('recieve.get_from_requests_queue'):
                stream = self.requests_queue.get(
                    timeout=receive_timeout_in_seconds or self.receive_timeout_in_seconds,
                )
        except six.moves.queue.Empty:
            raise MessageReceiveTimeout('No message received for service {}'.format(self.service_name))
        except Exception as e:
            self._get_counter('receive.error.unknown').increment()
            raise MessageReceiveError(
                'Unknown error receiving message for service {}'.format(self.service_name),
                six.text_type(type(e).__name__),
                *e.args
            )

        serialized_message = stream.stream_data.getvalue()

        with self._get_timer('receive.deserialize'):
            serializer = self.default_serializer
            if serialized_message.startswith(b'content-type'):
                # TODO: Breaking change: Assume all messages start with a content type. This should not be done until
                # TODO all servers and clients have Step 2 code. This will be a Step 3 breaking change.
                header, serialized_message = serialized_message.split(b';', 1)
                mime_type = header.split(b':', 1)[1].decode('utf-8').strip()
                if mime_type in Serializer.all_supported_mime_types:
                    serializer = Serializer.resolve_serializer(mime_type)

            message = serializer.blob_to_dict(serialized_message)
            message.setdefault('meta', {})['serializer'] = serializer

            meta = message.get('meta')
            meta['stream_id'] = stream.stream_id
            meta['protocol_key'] = stream.h2_connection.key

        if self._is_message_expired(message):
            self._get_counter('receive.error.message_expired').increment()
            raise MessageReceiveTimeout('Message expired for service {}'.format(self.service_name))

        request_id = message.get('request_id')
        if request_id is None:
            self._get_counter('receive.error.no_request_id').increment()
            raise InvalidMessageError('No request ID for service {}'.format(self.service_name))

        return request_id, message.get('meta', {}), message.get('body')

    @staticmethod
    def _is_message_expired(message):
        return message.get('meta', {}).get('__expiry__') and message['meta']['__expiry__'] < time.time()

    def _get_metric_name(self, name):
        if self.metrics_prefix:
            return '{prefix}.transport.http2_gateway.{name}'.format(prefix=self.metrics_prefix, name=name)
        else:
            return 'transport.http2_gateway.{}'.format(name)

    def _get_counter(self, name):
        return self.metrics.counter(self._get_metric_name(name))

    def _get_timer(self, name):
        return self.metrics.timer(self._get_metric_name(name), resolution=TimerResolution.MICROSECONDS)


@attr.s()
class Http2ClientTransportCore(object):

    backend_layer_kwargs = attr.ib(
        # Keyword args for the backend layer (Standard Redis and Sentinel Redis modes)
        default={},
        validator=attr.validators.instance_of(dict),
    )

    message_expiry_in_seconds = attr.ib(
        # How long after a message is sent before it's considered "expired" and not received by default, unless
        # overridden in the send_message argument `message_expiry_in_seconds`
        default=60,
        converter=int,
    )

    metrics = attr.ib(
        default=NoOpMetricsRecorder(),
        validator=attr.validators.instance_of(MetricsRecorder),
    )

    metrics_prefix = attr.ib(
        default='',
        validator=attr.validators.instance_of(six.text_type),
    )

    receive_timeout_in_seconds = attr.ib(
        # How long to block when waiting to receive a message by default, unless overridden in the receive_message
        # argument `receive_timeout_in_seconds`
        default=5,
        converter=int,
    )

    default_serializer_config = attr.ib(
        # Configuration for which serializer should be used by this transport
        default={'object': MsgpackSerializer, 'kwargs': {}},
        converter=dict,
    )

    service_name = attr.ib(
        # Service name used for error messages
        default='',
        validator=attr.validators.instance_of(six.text_type),
    )

    def __attrs_post_init__(self):
        self._default_serializer = None
        self.requests = collections.deque()

        self.http_host = self.backend_layer_kwargs['http_host']
        self.http_port = self.backend_layer_kwargs['http_port']


    # noinspection PyAttributeOutsideInit
    @property
    def default_serializer(self):
        if self._default_serializer is None:
            self._default_serializer = self.default_serializer_config['object'](
                **self.default_serializer_config.get('kwargs', {})
            )

        return self._default_serializer

    def send_request_message(self, request_id, meta, body, message_expiry_in_seconds=None):
        connection = HTTP20Connection(host=self.http_host, port=self.http_port)
        message = {'request_id': request_id, 'meta': meta, 'body': body}

        serializer = self.default_serializer
        non_default_serializer = False
        if 'serializer' in meta:
            # TODO: Breaking change: Assume a MIME type is always specified. This should not be done until all
            # TODO servers and clients have Step 2 code. This will be a Step 3 breaking change.
            serializer = meta.pop('serializer')
            non_default_serializer = True
        serialized_message = serializer.dict_to_blob(message)
        if non_default_serializer:
            # TODO: Breaking change: Make this happen always, not just when a specific MIME type was requested.
            # TODO This should not be done until all servers and clients have this Step 1 code. This will be a Step
            # TODO 2 breaking change.
            serialized_message = (
                'content-type:{};'.format(serializer.mime_type).encode('utf-8') + serialized_message
            )

        request = connection.request('POST', '/', body=serialized_message)
        self.requests.append((connection, request))

    def receive_message(self, receive_timeout_in_seconds):
        connection, request = self.requests.popleft()
        response = connection.get_response(request)

        body = response.read()
        # headers = response.headers

        serializer = self.default_serializer
        if body.startswith(b'content-type'):
            # TODO: Breaking change: Assume all messages start with a content type. This should not be done until
            # TODO all servers and clients have Step 2 code. This will be a Step 3 breaking change.
            header, body = body.split(b';', 1)
            mime_type = header.split(b':', 1)[1].decode('utf-8').strip()
            if mime_type in Serializer.all_supported_mime_types:
                serializer = Serializer.resolve_serializer(mime_type)

        message = serializer.blob_to_dict(body)
        return (message.get('request_id'), message.get('meta', {}), message.get('body'))
