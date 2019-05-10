import collections
import io
import selectors
import socket
import threading
import types

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions


class SocketServer(object):

    address_family = socket.AF_INET

    socket_type = socket.SOCK_STREAM

    request_queue_size = 5

    def __init__(self, server_address, protocol_class):

        self.__is_shut_down = threading.Event()
        self.protocol_class = protocol_class

        self.server_address = server_address
        self.socket = socket.socket(self.address_family, self.socket_type)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.setblocking(False)

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

    def init_selector(self, poll_interval=0.5, listener=None):
        self.listener = listener
        self.__is_shut_down.clear()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.socket, selectors.EVENT_READ)
                while not self.__shutdown_request:
                    events = selector.select(poll_interval)

                    for key, mask in events:
                        if key.data is None:
                            self.accept_wrapper(key.fileobj, selector)
                        else:
                            self.service_connection(key, mask, selector)
        finally:
            self.__shutdown_request = False
            self.__is_shut_down.set()

    def send_message_response(self, stream_id, request_id, message, request_headers):
        if request_id in self.protocol_by_request_id:
            protocol = self.protocol_by_request_id[request_id]
            protocol.send_data(stream_id, message, request_headers)

    def accept_wrapper(self, socket, selector):
        conn, addr = socket.accept()  # Should be ready to read
        conn.setblocking(False)

        protocol = self.protocol_class(self, selector, socket)
        events = selectors.EVENT_READ | selectors.EVENT_WRITE
        selector.register(conn, events, data=protocol)

    def service_connection(self, key, mask, selector):
        sock = key.fileobj
        protocol = key.data
        if mask & selectors.EVENT_READ:
            recv_data = sock.recv(1024)  # Should be ready to read
            if recv_data:
                stream = protocol.dataReceived(recv_data)
                if stream and self.listener:
                    request_id, meta, body = self.listener.parse_message(stream)
                    self.protocol_by_request_id[request_id] = protocol
                    self.listener.on_receive_message(request_id, meta, body)
            else:
                self.close_and_unregister(sock, selector)
        if mask & selectors.EVENT_WRITE:
            data_to_send = protocol.get_data_to_send()
            if data_to_send:
                sent = sock.sendall(data_to_send)  # Should be ready to write

    def close_and_unregister(self, sock, selector):
        selector.unregister(sock)
        sock.close()


class Protocol:
    def __init__(self, socket_server, selector, sock):
        self.socket_server = socket_server
        self.selector = selector
        self.sock = sock

    def dataReceived(self, data):
        raise NotImplementedError("This method should be overridden")

    def get_data_to_send(self):
        raise NotImplementedError("This method should be overridden")


