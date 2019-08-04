from __future__ import (
    absolute_import,
    unicode_literals,
)

import unittest
import uuid

from pysoa.common.metrics import NoOpMetricsRecorder
from pysoa.common.transport.http2_gateway.server import Http2ServerTransport
from pysoa.test.compatibility import mock


@mock.patch('pysoa.common.transport.http2_gateway.server.Http2ServerTransportCore')
class TestServerTransport(unittest.TestCase):
    @staticmethod
    def _get_transport(service='my_service', **kwargs):
        return Http2ServerTransport(service, NoOpMetricsRecorder(), **kwargs)

    def test_core_args(self, mock_core):
        transport = self._get_transport(hello='world', goodbye='earth')

        mock_core.assert_called_once_with(
            service_name='my_service',
            hello='world',
            goodbye='earth',
            metrics=transport.metrics,
            metrics_prefix='server',
        )

        mock_core.reset_mock()

        transport = self._get_transport(hello='world', goodbye='earth', maximum_message_size_in_bytes=79)

        mock_core.assert_called_once_with(
            service_name='my_service',
            hello='world',
            goodbye='earth',
            metrics=transport.metrics,
            metrics_prefix='server',
            maximum_message_size_in_bytes=79,
        )

    def test_receive_request_message(self, mock_core):
        transport = self._get_transport()

        request_id = uuid.uuid4().hex
        meta = {'app': 'ppa'}
        message = {'test': 'payload'}

        mock_core.return_value.receive_message.return_value = request_id, meta, message

        self.assertEqual((request_id, meta, message), transport.receive_request_message())

        mock_core.return_value.receive_message.assert_called_once_with()

    def test_receive_request_message_another_service(self, mock_core):
        transport = self._get_transport('geo')

        request_id = uuid.uuid4().hex
        message = {'another': 'message'}

        mock_core.return_value.receive_message.return_value = request_id, {}, message

        self.assertEqual((request_id, {}, message), transport.receive_request_message())

        mock_core.return_value.receive_message.assert_called_once_with()

    def test_send_response_message(self, mock_core):
        transport = self._get_transport()

        request_id = uuid.uuid4().hex
        meta = {'app': 'ppa'}
        message = {'test': 'payload'}

        transport.send_response_message(request_id, meta, message)

        mock_core.return_value.send_message.assert_called_once_with(
            request_id,
            meta,
            message,
        )
