from __future__ import (
    absolute_import,
    unicode_literals,
)

import queue
from threading import Thread

from conformity import fields

from pysoa.common.metrics import TimerResolution
from pysoa.common.serializer.msgpack_serializer import MsgpackSerializer
from pysoa.common.transport.base import ServerTransport
from pysoa.common.transport.exceptions import (
    InvalidMessageError,
    MessageReceiveTimeout,
)
from pysoa.common.transport.http2.settings import Http2TransportSchema
from pysoa.common.transport.http2.socketserver import (
    H2Connection,
    SocketServer,
)


def run(request_queue, response_queue, **kwargs):

    socket_server = SocketServer(
        server_address=(
            kwargs['http_host'],
            int(kwargs['http_port']),
        ),
        protocol_class=H2Connection,
        request_queue=request_queue,
        response_queue=response_queue,
    )
    socket_server.init_selector()


@fields.ClassConfigurationSchema.provider(Http2TransportSchema())
class Http2ServerTransport(ServerTransport):

    def __init__(self, service_name, metrics, **kwargs):
        super(Http2ServerTransport, self).__init__(service_name, metrics)

        self.request_queue = queue.Queue()
        self.response_queue = queue.Queue()

        self.socker_server_daemon = Thread(
            target=run,
            args=(self.request_queue, self.response_queue),
            kwargs=kwargs['backed_layer_kwargs']
        )
        self.socker_server_daemon.setDaemon(True)
        self.socker_server_daemon.start()

        self._default_serializer = None
        self.default_serializer_config = {'object': MsgpackSerializer}

    @property
    def default_serializer(self):
        if self._default_serializer is None:
            self._default_serializer = self.default_serializer_config['object'](
                **self.default_serializer_config.get('kwargs', {})
            )

        return self._default_serializer

    def parse_message(self, stream):
        if stream.stream_id:
            serializer = self.default_serializer

            message = serializer.blob_to_dict(stream.stream_data.getvalue())
            request_id = message.get('request_id')
            meta = message.get('meta', {})
            meta['stream_id'] = stream.stream_id
            meta['protocol_key'] = stream.h2_connection.key
            return (
                request_id,
                message.get('meta', {}),
                message.get('body'),
            )

    def receive_request_message(self):
        timer = self.metrics.timer('server.transport.http2.receive', resolution=TimerResolution.MICROSECONDS)
        timer.start()
        stop_timer = True
        try:
            stream = self.request_queue.get()
            return self.parse_message(stream)
        except MessageReceiveTimeout:
            stop_timer = False
            raise
        finally:
            if stop_timer:
                timer.stop()

    def send_response_message(self, request_id, meta, body):
        message = {'request_id': request_id, 'meta': meta, 'body': body}

        serializer = self.default_serializer

        serialized_message = serializer.dict_to_blob(message)

        response_headers = [
            (':status', '200'),
            ('content-type', 'application/json'),
            ('content-length', str(len(serialized_message))),
            ('server', 'pysoa-h2'),
        ]

        self.response_queue.put_nowait((
            meta['protocol_key'],
            meta['stream_id'],
            request_id,
            serialized_message,
            response_headers,
        ))
