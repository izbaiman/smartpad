"""Network helpers: find the LAN address the phone should connect to, build QR codes."""
from __future__ import annotations

import io
import socket

import qrcode
import qrcode.image.svg


def lan_ip() -> str:
    """Best-effort LAN IPv4 of this computer (the interface used for the default route)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packets are sent for UDP connect(); it just selects the outgoing interface.
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def all_lan_ips() -> list[str]:
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except socket.gaierror:
        pass
    primary = lan_ip()
    ordered = [primary] + sorted(ips - {primary})
    return [ip for ip in ordered if ip != "127.0.0.1"] or ["127.0.0.1"]


def qr_svg(data: str) -> str:
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


def qr_terminal(data: str) -> str:
    qr = qrcode.QRCode(border=1)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue()
