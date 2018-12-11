from __future__ import (
    absolute_import,
    unicode_literals,
)

from pysoa.common.metrics import TimerResolution
from pysoa.common.transport.base import ServerTransport
from pysoa.common.transport.exceptions import (
    InvalidMessageError,
    MessageReceiveTimeout,
)
from pysoa.common.transport.http2.settings import Http2TransportSchema

from pysoa.common.transport.http2.core import Http2TransportCore


class Http2ServerTransport(ServerTransport):

    def __init__(self, service_name, metrics, **kwargs):
        super(Http2ServerTransport, self).__init__(service_name, metrics)

        self.core = Http2TransportCore(service_name=service_name, metrics=metrics, metrics_prefix='server', **kwargs)

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
        with self.metrics.timer('server.transport.http2.send', resolution=TimerResolution.MICROSECONDS):
            self.core.send_message_response(request_id, meta, body)


Http2ServerTransport.settings_schema = Http2TransportSchema(Http2ServerTransport)
