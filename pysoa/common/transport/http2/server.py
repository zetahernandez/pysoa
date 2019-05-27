from __future__ import (
    absolute_import,
    unicode_literals,
)

from pysoa.common.metrics import TimerResolution
from pysoa.common.serializer.msgpack_serializer import MsgpackSerializer
from pysoa.common.transport.base import ServerTransport
from pysoa.common.transport.exceptions import (
    InvalidMessageError,
    MessageReceiveTimeout,
)
from pysoa.common.transport.http2.core import Http2TransportCore
from pysoa.common.transport.http2.settings import Http2TransportSchema
from pysoa.common.transport.http2.socketserver import (
    H2Connection,
    SocketServer,
)


class Http2ServerTransport(ServerTransport):

    def __init__(self, service_name, metrics, server=None, **kwargs):
        super(Http2ServerTransport, self).__init__(service_name, metrics)

        self.server = server
        # self.core = Http2TransportCore(service_name=service_name, metrics=metrics, metrics_prefix='server', **kwargs)
        self.core = SocketServer(
            server_address=(
                kwargs['http_host'],
                int(kwargs['http_port']),
            ),
            protocol_class=H2Connection
        )
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

            return (
                request_id,
                message.get('meta', {}),
                message.get('body'),
            )

    def on_receive_message(self, request_id, meta, body):
        self.server.handle_request(
            request_id,
            meta,
            body,
        )

    def run(self):
        try:
            self.core.init_selector(listener=self)
        except MessageReceiveTimeout:
            self.server.perform_idle_actions()
            return

    def receive_request_message(self):
        timer = self.metrics.timer('server.transport.http2.receive', resolution=TimerResolution.MICROSECONDS)
        timer.start()
        stop_timer = True
        try:
            return self.core.receive_message()
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

        self.core.send_message_response(
            meta['stream_id'],
            request_id,
            serialized_message,
            response_headers,
        )


Http2ServerTransport.settings_schema = Http2TransportSchema(Http2ServerTransport)
