from __future__ import (
    absolute_import,
    unicode_literals,
)

import collections
import io
from uuid import uuid4

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions


class Protocol:

    def data_received(self, data):
        raise NotImplementedError("This method should be overridden")

    def data_to_send(self):
        raise NotImplementedError("This method should be overridden")


class ProtocolError(Exception):
    """ An error on the protocol layer
    """


class H2Connection(Protocol):

    def __init__(self):
        super().__init__()
        config = h2.config.H2Configuration(
            client_side=False, header_encoding=None
        )
        self.key = uuid4().hex
        self.conn = h2.connection.H2Connection(config=config)
        self.streams = {}
        self._data_to_send = collections.deque()
        self.connectionMade()
        self._stillProducing = True

    def data_to_send(self):
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

    def data_received(self, data):
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
            data_to_send = self.conn.data_to_send()
            if data_to_send:
                self._data_to_send.append(data_to_send)

            self.connectionLost()
            raise ProtocolError()

        stream_done = None
        for event in events:
            if isinstance(event, h2.events.RequestReceived):
                self._requestReceived(event)
            elif isinstance(event, h2.events.DataReceived):
                self._requestDataReceived(event)
            elif isinstance(event, h2.events.StreamEnded):
                stream_done = self._requestEnded(event)
            elif isinstance(event, h2.events.StreamReset):
                self._requestAborted(event)
            elif isinstance(event, h2.events.WindowUpdated):
                self._handleWindowUpdate(event)
            elif isinstance(event, h2.events.PriorityUpdated):
                self._handlePriorityUpdate(event)
            elif isinstance(event, h2.events.ConnectionTerminated):
                self.connectionLost()

        data_to_send = self.conn.data_to_send()
        if data_to_send:
            self._data_to_send.append(data_to_send)
        return stream_done

    def connectionLost(self):
        """
        Called when the transport connection is lost.

        Informs all outstanding response handlers that the connection has been
        lost, and cleans up all internal state.
        """
        # self._stillProducing = False
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
        # del self._outboundStreamQueues[streamID]
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
        # self._outboundStreamQueues[event.stream_id] = collections.deque()

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

    def send_data(self, stream_id, serialized_message, response_headers):
        self.conn.send_headers(stream_id, response_headers)
        self.conn.send_data(stream_id, serialized_message, end_stream=True)
        self._data_to_send.append(self.conn.data_to_send())


class H2Stream:

    def __init__(self, stream_id, h2_connection, headers):
        """

        """
        self.stream_id = stream_id
        self.completed = False
        self.h2_connection = h2_connection
        self.headers = headers
        # self.producing = True
        self.stream_data = io.BytesIO()
        # self._inboundDataBuffer = collections.deque()

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
        self.stream_data.write(data)
        # if not self.producing:
        #     # Buffer data.
        # self._inboundDataBuffer.append((data, flowControlledLength))
        # else:
        #     self._request.handleContentChunk(data)
        #     self._conn.openStreamWindow(self.streamID, flowControlledLength)

    def requestComplete(self):
        """
        Called by the L{H2Connection} when the all data for a request has been
        received. Currently, with the legacy L{twisted.web.http.Request}
        object, just calls requestReceived unless the producer wants us to be
        quiet.
        """
        self.completed = True
