"""
Print the addresses other clinic PCs should use to reach this server.

Prefers the computer name, because a DHCP address changes the next time this PC
reconnects and every bookmark in the clinic then points at nothing.

    python show_address.py
"""

from __future__ import annotations

import socket

PORT = 8501


def lan_addresses() -> list[str]:
    """Every non-loopback IPv4 address this machine answers on."""
    found: set[str] = set()

    # The reliable trick: ask the OS which local address it would use to reach
    # the network. No packet is actually sent.
    for probe in ("10.255.255.255", "192.168.1.1", "8.8.8.8"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.2)
        try:
            sock.connect((probe, 1))
            found.add(sock.getsockname()[0])
        except OSError:
            pass
        finally:
            sock.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass

    usable = [
        ip for ip in found
        if not ip.startswith("127.") and not ip.startswith("169.254.")
    ]
    # Private ranges first - those are the ones a clinic PC can actually reach.
    usable.sort(key=lambda ip: (not ip.startswith(("192.168.", "10.", "172.")), ip))
    return usable


def main() -> None:
    host = socket.gethostname()
    print(f"     http://{host}:{PORT}          <- use this one, it survives a reboot")
    for ip in lan_addresses():
        print(f"     http://{ip}:{PORT}")
    if not lan_addresses():
        print("     (no network address found - is this PC connected to the clinic network?)")


if __name__ == "__main__":
    main()
