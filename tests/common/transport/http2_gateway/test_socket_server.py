import select
import socket
import test.support
from test.support import reap_threads, verbose, find_unused_port
import threading

from pysoa.common.transport.http2.socketserver import Protocol, SocketServer


HOST = test.support.HOST

TEST_STR = b'Echo message\n'

test.support.requires("network")


class EchoProtocol(Protocol):
    def __init__(self, transport, _, __):
        super().__init__(transport, _, __)
        self.buffer = b''

    def dataReceived(self, data):
        self.buffer += data

    def dataToSend(self):
        result = self.buffer
        self.buffer = b''
        return result


def make_server(srv_cls, protocol_cls):
    return srv_cls((HOST, find_unused_port()), protocol_cls)


def close_server(server):
    server.server_close()


@reap_threads
def run_server(svrcls, protocol_cls, testfunc):
    server = make_server(svrcls, protocol_cls)
    # We had the OS pick a port, so pull the real address out of
    # the server.
    addr = server.server_address
    if verbose:
        print("ADDR =", addr)
        print("CLASS =", svrcls)

    t = threading.Thread(
        name="%s serving" % svrcls,
        target=server.init_selector,
        # Short poll interval to make the test finish quickly.
        # Time between requests is short enough that we won't wake
        # up spuriously too many times.
        kwargs={"poll_interval": 0.01},
    )
    t.daemon = True  # In case this function raises.
    t.start()
    if verbose:
        print("server running")
    for i in range(3):
        if verbose:
            print("test client", i)
        testfunc(svrcls.address_family, addr)
    if verbose:
        print("waiting for server")
    server.shutdown()
    t.join()
    close_server(server)

    assert -1 == server.socket.fileno()
    if verbose:
        print("done")


_real_select = select.select


def receive(sock, n, timeout=20):
    r, w, x = _real_select([sock], [], [], timeout)
    if sock in r:
        return sock.recv(n)
    else:
        raise RuntimeError("timed out on %r" % (sock,))


def assert_stream(proto, addr):
    s = socket.socket(proto, socket.SOCK_STREAM)
    s.connect(addr)
    s.sendall(TEST_STR)
    buf = data = receive(s, 100)
    while data and b"\n" not in buf:
        data = receive(s, 100)
        buf += data

    assert buf == TEST_STR
    s.close()


def test_socket_server_echo():
    run_server(SocketServer, EchoProtocol, assert_stream)
