"""Start SmartPad on this computer.

    python run.py                 # HTTP on port 8765
    python run.py --https         # HTTPS with an auto-generated self-signed certificate
    python run.py --port 9000 --no-browser
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def ensure_cert(ips: list[str]) -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    from server.config import CERT_DIR

    CERT_DIR.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = CERT_DIR / "cert.pem", CERT_DIR / "key.pem"
    marker = CERT_DIR / "ips.txt"
    wanted = ",".join(sorted(ips))
    if cert_path.exists() and key_path.exists() and marker.exists() and marker.read_text() == wanted:
        return str(cert_path), str(key_path)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SmartPad Local")])
    san = [x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in set(ips + ["127.0.0.1"])]
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    marker.write_text(wanted)
    return str(cert_path), str(key_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="SmartPad server")
    parser.add_argument("--port", type=int, help="port (default 8765)")
    parser.add_argument("--https", action="store_true", help="serve over HTTPS with a self-signed certificate")
    parser.add_argument("--no-browser", action="store_true", help="do not open the status page on this computer")
    parser.add_argument("--no-input", action="store_true", help="do not control the real mouse (testing)")
    args = parser.parse_args()

    if args.port:
        os.environ["SMARTPAD_PORT"] = str(args.port)
    if args.https:
        os.environ["SMARTPAD_HTTPS"] = "1"
    if args.no_input:
        os.environ["SMARTPAD_INPUT_CONTROL"] = "0"

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    import uvicorn

    from server import ai
    from server.config import settings
    from server.main import app, controller, pairing_url
    from server.network import all_lan_ips, qr_terminal

    ips = all_lan_ips()
    ssl_kwargs = {}
    if settings.https:
        cert, key = ensure_cert(ips)
        ssl_kwargs = {"ssl_certfile": cert, "ssl_keyfile": key}

    url = pairing_url(ips[0])
    caps = ai.capabilities()
    print("\n" + "=" * 64)
    print("  SmartPad is running")
    print("=" * 64)
    print(qr_terminal(url))
    print("  Scan the QR code with your phone camera, or open:")
    for ip in ips:
        print(f"    {pairing_url(ip)}")
    print(f"\n  Computer status page: {'https' if settings.https else 'http'}://localhost:{settings.port}/desktop")
    print(f"  Mouse control : {'ON' if controller.enabled else 'OFF'} {controller.error or ''}")
    print(f"  AI provider   : {caps['provider']}" + (f" ({caps['model']})" if caps["model"] else " (add ANTHROPIC_API_KEY to .env for AI recognition)"))
    print("  Phone and computer must be on the same Wi-Fi network. Press Ctrl+C to stop.")
    print("=" * 64 + "\n")

    if not args.no_browser:
        local = f"{'https' if settings.https else 'http'}://localhost:{settings.port}/desktop"
        threading.Timer(1.5, lambda: webbrowser.open(local)).start()

    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning", ws_ping_interval=20, ws_ping_timeout=20, **ssl_kwargs)


if __name__ == "__main__":
    main()
