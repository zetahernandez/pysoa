from __future__ import (
    absolute_import,
    unicode_literals,
)

import uuid

from pysoa.common.metrics import TimerResolution
from pysoa.common.transport.base import (
    ClientTransport,
)
from pysoa.common.transport.exceptions import MessageReceiveTimeout
from pysoa.common.transport.http2_gateway.settings import Http2TransportSchema

from pysoa.common.transport.http2_gateway.core import Http2ClientTransportCore
from conformity import fields


@fields.ClassConfigurationSchema.provider(Http2TransportSchema())
class Http2ClientTransport(ClientTransport):

    def __init__(self, service_name, metrics, **kwargs):
        super(Http2ClientTransport, self).__init__(service_name, metrics)

        self.client_id = uuid.uuid4().hex
        self.core = Http2ClientTransportCore(
            service_name=service_name,
            metrics=metrics,
            metrics_prefix='client',
            **kwargs
        )
        self._requests_outstanding = 0

    @property
    def requests_outstanding(self):
        return self._requests_outstanding

    def send_request_message(self, request_id, meta, body, message_expiry_in_seconds=None):
        self._requests_outstanding += 1
        with self.metrics.timer('client.transport.http2_gateway.send', resolution=TimerResolution.MICROSECONDS):
            self.core.send_request_message(request_id, meta, body, message_expiry_in_seconds)

    def receive_response_message(self, receive_timeout_in_seconds=None):
        if self._requests_outstanding > 0:
            with self.metrics.timer('client.transport.http2_gateway.receive', resolution=TimerResolution.MICROSECONDS):
                try:
                    request_id, meta, response = self.core.receive_message(
                        receive_timeout_in_seconds,
                    )
                except MessageReceiveTimeout:
                    self.metrics.counter('client.transport.http2_gateway.receive.error.timeout').increment()
                    raise
            self._requests_outstanding -= 1
            return request_id, meta, response
        else:
            # This tells Client.get_all_responses to stop waiting for more.
            return None, None, None
