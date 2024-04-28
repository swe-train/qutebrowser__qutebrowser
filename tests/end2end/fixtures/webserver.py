# SPDX-FileCopyrightText: Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Fixtures for the server webserver."""

import re
import sys
import json
import pathlib
import socket
import dataclasses
from http import HTTPStatus

import pytest
from qutebrowser.qt.core import pyqtSignal, QUrl

from end2end.fixtures import testprocess
from helpers import testutils


class Request(testprocess.Line):

    """A parsed line from the flask log output.

    Attributes:
        verb/path/status: Parsed from the log output.
    """

    def __init__(self, data):
        super().__init__(data)
        try:
            parsed = json.loads(data)
        except ValueError:
            raise testprocess.InvalidLine(data)

        assert isinstance(parsed, dict)
        assert set(parsed.keys()) == {'path', 'verb', 'status'}

        self.verb = parsed['verb']

        path = parsed['path']
        self.path = '/' if path == '/' else path.rstrip('/')

        self.status = parsed['status']
        self._check_status()

    def _check_status(self):
        """Check if the http status is what we expected."""
        path_to_statuses = {
            '/favicon.ico': [
                HTTPStatus.OK,
                HTTPStatus.PARTIAL_CONTENT,
                HTTPStatus.NOT_MODIFIED,
            ],

            '/does-not-exist': [HTTPStatus.NOT_FOUND],
            '/does-not-exist-2': [HTTPStatus.NOT_FOUND],
            '/404': [HTTPStatus.NOT_FOUND],

            '/redirect-later': [HTTPStatus.FOUND],
            '/redirect-self': [HTTPStatus.FOUND],
            '/redirect-to': [HTTPStatus.FOUND],
            '/relative-redirect': [HTTPStatus.FOUND],
            '/absolute-redirect': [HTTPStatus.FOUND],
            '/redirect-http/data/downloads/download.bin': [HTTPStatus.FOUND],

            '/cookies/set': [HTTPStatus.FOUND],
            '/cookies/set-custom': [HTTPStatus.FOUND],

            '/500-inline': [HTTPStatus.INTERNAL_SERVER_ERROR],
            '/500': [HTTPStatus.INTERNAL_SERVER_ERROR],
        }
        for i in range(25):
            path_to_statuses['/redirect/{}'.format(i)] = [HTTPStatus.FOUND]
        for suffix in ['', '1', '2', '3', '4', '5', '6']:
            key = ('/basic-auth/user{suffix}/password{suffix}'
                   .format(suffix=suffix))
            path_to_statuses[key] = [HTTPStatus.UNAUTHORIZED, HTTPStatus.OK]

        default_statuses = [HTTPStatus.OK, HTTPStatus.NOT_MODIFIED]

        sanitized = QUrl('http://localhost' + self.path).path()  # Remove ?foo
        expected_statuses = path_to_statuses.get(sanitized, default_statuses)
        if self.status not in expected_statuses:
            raise AssertionError(
                "{} loaded with status {} but expected {}".format(
                    sanitized, self.status,
                    ' / '.join(repr(e) for e in expected_statuses)))

    def __eq__(self, other):
        return NotImplemented


@dataclasses.dataclass(frozen=True)
class ExpectedRequest:

    """Class to compare expected requests easily."""

    verb: str
    path: int

    @classmethod
    def from_request(cls, request):
        """Create an ExpectedRequest from a Request."""
        return cls(request.verb, request.path)

    def __eq__(self, other):
        if isinstance(other, (Request, ExpectedRequest)):
            return self.verb == other.verb and self.path == other.path
        else:
            return NotImplemented


def is_ignored_webserver_message(line: str) -> bool:
    return testutils.pattern_match(
        pattern=(
            "Client ('127.0.0.1', *) lost — peer dropped the TLS connection suddenly, "
            "during handshake: (1, '[SSL: SSLV3_ALERT_CERTIFICATE_UNKNOWN] * "
            "alert certificate unknown (_ssl.c:*)')"
        ),
        value=line,
    )


class WebserverProcess(testprocess.Process):

    """Abstraction over a running Flask server process.

    Reads the log from its stdout and parses it.

    Signals:
        new_request: Emitted when there's a new request received.
    """

    new_request = pyqtSignal(Request)
    Request = Request  # So it can be used from the fixture easily.
    ExpectedRequest = ExpectedRequest

    KEYS = ['verb', 'path']

    def __init__(self, request, script, parent=None):
        super().__init__(request, parent)
        self._script = script
        self.port = self._random_port()
        self.new_data.connect(self.new_request)

    def _random_port(self) -> int:
        """Get a random free port."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('localhost', 0))
            return sock.getsockname()[1]

    def get_requests(self):
        """Get the requests to the server during this test."""
        requests = self._get_data()
        return [r for r in requests if r.path != '/favicon.ico']

    def _parse_line(self, line):
        self._log(line)
        started_re = re.compile(r' \* Running on https?://127\.0\.0\.1:{}/? '
                                r'\(Press CTRL\+C to quit\)'.format(self.port))
        if started_re.fullmatch(line):
            self.ready.emit()
            return None

        try:
            return Request(line)
        except testprocess.InvalidLine:
            if is_ignored_webserver_message(line):
                return None
            raise

    def _executable_args(self):
        if hasattr(sys, 'frozen'):
            executable = str(pathlib.Path(sys.executable).parent / self._script)
            args = []
        else:
            executable = sys.executable
            py_file = (pathlib.Path(__file__).parent / self._script).with_suffix('.py')
            args = [str(py_file)]
        return executable, args

    def _default_args(self):
        return [str(self.port)]


@pytest.fixture(scope='session', autouse=True)
def server(qapp, request):
    """Fixture for an server object which ensures clean setup/teardown."""
    server = WebserverProcess(request, 'webserver_sub')
    server.start()
    yield server
    server.terminate()


@pytest.fixture(autouse=True)
def server_per_test(server, request):
    """Fixture to clean server request list after each test."""
    if not hasattr(request.node, '_server_logs'):
        request.node._server_logs = []
    request.node._server_logs.append(('server', server.captured_log))

    server.before_test()
    yield
    server.after_test()


@pytest.fixture
def server2(qapp, request):
    """Fixture for a second server object for cross-origin tests."""
    server = WebserverProcess(request, 'webserver_sub')

    if not hasattr(request.node, '_server_logs'):
        request.node._server_logs = []
    request.node._server_logs.append(('secondary server', server.captured_log))

    server.start()
    yield server
    server.terminate()


@pytest.fixture
def ssl_server(request, qapp):
    """Fixture for a webserver with a self-signed SSL certificate.

    This needs to be explicitly used in a test.
    """
    server = WebserverProcess(request, 'webserver_sub_ssl')

    if not hasattr(request.node, '_server_logs'):
        request.node._server_logs = []
    request.node._server_logs.append(('SSL server', server.captured_log))

    server.start()
    yield server
    server.after_test()
    server.terminate()
