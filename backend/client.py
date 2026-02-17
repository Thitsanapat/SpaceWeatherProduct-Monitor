# backend/client.py
import socket
import base64


class NTRIPClient:
    """
    Simple NTRIP v1 client (GET mountpoint + Basic Auth)
    Returns a connected TCP socket streaming RTCM bytes.
    """

    def __init__(self, caster_ip: str, caster_port: int, mountpoint: str,
                 username: str, password: str, timeout: int = 10):
        self.caster_ip = caster_ip
        self.caster_port = int(caster_port)
        self.mountpoint = mountpoint.lstrip("/")  # safe
        self.username = username
        self.password = password
        self.timeout = timeout

    def connect_to_ntrip(self):
        """
        Connect to caster and return socket if HTTP 200 OK.
        Raise RuntimeError if auth/response is not OK.
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect((self.caster_ip, self.caster_port))
        s.settimeout(None)

        cred = base64.b64encode(f"{self.username}:{self.password}".encode("ascii")).decode("ascii")
        req = (
            f"GET /{self.mountpoint} HTTP/1.0\r\n"
            f"User-Agent: NTRIP PythonClient/1.0\r\n"
            f"Authorization: Basic {cred}\r\n"
            f"Accept: */*\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("utf-8")

        s.sendall(req)

        # read response header
        resp = s.recv(1024)
        if b"200 OK" not in resp:
            s.close()
            raise RuntimeError(f"NTRIP connect failed. Response: {resp!r}")

        print("✅ Successfully connected to NTRIP caster")
        return s


class TCPclient:
    """
    Simple TCP client (non-NTRIP). Kept for compatibility with your old code.
    """
    def __init__(self, caster_ip: str, caster_port: int, timeout: int = 10):
        self.caster_ip = caster_ip
        self.caster_port = int(caster_port)
        self.timeout = timeout

    def connect_to_TCP(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect((self.caster_ip, self.caster_port))
        s.settimeout(None)
        return s
