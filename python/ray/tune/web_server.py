from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import json
import logging
import sys
import threading

import ray.cloudpickle as cloudpickle
from ray.tune.error import TuneError, TuneManagerError
from ray.tune.suggest import BasicVariantGenerator
from ray.utils import binary_to_hex, hex_to_binary

if sys.version_info[0] == 2:
    from SimpleHTTPServer import SimpleHTTPRequestHandler
    from SocketServer import TCPServer as HTTPServer
elif sys.version_info[0] == 3:
    from http.server import SimpleHTTPRequestHandler, HTTPServer

logger = logging.getLogger(__name__)

try:
    import requests  # `requests` is not part of stdlib.
except ImportError:
    requests = None
    logger.exception("Couldn't import `requests` library. "
                     "Be sure to install it on the client side.")


def load_trial_info(trial_info):
    trial_info["config"] = cloudpickle.loads(
        hex_to_binary(trial_info["config"]))
    trial_info["result"] = cloudpickle.loads(
        hex_to_binary(trial_info["result"]))


class TuneClient(object):
    """Client to interact with ongoing Tune experiment.

    Requires server to have started running."""
    STOP = "STOP"
    ADD = "ADD"
    GET_LIST = "GET_LIST"
    GET_TRIAL = "GET_TRIAL"

    def __init__(self, tune_address):
        # TODO(rliaw): Better to specify address and port forward
        self._tune_address = tune_address
        self._path = "http://{}".format(tune_address)

    def get_all_trials(self):
        """Returns a list of all trials (trial_id, config, status)."""
        return self._get_response({"command": TuneClient.GET_LIST})

    def get_trial(self, trial_id):
        """Returns the last result for queried trial."""
        return self._get_response({
            "command": TuneClient.GET_TRIAL,
            "trial_id": trial_id
        })

    def add_trial(self, name, trial_spec):
        """Adds a trial of `name` with configurations."""
        # TODO(rliaw): have better way of specifying a new trial
        return self._get_response({
            "command": TuneClient.ADD,
            "name": name,
            "spec": trial_spec
        })

    def stop_trial(self, trial_id):
        """Requests to stop trial."""
        return self._get_response({
            "command": TuneClient.STOP,
            "trial_id": trial_id
        })

    def _get_response(self, data):
        payload = json.dumps(data).encode()
        response = requests.get(self._path, data=payload)
        parsed = response.json()

        if "trial_info" in parsed:
            load_trial_info(parsed["trial_info"])
        elif "trials" in parsed:
            for trial_info in parsed["trials"]:
                load_trial_info(trial_info)

        return parsed


def RunnerHandler(runner):
    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            content_len = int(self.headers.get('Content-Length'), 0)
            raw_body = self.rfile.read(content_len)
            parsed_input = json.loads(raw_body.decode())
            status, response = self.execute_command(parsed_input)
            if status:
                self.send_response(200)
            else:
                self.send_response(400)
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        def trial_info(self, trial):
            if trial.last_result:
                result = trial.last_result.copy()
            else:
                result = None
            info_dict = {
                "id": trial.trial_id,
                "trainable_name": trial.trainable_name,
                "config": binary_to_hex(cloudpickle.dumps(trial.config)),
                "status": trial.status,
                "result": binary_to_hex(cloudpickle.dumps(result))
            }
            return info_dict

        def execute_command(self, args):
            def get_trial():
                trial = runner.get_trial(args["trial_id"])
                if trial is None:
                    error = "Trial ({}) not found.".format(args["trial_id"])
                    raise TuneManagerError(error)
                else:
                    return trial

            command = args["command"]
            response = {}
            try:
                if command == TuneClient.GET_LIST:
                    response["trials"] = [
                        self.trial_info(t) for t in runner.get_trials()
                    ]
                elif command == TuneClient.GET_TRIAL:
                    trial = get_trial()
                    response["trial_info"] = self.trial_info(trial)
                elif command == TuneClient.STOP:
                    trial = get_trial()
                    runner.request_stop_trial(trial)
                elif command == TuneClient.ADD:
                    name = args["name"]
                    spec = args["spec"]
                    trial_generator = BasicVariantGenerator()
                    trial_generator.add_configurations({name: spec})
                    for trial in trial_generator.next_trials():
                        runner.add_trial(trial)
                else:
                    raise TuneManagerError("Unknown command.")
                status = True
            except TuneError as e:
                status = False
                response["message"] = str(e)

            return status, response

    return Handler


class TuneServer(threading.Thread):

    DEFAULT_PORT = 4321

    def __init__(self, runner, port=None):

        threading.Thread.__init__(self)
        self._port = port if port else self.DEFAULT_PORT
        address = ('localhost', self._port)
        logger.info("Starting Tune Server...")
        self._server = HTTPServer(address, RunnerHandler(runner))
        self.start()

    def run(self):
        self._server.serve_forever()

    def shutdown(self):
        self._server.shutdown()
