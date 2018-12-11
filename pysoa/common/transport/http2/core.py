from __future__ import (
    absolute_import,
    unicode_literals,
)

import collections
import io
import socket

import attr
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.errors import ErrorCodes
from h2.events import (
    ConnectionTerminated,
    DataReceived,
    RequestReceived,
    StreamEnded,
    ResponseReceived,
    SettingsAcknowledged,
    StreamReset,
)
from h2.exceptions import ProtocolError
import six

from pysoa.common.metrics import (
    MetricsRecorder,
    NoOpMetricsRecorder,
    TimerResolution,
)
from pysoa.common.serializer.msgpack_serializer import MsgpackSerializer
from pysoa.common.transport.exceptions import (
    InvalidMessageError,
    MessageReceiveError,
    MessageReceiveTimeout,
    MessageSendError,
    MessageTooLarge,
)


RequestData = collections.namedtuple('RequestData', ['headers', 'data'])


@attr.s()
class Http2TransportCore(object):

    http_host = attr.ib(
        default='127.0.0.1',
    )

    http_port = attr.ib(
        default=55933,
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

    # noinspection PyAttributeOutsideInit
    @property
    def default_serializer(self):
        if self._default_serializer is None:
            self._default_serializer = self.default_serializer_config['object'](
                **self.default_serializer_config.get('kwargs', {})
            )

        return self._default_serializer

    @property
    def transport(self):
        if self._socket:
            return self._socket
        self._socket = socket.socket(family=socket.AF_INET, type=socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.settimeout(1.0)
        self._socket.bind((self.http_host, self.http_port))
        self._socket.listen(1)

        return self._socket

    def send_message_response(self, request_id, meta, body):
        message = {'request_id': request_id, 'meta': meta, 'body': body}

        serializer = self.default_serializer

        serialized_message = serializer.dict_to_blob(message)

        response_headers = (
            (':status', '200'),
            ('content-type', 'application/json'),
            ('content-length', str(len(serialized_message))),
            ('server', 'asyncio-h2'),
        )
        self.conn.send_headers(meta.get('stream_id'), response_headers)
        self.conn.send_data(meta.get('stream_id'), serialized_message, end_stream=True)

        self.socket_conn.sendall(self.conn.data_to_send())

        while True:
            data = self.socket_conn.recv(65535)
            if not data:
                break

            self.data_received(data)

    def receive_message(self):
        try:
            conn, address = self.transport.accept()
        except socket.timeout:
            raise MessageReceiveTimeout('Timeout')

        self.conn.initiate_connection()
        self.socket_conn = conn

        data = self.conn.data_to_send()
        if data:
            self.socket_conn.sendall(data)

        while True:
            data = self.socket_conn.recv(65535)
            if not data:
                raise MessageReceiveTimeout('No message received for service')  # .format(self.service_name))

            stream_id, headers, body = self.data_received(data)
            if stream_id:
                serializer = self.default_serializer

                message = serializer.blob_to_dict(body)
                request_id = message.get('request_id')
                meta = message.get('meta', {})
                meta['stream_id'] = stream_id
                return request_id, message.get('meta', {}), message.get('body')

    def data_received(self, data):
        try:
            events = self.conn.receive_data(data)
        except ProtocolError:
            self.socket_conn.sendall(self.conn.data_to_send())
            self.transport.close()
        else:
            self.socket_conn.sendall(self.conn.data_to_send())
            for event in events:
                if isinstance(event, RequestReceived):
                    self.request_received(event.headers, event.stream_id)
                elif isinstance(event, DataReceived):
                    self.receive_data(event.data, event.stream_id)
                elif isinstance(event, StreamEnded):
                    return self.stream_complete(event.stream_id)
                elif isinstance(event, ConnectionTerminated):
                    config = H2Configuration(client_side=False, header_encoding='utf-8')
                    self.conn = H2Connection(config=config)
                    self.transport.close()
                    self._socket = None

                data = self.conn.data_to_send()
                if data:
                    self.transport.write(data)

        return None, None, None

    def request_received(self, headers, stream_id):
        headers = collections.OrderedDict(headers)
        method = headers[':method']

        # We only support GET and POST.
        if method not in ('GET', 'POST'):
            self.return_405(headers, stream_id)
            return

        # Store off the request data.
        request_data = RequestData(headers, io.BytesIO())
        self.stream_data[stream_id] = request_data

    def receive_data(self, data, stream_id):
        """
        We've received some data on a stream. If that stream is one we're
        expecting data on, save it off. Otherwise, reset the stream.
        """
        try:
            stream_data = self.stream_data[stream_id]
        except KeyError:
            self.conn.reset_stream(
                stream_id, error_code=ErrorCodes.PROTOCOL_ERROR
            )
        else:
            stream_data.data.write(data)

    def stream_complete(self, stream_id):
        """
        When a stream is complete, we can send our response.
        """
        try:
            request_data = self.stream_data[stream_id]
        except KeyError:
            # Just return, we probably 405'd this already
            return

        headers = request_data.headers
        body = request_data.data.getvalue()

        return stream_id, headers, body

    def __attrs_post_init__(self):
        config = H2Configuration(client_side=False, header_encoding='utf-8')
        self.conn = H2Connection(config=config)
        self._socket = None
        self.stream_data = {}
        self._default_serializer = None


@attr.s()
class Http2ClientTransportCore(object):

    http_host = attr.ib(
        default='127.0.0.1',
    )

    http_port = attr.ib(
        default=55933,
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

    # noinspection PyAttributeOutsideInit
    @property
    def default_serializer(self):
        if self._default_serializer is None:
            self._default_serializer = self.default_serializer_config['object'](
                **self.default_serializer_config.get('kwargs', {})
            )

        return self._default_serializer

    @property
    def transport(self):
        if self._socket:
            return self._socket
        self._socket = socket.socket(family=socket.AF_INET, type=socket.SOCK_STREAM)
        self._socket.connect((self.http_host, self.http_port))

        return self._socket

    def __attrs_post_init__(self):
        config = H2Configuration(client_side=True, header_encoding='utf-8')
        self.conn = H2Connection(config=config)
        self._socket = None
        self.stream_data = {}
        self.request_made = False
        self._default_serializer = None

    def send_request_message(self, request_id, meta, body, message_expiry_in_seconds=None):
        if request_id is None:
            raise InvalidMessageError('No request ID')

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

        self.conn.initiate_connection()
        self.transport.sendall(self.conn.data_to_send())

        while True:
            data = self.transport.recv(65535)
            if not data:
                raise MessageReceiveTimeout('No message received for service')  # .format(self.service_name))

            stream_id, headers, body = self.data_received(data, serialized_message)

            if stream_id:
                serializer = self.default_serializer

                message = serializer.blob_to_dict(body)
                self.response = (request_id, message.get('meta', {}), message.get('body'))
                break

    def data_received(self, data, serialized_message):
        try:
            events = self.conn.receive_data(data)
        except ProtocolError:
            self.socket_conn.sendall(self.conn.data_to_send())
            self.transport.close()
        else:
            self.transport.sendall(self.conn.data_to_send())
            for event in events:
                if isinstance(event, ResponseReceived):
                    self.handleResponse(event.headers, event.stream_id)
                elif isinstance(event, DataReceived):
                    self.handleData(event.data, event.stream_id)
                elif isinstance(event, StreamEnded):
                    return self.endStream(event.stream_id)
                elif isinstance(event, SettingsAcknowledged):
                    self.settingsAcked(event, serialized_message)
                elif isinstance(event, StreamReset):
                    self.transport.close()
                else:
                    print(event)

                data = self.conn.data_to_send()
                if data:
                    self.transport.sendall(data)
        return None, None, None

    def settingsAcked(self, event, serialized_message):
        # Having received the remote settings change, lets send our request.
        if not self.request_made:
            self.sendRequest(1, serialized_message)

    def sendRequest(self, stream_id, serialized_message):
        request_headers = [
            (':method', 'POST'),
            (':authority', ''),
            (':scheme', 'http'),
            (':path', '/{}'.format(self.service_name)),
            ('content-type', 'application/octet-stream'),
            ('content-length', str(len(serialized_message))),
            ('user-agent', 'hyper-h2/1.0.0'),
        ]
        self.conn.send_headers(stream_id, request_headers)
        self.request_made = True

        self.conn.send_data(stream_id, serialized_message, end_stream=True)

        self.transport.sendall(self.conn.data_to_send())

    def handleResponse(self, headers, stream_id):
        headers = collections.OrderedDict(headers)

        # Store off the request data.
        request_data = RequestData(headers, io.BytesIO())
        self.stream_data[stream_id] = request_data

    def handleData(self, data, stream_id):
        """
        We've received some data on a stream. If that stream is one we're
        expecting data on, save it off. Otherwise, reset the stream.
        """
        try:
            stream_data = self.stream_data[stream_id]
        except KeyError:
            self.conn.reset_stream(
                stream_id, error_code=ErrorCodes.PROTOCOL_ERROR
            )
        else:
            stream_data.data.write(data)

    def endStream(self, stream_id):
        """
        We call this when the stream is cleanly ended by the remote peer. That
        means that the response is complete.

        Because this code only makes a single HTTP/2 request, once we receive
        the complete response we can safely tear the connection down and stop
        the reactor. We do that as cleanly as possible.
        """
        self.request_complete = True
        self.conn.close_connection()
        self.transport.sendall(self.conn.data_to_send())
        self.transport.close()
        self._socket = None

        request_data = self.stream_data[stream_id]
        headers = request_data.headers
        body = request_data.data.getvalue()

        return stream_id, headers, body

    def receive_message(self, receive_timeout_in_seconds):
        return self.response
