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
from h2.settings import SettingCodes


class Protocol:

    def data_received(self, data):
        raise NotImplementedError("This method should be overridden")

    def data_to_send(self):
        raise NotImplementedError("This method should be overridden")


class ProtocolError(Exception):
    """ An error on the protocol layer
    """


class H2Connection(Protocol):

    def __init__(self, transport):
        super().__init__()
        config = h2.config.H2Configuration(
            client_side=False, header_encoding=None
        )
        self.transport = transport
        self.key = uuid4().hex
        self.conn = h2.connection.H2Connection(config=config)
        self.streams = {}
        self.connectionMade()
        self._stillProducing = True


    def connectionMade(self):
        """
        Initiate HTTP2 connection
        """
        self.conn.initiate_connection()
        self.transport.sendall(self.conn.data_to_send())

    def data_received(self, data):
        """
        Called whenever a chunk of data is received from the transport.
        """

        try:
            events = self.conn.receive_data(data)
        except h2.exceptions.ProtocolError:
            # A remote protocol error terminates the connection.
            data_to_send = self.conn.data_to_send()
            if data_to_send:
                self.transport.sendall(data_to_send)

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
            # elif isinstance(event, h2.events.PriorityUpdated):
            #     self._handlePriorityUpdate(event)
            elif isinstance(event, h2.events.ConnectionTerminated):
                self.connectionLost()
            # elif isinstance(event, h2.events.RemoteSettingsChanged):
            #     if SettingCodes.INITIAL_WINDOW_SIZE in event.changed_settings:
            #         self.window_updated(None, 0)

        data_to_send = self.conn.data_to_send()
        if data_to_send:
            self.transport.sendall(data_to_send)
        return stream_done

    def _handleWindowUpdate(self, event):
        """
        A window update frame was received. Unblock some number of flow control
        Futures.
        """
        if event.stream_id and event.stream_id in self.streams:
            self._send_data(event.stream_id)
        elif not event.stream_id:
            for stream in list(self.streams.values()):
                self._send_data(stream.stream_id)

    def connectionLost(self):
        """
        Called when the transport connection is lost.

        Informs all outstanding response handlers that the connection has been
        lost, and cleans up all internal state.
        """
        for streamID in list(self.streams.keys()):
            self._requestDone(streamID)

    def _requestEnded(self, event):
        """
        Internal handler for when a request is complete, and we expect no
        further data for that request.
        """
        stream = self.streams[event.stream_id]
        stream.requestComplete()
        return self.streams[event.stream_id]

    def _requestAborted(self, event):
        """
        Internal handler for when a request is aborted by a remote peer.
        """
        self._requestDone(event.stream_id)

    def _requestDone(self, streamID):
        """
        Called internally by the data sending loop to clean up state that was
        being used for the stream. Called when the stream is complete.
        """
        del self.streams[streamID]

    def _requestReceived(self, event):
        """
        Internal handler for when a request has been received.

        """
        stream = H2Stream(
            event.stream_id,
            self,
            event.headers,
        )
        self.streams[event.stream_id] = stream

    def _requestDataReceived(self, event):
        """
        Internal handler for when a chunk of data is received for a given
        request.
        """
        self.conn.acknowledge_received_data(len(event.data), event.stream_id)
        stream = self.streams[event.stream_id]
        stream.receiveDataChunk(event.data, event.flow_controlled_length)

    def start_data_send(self, stream_id, serialized_message, response_headers):
        # Write headers on the transport
        self.conn.send_headers(stream_id, response_headers)
        self.transport.send(self.conn.data_to_send())

        self.streams[stream_id].write_data_to_stream(serialized_message)
        print('total data %s', len(serialized_message))
        self._send_data(stream_id)

    def _send_data(self, stream_id):
        chunk_size = min(
            self.conn.local_flow_control_window(stream_id),
            self.conn.max_outbound_frame_size,
        )
        chunk_size = max(0, chunk_size)

        if chunk_size == 0:
            return

        stream = self.streams[stream_id]
        data = stream.pop_stream_outbound_data(chunk_size)

        if data:
            self.conn.send_data(stream_id, data)
            self.transport.send(self.conn.data_to_send())

        if stream.complete:
            self.conn.end_stream(stream_id)
            self.transport.send(self.conn.data_to_send())

            self._requestDone(stream.stream_id)
        else:
            self._send_data(stream_id)


class H2Stream:

    def __init__(self, stream_id, h2_connection, headers):
        self.stream_id = stream_id
        self.completed = False
        self.h2_connection = h2_connection
        self.headers = headers
        self.stream_inbound_data = io.BytesIO()
        self.stream_outbound_data = bytearray()

    def __del__(self):
        self.stream_inbound_data.close()

    @property
    def complete(self):
        """
        Returns true if there is not more outbound data to send
        """
        return len(self.stream_outbound_data) == 0

    def receiveDataChunk(self, data, flowControlledLength):
        """
        Called when the connection has received a chunk of data from the
        underlying transport. If the stream has been registered with a
        consumer, and is currently able to push data, immediately passes it
        through. Otherwise, buffers the chunk until we can start producing.
        """
        self.stream_inbound_data.write(data)

    def write_data_to_stream(self, data):
        self.stream_outbound_data.extend(data)

    def pop_stream_outbound_data(self, max_length):
        length = min(len(self.stream_outbound_data), max_length)
        data = bytes(self.stream_outbound_data[:length])
        del self.stream_outbound_data[:length]

        return data

    def requestComplete(self):
        """
        Called by the L{H2Connection} when the all data for a request has been
        received. Currently, with the legacy L{twisted.web.http.Request}
        object, just calls requestReceived unless the producer wants us to be
        quiet.
        """
        self.completed = True
