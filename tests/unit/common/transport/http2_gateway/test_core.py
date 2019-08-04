from __future__ import (
    absolute_import,
    unicode_literals,
)

import timeit
import unittest

import attr

from pysoa.common.serializer.json_serializer import JSONSerializer
from pysoa.common.serializer.msgpack_serializer import MsgpackSerializer
from pysoa.common.transport.exceptions import (
    InvalidMessageError,
    MessageReceiveError,
    MessageReceiveTimeout,
    MessageSendError,
    MessageTooLarge,
)
from pysoa.common.transport.http2_gateway.core import Http2ServerTransportCore
from pysoa.test.compatibility import mock


@attr.s
class MockSerializer(object):
    kwarg1 = attr.ib(default=None)
    kwarg2 = attr.ib(default=None)


@mock.patch('pysoa.common.transport.http2_gateway.core.run_socket_server')
class TestHttp2ServerTransportCore(unittest.TestCase):

    def setUp(self):
        pass

    def test_standard_client_created_with_defaults(self, mock_run_socket_server):
        core = Http2ServerTransportCore()

        self.assertEqual(60, core.message_expiry_in_seconds)
        self.assertEqual(10000, core.queue_capacity)
        self.assertEqual(10, core.queue_full_retries)
        self.assertEqual(5, core.receive_timeout_in_seconds)
        self.assertIsInstance(core.default_serializer, MsgpackSerializer)
        self.assertEqual(10000, core.requests_queue.maxsize)
        self.assertEqual(10000, core.responses_queue.maxsize)

        mock_run_socket_server.assert_called_once_with(
            core.requests_queue,
            core.responses_queue,
            http_host='127.0.0.1',
            http_port='60061'
        )

    def test_standard_client_created(self, mock_run_socket_server):
        core = Http2ServerTransportCore(
            service_name='example',
            backend_layer_kwargs={
                'http_host': 'localhost',
                'http_port': '54545',
            },
            message_expiry_in_seconds=30,
            queue_capacity=100,
            queue_full_retries=7,
            receive_timeout_in_seconds=10,
            default_serializer_config={'object': MockSerializer, 'kwargs': {'kwarg1': 'hello'}},
        )

        self.assertEqual('example', core.service_name)
        self.assertEqual(30, core.message_expiry_in_seconds)
        self.assertEqual(100, core.queue_capacity)
        self.assertEqual(7, core.queue_full_retries)
        self.assertEqual(10, core.receive_timeout_in_seconds)
        self.assertIsInstance(core.default_serializer, MockSerializer)
        self.assertEqual('hello', core.default_serializer.kwarg1)
        self.assertEqual(100, core.requests_queue.maxsize)
        self.assertEqual(100, core.responses_queue.maxsize)

        mock_run_socket_server.assert_called_once_with(
            core.requests_queue,
            core.responses_queue,
            http_host='localhost',
            http_port='54545'
        )

    @staticmethod
    def _get_core(**kwargs):
        return Http2ServerTransportCore(**kwargs)

    def test_invalid_request_id(self, mock_run_socket_server):
        core = self._get_core()

        with self.assertRaises(InvalidMessageError):
            # noinspection PyTypeChecker
            core.send_message(None, {}, {'test': 'payload'})

    def test_oversized_message_is_logged(self, mock_run_socket_server):
        core = self._get_core(log_messages_larger_than_bytes=150)

        message = {'test': ['payload%i' % i for i in range(100, 110)]}  # This creates a message > 150 bytes

        core.send_message(1, {'protocol_key': 1, 'stream_id': 1}, message)

    def test_send_queue_full(self, mock_run_socket_server):
        core = self._get_core(queue_full_retries=1, queue_capacity=3)
        meta = {'stream_id': 1, 'protocol_key': 1}
        request_id1 = 32
        request_id2 = 33
        request_id3 = 34
        request_id4 = 35

        core.send_message(request_id1, meta, {'test': 'payload1'})
        core.send_message(request_id2, meta, {'test': 'payload2'})
        core.send_message(request_id3, meta, {'test': 'payload3'})

        with self.assertRaises(MessageSendError) as error_context:
            core.send_message(request_id4, meta, {'test': 'payload4'})

        self.assertTrue('Http2 responses queue was full after 1 retries' in error_context.exception.args[0])

    def test_receive_timeout_default(self, mock_run_socket_server):
        core = self._get_core(receive_timeout_in_seconds=1)

        start = timeit.default_timer()
        with self.assertRaises(MessageReceiveTimeout) as error_context:
            core.receive_message()
        elapsed = timeit.default_timer() - start

        self.assertTrue('received' in error_context.exception.args[0])
        self.assertTrue(0.9 < elapsed < 1.1)

    def test_receive_timeout_override(self, mock_run_socket_server):
        core = self._get_core()

        start = timeit.default_timer()
        with self.assertRaises(MessageReceiveTimeout) as error_context:
            core.receive_message(receive_timeout_in_seconds=1)
        elapsed = timeit.default_timer() - start

        self.assertTrue('received' in error_context.exception.args[0])
        self.assertTrue(0.9 < elapsed < 1.1)

    def test_content_type_default(self, mock_run_socket_server):
        core = self._get_core(receive_timeout_in_seconds=3, message_expiry_in_seconds=10)

        core.requests_queue.put((1, 10, MsgpackSerializer().dict_to_blob({
            'request_id': 15,
            'meta': {},
            'body': {'foo': 'bar'}
        })))

        request_id, meta, body = core.receive_message()

        self.assertEqual(15, request_id)
        self.assertIn('serializer', meta)
        self.assertIn('stream_id', meta)
        self.assertIn('protocol_key', meta)
        self.assertEqual(3, len(meta))
        self.assertEqual(1, meta['stream_id'])
        self.assertEqual(10, meta['protocol_key'])
        self.assertEqual({'foo': 'bar'}, body)

    def test_content_type_explicit_msgpack(self, mock_run_socket_server):
        core = self._get_core(receive_timeout_in_seconds=3, message_expiry_in_seconds=10)
        core.requests_queue.put((1, 10, b'content-type:application/msgpack;' + MsgpackSerializer().dict_to_blob({
            'request_id': 15,
            'meta': {},
            'body': {'foo': 'bar'}
        })))

        request_id, meta, body = core.receive_message()

        self.assertEqual(15, request_id)
        self.assertIn('serializer', meta)
        self.assertEqual(3, len(meta))
        self.assertIsInstance(meta['serializer'], MsgpackSerializer)
        self.assertEqual({'foo': 'bar'}, body)

    def test_content_type_explicit_json(self, mock_run_socket_server):
        core = self._get_core(receive_timeout_in_seconds=3, message_expiry_in_seconds=10)
        core.requests_queue.put((1, 10, b'content-type : application/json ;' + JSONSerializer().dict_to_blob({
            'request_id': 15,
            'meta': {},
            'body': {'foo': 'bar'}
        })))

        request_id, meta, body = core.receive_message()

        self.assertEqual(15, request_id)
        self.assertIn('serializer', meta)
        self.assertEqual(3, len(meta))
        self.assertIsInstance(meta['serializer'], JSONSerializer)
        self.assertEqual({'foo': 'bar'}, body)
