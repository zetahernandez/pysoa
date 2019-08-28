from multiprocessing import Process

class BaseHTTP2BackendThread(Process):

    def __init__(self, requests_queue, responses_queue, backend_layer_config):
        super(Process, self).__init__(daemon=True)
        self.requests_queue = requests_queue
        self.responses_queue = responses_queue

        self.backend_layer_config = backend_layer_config

    def run(self):
        raise NotImplementedError()
