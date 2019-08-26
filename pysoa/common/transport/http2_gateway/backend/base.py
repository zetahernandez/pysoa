import threading

class BaseHTTP2BackendThread(threading.Thread):

    def __init__(self, requests_queue, responses_queue, backend_layer_config):
        threading.Thread.__init__(self, daemon=True)

        self.requests_queue = requests_queue
        self.responses_queue = responses_queue

        self.backend_layer_config = backend_layer_config

    def run(self):
        raise NotImplementedError()
