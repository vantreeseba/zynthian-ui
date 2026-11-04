"""Minimal stub of pyliblo3 for desktop UI testing without OSC engines.

Provides the symbols zynthian-ui touches; all sends are no-ops.
"""
import logging

UDP = 1
TCP = 2
UNIX = 3


class AddressError(Exception):
    pass


class Address:
    def __init__(self, *args, **kwargs):
        self.args = args

    @property
    def url(self):
        return "osc.udp://stub:0/"

    @property
    def port(self):
        return self.args[-1] if self.args else 0


def send(target, path, *args):
    logging.debug(f"liblo-stub send {path} {args}")


class Server:
    def __init__(self, port=None, proto=UDP, **kwargs):
        self.port = port or 0

    def add_method(self, *args, **kwargs):
        pass

    def recv(self, timeout=0):
        return False

    def send(self, target, path, *args):
        pass

    def free(self):
        pass

    @property
    def url(self):
        return "osc.udp://stub:0/"


class ServerThread(Server):
    def __init__(self, port=None, proto=UDP, **kwargs):
        super().__init__(port, proto, **kwargs)

    def start(self):
        pass

    def stop(self):
        pass


class ServerError(Exception):
    pass


def make_method(path=None, types=None):
    def decorator(f):
        return f
    return decorator