class H2Connection(Protocol):

    def __init__(self, socket_server, selector, sock):
        super().__init__(socket_server, selector, sock)

        config = h2.config.H2Configuration(
            client_side=False, header_encoding=None
        )
        self.conn = h2.connection.H2Connection(config=config)
        self.streams = {}
        self.connectionMade()
        self._data_to_send = collections.deque()
        self._stillProducing = True

    def get_data_to_send(self):
        try:
            return self._data_to_send.pop()
        except IndexError:
            return None

    def connectionMade(self):
        """
        Called by the reactor when a connection is received. May also be called
        by the L{twisted.web.http._GenericHTTPChannelProtocol} during upgrade
        to HTTP/2.
        """
        # self.setTimeout(self.timeOut)
        self.conn.initiate_connection()
        self._data_to_send.append(self.conn.data_to_send())

    def dataReceived(self, data):
        """
        Called whenever a chunk of data is received from the transport.

        @param data: The data received from the transport.
        @type data: L{bytes}
        """
        # self.resetTimeout()

        try:
            events = self.conn.receive_data(data)
        except h2.exceptions.ProtocolError:
            # A remote protocol error terminates the connection.
            dataToSend = self.conn.data_to_send()
            # Send data and close the connection, this will write
            # in the socket when the selector ¡s in reading mode

            self.socket.sendall(dataToSend)
            self.socket_server.close_and_unregister(self.sock, self.selector)
            # self.connectionLost(Failure())
            self.connectionLost()
            return
        stream_done = None
        for event in events:
            if isinstance(event, h2.events.RequestReceived):
                self._requestReceived(event)
            elif isinstance(event, h2.events.DataReceived):
                self._requestDataReceived(event)
            elif isinstance(event, h2.events.StreamEnded):
                self._requestEnded(event)
                stream_done = self.streams[event.stream_id]
            elif isinstance(event, h2.events.StreamReset):
                self._requestAborted(event)
            elif isinstance(event, h2.events.WindowUpdated):
                self._handleWindowUpdate(event)
            elif isinstance(event, h2.events.PriorityUpdated):
                self._handlePriorityUpdate(event)
            elif isinstance(event, h2.events.ConnectionTerminated):
                self.socket_server.close_and_unregister(self.sock, self.selector)
                # self.connectionLost(ConnectionLost("Remote peer sent GOAWAY"))
                self.connectionLost()
        dataToSend = self.conn.data_to_send()
        if dataToSend:
            self._data_to_send.append(dataToSend)
        return stream_done

    def connectionLost(self):
        """
        Called when the transport connection is lost.

        Informs all outstanding response handlers that the connection has been
        lost, and cleans up all internal state.
        """
        self._stillProducing = False
        # self.setTimeout(None)

        # for stream in self.streams.values():
        #     stream.connectionLost(reason)

        for streamID in list(self.streams.keys()):
            self._requestDone(streamID)

        # If we were going to force-close the transport, we don't have to now.
        # if self._abortingCall is not None:
        #     self._abortingCall.cancel()
        #     self._abortingCall = None

    def _requestEnded(self, event):
        """
        Internal handler for when a request is complete, and we expect no
        further data for that request.

        @param event: The Hyper-h2 event that encodes information about the
            completed stream.
        @type event: L{h2.events.StreamEnded}
        """
        stream = self.streams[event.stream_id]
        stream.requestComplete()
        return self.streams[event.stream_id]

    def _requestAborted(self, event):
        """
        Internal handler for when a request is aborted by a remote peer.

        @param event: The Hyper-h2 event that encodes information about the
            reset stream.
        @type event: L{h2.events.StreamReset}
        """
        # stream = self.streams[event.stream_id]
        # stream.connectionLost(
        #     ConnectionLost("Stream reset with code %s" % event.error_code)
        # )
        self._requestDone(event.stream_id)

    def _requestDone(self, streamID):
        """
        Called internally by the data sending loop to clean up state that was
        being used for the stream. Called when the stream is complete.

        @param streamID: The ID of the stream to clean up state for.
        @type streamID: L{int}
        """
        del self._outboundStreamQueues[streamID]
        # self.priority.remove_stream(streamID)
        del self.streams[streamID]
        # cleanupCallback = self._streamCleanupCallbacks.pop(streamID)
        # cleanupCallback.callback(streamID)

    def _requestReceived(self, event):
        """
        Internal handler for when a request has been received.

        @param event: The Hyper-h2 event that encodes information about the
            received request.
        @type event: L{h2.events.RequestReceived}
        """
        stream = H2Stream(
            event.stream_id,
            self,
            event.headers,
        )
        self.streams[event.stream_id] = stream
        # self._streamCleanupCallbacks[event.stream_id] = Deferred()
        self._outboundStreamQueues[event.stream_id] = collections.deque()

        # # Add the stream to the priority tree but immediately block it.
        # try:
        #     self.priority.insert_stream(event.stream_id)
        # except priority.DuplicateStreamError:
        #     # Stream already in the tree. This can happen if we received a
        #     # PRIORITY frame before a HEADERS frame. Just move on: we set the
        #     # stream up properly in _handlePriorityUpdate.
        #     pass
        # else:
        #     self.priority.block(event.stream_id)

    def _requestDataReceived(self, event):
        """
        Internal handler for when a chunk of data is received for a given
        request.

        @param event: The Hyper-h2 event that encodes information about the
            received data.
        @type event: L{h2.events.DataReceived}
        """
        stream = self.streams[event.stream_id]
        stream.receiveDataChunk(event.data, event.flow_controlled_length)

    def send_data(self, stream_id, serialized_message, request_headers):
        self.conn.send_data(stream_id, serialized_message, end_stream=True)
        self.conn.send_headers(stream_id, request_headers)
        self._data_to_send.append(self.conn.data_to_send())


class H2Stream:

    def __init__(self, stream_id, h2_connection, headers):
        """

        """
        self.stream_id = stream_id
        self.h2_connection = h2_connection
        self.headers = headers
        self.producing = True
        self.stream_data = io.BytesIO()
        self._inboundDataBuffer = collections.deque()

    def __del__(self):
        self.stream_data.close()

    def receiveDataChunk(self, data, flowControlledLength):
        """
        Called when the connection has received a chunk of data from the
        underlying transport. If the stream has been registered with a
        consumer, and is currently able to push data, immediately passes it
        through. Otherwise, buffers the chunk until we can start producing.

        @param data: The chunk of data that was received.
        @type data: L{bytes}

        @param flowControlledLength: The total flow controlled length of this
            chunk, which is used when we want to re-open the window. May be
            different to C{len(data)}.
        @type flowControlledLength: L{int}
        """
        if not self.producing:
            # Buffer data.
            self._inboundDataBuffer.append((data, flowControlledLength))
        else:
            self._request.handleContentChunk(data)
            self._conn.openStreamWindow(self.streamID, flowControlledLength)

    def requestComplete(self):
        """
        Called by the L{H2Connection} when the all data for a request has been
        received. Currently, with the legacy L{twisted.web.http.Request}
        object, just calls requestReceived unless the producer wants us to be
        quiet.
        """
        self.completed = True
