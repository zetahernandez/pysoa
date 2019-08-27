from __future__ import (
    absolute_import,
    unicode_literals,
)

import six

from OpenSSL import crypto
from twisted.internet import (
    endpoints,
    reactor,
    ssl,
    task,
)
from twisted.web import (
    resource,
    server,
)

from pysoa.common.transport.http2_gateway.backend.base import BaseHTTP2BackendThread


class TwistedHTTP2BackendThread(BaseHTTP2BackendThread):

    def __init__(self, *args, **kwargs):
        super(TwistedHTTP2BackendThread, self).__init__(*args, **kwargs)

    def run(self):
        with open('cert.pem', 'r') as f:
            cert_data = f.read()

        with open('privkey.pem', 'r') as f:
            key_data = f.read()

        site = server.Site(PySOADelayedResource(
            requests_queue=self.requests_queue,
            responses_queue=self.responses_queue,
        ))
        cert = crypto.load_certificate(crypto.FILETYPE_PEM, cert_data)
        key = crypto.load_privatekey(crypto.FILETYPE_PEM, key_data)

        options = ssl.CertificateOptions(
            privateKey=key,
            certificate=cert,
            acceptableProtocols=[b'h2'],
        )
        endpoint = endpoints.SSL4ServerEndpoint(
            reactor,
            int(self.backend_layer_config['http_port']),
            options,
            interface=self.backend_layer_config['http_host']
        )
        endpoint.listen(site)
        reactor.run(installSignalHandlers=0)


class PySOADelayedResource(resource.Resource):
    isLeaf = True

    def __init__(self, requests_queue, responses_queue):
        self.requests_queue = requests_queue
        self.responses_queue = responses_queue

    def _delayed_response(self, request):
        try:
            protocol_key, stream_id, request_id, message, response_headers = self.responses_queue.get_nowait()
        except six.moves.queue.Empty:
            d = task.deferLater(reactor, 0, lambda: request)
            d.addCallback(self._delayed_response)
            return server.NOT_DONE_YET

        request.write(message)
        request.finish()

    def render_POST(self, request):
        try:
            self.requests_queue.put((
                None,
                None,
                request.content.read(),
            ), timeout=3)
        except six.moves.queue.Full:
            pass

        d = task.deferLater(reactor, 0, lambda: request)
        d.addCallback(self._delayed_response)
        return server.NOT_DONE_YET
