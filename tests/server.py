from __future__ import with_statement

from functools import wraps
import itertools
import os
import re
import socket
import sys
import threading
import time
import types
from Python26SocketServer import BaseRequestHandler, ThreadingMixIn, TCPServer

import paramiko as ssh

from fabric.operations import _sudo_prefix
from fabric.api import env, hide
from fabric.thread_handling import ThreadHandler
from fabric.network import disconnect_all


#
# Constants
#

PORT = 2200
USER = 'username'
RESPONSES = {
    "ls /simple": "some output",
    "ls /": """AUTHORS
FAQ
Fabric.egg-info
INSTALL
LICENSE
MANIFEST
README
build
docs
fabfile.py
fabfile.pyc
fabric
requirements.txt
setup.py
tests"""
}
PASSWORDS = {
    'root': 'root',
    USER: 'password'
}

def local_file(filename):
    return os.path.join(os.path.dirname(__file__), filename)

SERVER_PRIVKEY = local_file('server.key')
CLIENT_PUBKEY = local_file('client.key.pub')
CLIENT_PRIVKEY = local_file('client.key')
CLIENT_PRIVKEY_PASSPHRASE = "passphrase"


def _equalize(lists, fillval=None):
    """
    Pad all given list items in ``lists`` to be the same length.
    """
    lists = map(list, lists)
    upper = max(len(x) for x in lists)
    for lst in lists:
        diff = upper - len(lst)
        if diff:
            lst.extend([fillval] * diff)
    return lists


class ParamikoServer(ssh.ServerInterface):
    """
    Test-ready server implementing Paramiko's server interface parent class.

    Mostly just handles the bare minimum necessary to handle SSH-level things
    such as honoring authentication types and exec/shell/etc requests.

    The bulk of the actual server side logic is handled in the
    ``serve_responses`` function and its ``SSHHandler`` class.
    """
    def __init__(self, passwords, pubkeys):
        self.event = threading.Event()
        self.passwords = passwords
        self.pubkeys = pubkeys
        self.command = None

    def check_channel_request(self, kind, chanid):
        if kind == 'session':
            return ssh.OPEN_SUCCEEDED
        return ssh.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        self.command = command
        self.event.set()
        return True

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_shell_request(self, channel):
        self.event.set()
        return True

    def check_auth_password(self, username, password):
        self.username = username
        passed = self.passwords.get(username) == password
        return ssh.AUTH_SUCCESSFUL if passed else ssh.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        self.username = username
        return ssh.AUTH_SUCCESSFUL if self.pubkeys else ssh.AUTH_FAILED 

    def get_allowed_auths(self, username):
        return 'password,publickey'


class SSHServer(ThreadingMixIn, TCPServer):
    """
    Threading TCPServer subclass.
    """
    # Prevent "address already in use" errors when running tests 2x in a row.
    allow_reuse_address = True


def serve_responses(responses, passwords, pubkeys, port):
    """
    Return a threading TCP based SocketServer listening on ``port``.

    Used as a fake SSH server which will respond to commands given in
    ``responses`` and allow connections for users listed in ``passwords``.

    ``pubkeys`` is a Boolean value determining whether the server will allow
    pubkey auth or not.
    """
    # Define handler class inline so it can access serve_responses' args
    class SSHHandler(BaseRequestHandler):
        def handle(self):
            try:
                self.init_transport()
                self.waiting_for_command = False
                while not self.server.all_done.isSet():
                    # Don't overwrite channel if we're waiting for a command.
                    if not self.waiting_for_command:
                        self.channel = self.transport.accept(1)
                        if not self.channel:
                            continue
                    self.ssh_server.event.wait(10)
                    if self.ssh_server.command:
                        self.command = self.ssh_server.command
                        # Set self.sudo_prompt, update self.command
                        self.split_sudo_prompt()
                        if self.command in responses:
                            self.stdout, self.stderr, self.status = \
                                self.response()
                            if self.sudo_prompt and not self.sudo_password():
                                self.channel.send(
                                    "sudo: 3 incorrect password attempts\n"
                                )
                                break
                            self.respond()
                        else:
                            self.channel.send_stderr(
                                "Sorry, I don't recognize that command.\n"
                            )
                            self.channel.send_exit_status(1)
                        # Close up shop
                        self.command = self.ssh_server.command = None
                        self.waiting_for_command = False
                        time.sleep(0.5)
                        self.channel.close()
                    else:
                        # If we're here, self.command was False or None,
                        # but we do have a valid Channel object. Thus we're
                        # waiting for the command to show up.
                        self.waiting_for_command = True

            finally:
                self.transport.close()

        def init_transport(self):
            transport = ssh.Transport(self.request)
            transport.add_server_key(ssh.RSAKey(filename=SERVER_PRIVKEY))
            server = ParamikoServer(passwords, pubkeys)
            transport.start_server(server=server)
            self.ssh_server = server
            self.transport = transport

        def split_sudo_prompt(self):
            prefix = re.escape(_sudo_prefix(None).rstrip()) + ' +'
            result = re.findall(r'^(%s)?(.*)$' % prefix, self.command)[0]
            self.sudo_prompt, self.command = result

        def response(self):
            result = responses[self.command]
            stderr = ""
            status = 0
            if isinstance(result, types.StringTypes):
                stdout = result
            else:
                size = len(result)
                if size == 1:
                    stdout = result[0]
                elif size == 2:
                    stdout, stderr = result
                elif size == 3:
                    stdout, stderr, status = result
            stdout, stderr = _equalize((stdout, stderr))
            return stdout, stderr, status

        def sudo_password(self):
            # Give user 3 tries, as is typical
            passed = False
            for x in range(3):
                self.channel.send(env.sudo_prompt)
                password = self.channel.recv(65535).strip()
                # Spit back newline to fake the echo of user's
                # newline
                self.channel.send('\n')
                # Test password
                if password == passwords[self.ssh_server.username]:
                    passed = True
                    break
                # If here, password was bad.
                self.channel.send("Sorry, try again.\n")
            return passed

        def respond(self):
            for out, err in zip(self.stdout, self.stderr):
                if out is not None:
                    self.channel.send(out)
                if err is not None:
                    self.channel.send_stderr(err)
            self.channel.send_exit_status(self.status)

    return SSHServer(('localhost', port), SSHHandler)


def server(responses=RESPONSES, passwords=PASSWORDS, pubkeys=False, port=PORT):
    """
    Returns a decorator that runs an SSH server during function execution.

    Direct passthrough to ``serve_responses``.
    """
    def run_server(func):
        @wraps(func)
        def inner(*args, **kwargs):
            # Start server
            _server = serve_responses(responses, passwords, pubkeys, port)
            _server.all_done = threading.Event()
            worker = ThreadHandler('server', _server.serve_forever)
            # Execute function
            try:
                return func(*args, **kwargs)
            finally:
                # Clean up client side connections
                with hide('status'):
                    disconnect_all()
                # Stop server
                _server.all_done.set()
                _server.shutdown()
                # Why this is not called in shutdown() is beyond me.
                _server.server_close()
                worker.thread.join()
                # Handle subthread exceptions
                e = worker.exception
                if e:
                    raise e[0], e[1], e[2]
        return inner
    return run_server
