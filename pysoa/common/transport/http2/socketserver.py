from __future__ import (
    absolute_import,
    unicode_literals,
)

import queue
import selectors
import socket
import threading

from pysoa.common.transport.http2.protocol import ProtocolError


class SocketServer(object):

    address_family = socket.AF_INET

    socket_type = socket.SOCK_STREAM

    request_queue_size = 5

    def __init__(self, server_address, protocol_class, request_queue=None, response_queue=None):
        self.__is_shut_down = threading.Event()
        self.server_address = server_address
        self.protocol_class = protocol_class
        self.request_queue = request_queue
        self.response_queue = response_queue

        self.socket = socket.socket(self.address_family, self.socket_type)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.setblocking(False)
        self.protocol_by_request_id = {}

        self.server_bind()
        self.server_activate()
        self.__shutdown_request = False
        self.listener = None

    def server_bind(self):
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()

    def server_activate(self):
        self.socket.listen(self.request_queue_size)

    def server_close(self):
        self.socket.close()

    def accept(self):
        return self.socket.accept()

    def shutdown(self):
        """Stops the serve_forever loop.

        Blocks until the loop has finished. This must be called while
        serve_forever() is running in another thread, or it will
        deadlock.
        """
        self.__shutdown_request = True
        self.__is_shut_down.wait()

    def init_selector(self, poll_interval=0.5):
        self.__is_shut_down.clear()
        try:
            with selectors.DefaultSelector() as selector:
                self.selector = selector

                self.selector.register(self.socket, selectors.EVENT_READ)
                while not self.__shutdown_request:
                    events = self.selector.select(poll_interval)

                    for key, mask in events:
                        if key.data is None:
                            self._accept_wrapper(key.fileobj)
                        else:
                            self._service_connection(key, mask)

                    self.check_responses()
        finally:
            self.__shutdown_request = False
            self.__is_shut_down.set()

    def check_responses(self):
        """ Check for responses in response queue and tell the protocol to send it """

        try:
            protocol_key, stream_id, request_id, message, response_headers = self.response_queue.get_nowait()
        except queue.Empty:
            pass
        else:
            protocol = self.protocol_by_request_id.pop(protocol_key)
            protocol.send_data(stream_id, message, response_headers)

    def _accept_wrapper(self, socket):
        """ New connection from a client is aquired """
        conn, addr = socket.accept()  # Should be ready to read
        conn.setblocking(False)

        protocol = self.protocol_class()
        self.protocol_by_request_id[protocol.key] = protocol

        events = selectors.EVENT_READ | selectors.EVENT_WRITE
        self.selector.register(conn, events, data=protocol)

    def _service_connection(self, key, mask):
        conn = key.fileobj
        protocol = key.data

        if mask & selectors.EVENT_READ:
            try:
                recv_data = conn.recv(1024)  # Should be ready to read
            except ConnectionResetError:
                self.close_and_unregister(conn)
            else:
                if recv_data:
                    try:
                        stream = protocol.data_received(recv_data)
                    except ProtocolError:
                        data_to_send = protocol.data_to_send()
                        if data_to_send:
                            conn.sendall(data_to_send)
                        self.close_and_unregister(conn)
                    else:
                        if stream and self.request_queue:
                            self.request_queue.put_nowait(stream)
                            # request_id, meta, body = self.listener.parse_message(stream)
                            # self.protocol_by_request_id[request_id] = protocol
                            # self.listener.on_receive_message(request_id, meta, body)
                else:
                    self.close_and_unregister(conn)

        if mask & selectors.EVENT_WRITE:
            data_to_send = protocol.data_to_send()
            if data_to_send:
                conn.sendall(data_to_send)  # Should be ready to write

    def close_and_unregister(self, conn):
        try:
            self.selector.unregister(conn)
        except ValueError:
            pass

        try:
            conn.shutdown(2)
        except socket.error:
            pass
        try:
            conn.close()
        except socket.error:
            pass
