"""Network tripwire for synthetic malformed-config subprocess tests only."""
import socket


def denied(*args, **kwargs):
    raise RuntimeError("OFFLINE_ORACLE_NETWORK_FORBIDDEN")


socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
