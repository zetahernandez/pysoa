from pysoa.common.transport.http2_gateway.backend.base import BaseHTTP2BackendThread

from pysoa.common.transport.http2_gateway.socketserver import SocketServer
from pysoa.common.transport.http2_gateway.protocol import H2Connection


class HyperH2BackendThread(BaseHTTP2BackendThread):

    def __init__(self, *args, **kwargs):
        super(HyperH2BackendThread, self).__init__(*args, **kwargs)

        self.socket_server = SocketServer(
            server_address=(
                self.backend_layer_config['http_host'],
                int(self.backend_layer_config['http_port']),
            ),
            protocol_class=H2Connection,
            request_queue=self.requests_queue,
            response_queue=self.responses_queue,
        )

    def run(self):
        self.socket_server.init_selector()
