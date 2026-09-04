"""Best-effort router port mapping, so incoming BitTorrent connections arrive.

Seeding only really works if other peers can open a connection to us, and
behind the usual home router that needs a port forward. Almost nobody sets one
up by hand, so SteamFlix asks the router for one itself: UPnP first, which
essentially every consumer router speaks, then NAT-PMP for the ones that do
not.

Everything here is best effort and quiet about failing. Without a mapping
SteamFlix still uploads to every peer it connects to itself - a peer we dialled
can ask us for pieces over that same socket - it simply cannot accept new ones.
"""
import re
import socket
import struct
import subprocess
import sys
import time
from urllib.parse import urljoin

SSDP_ADDR = ("239.255.255.250", 1900)
SEARCH_TARGETS = [
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "upnp:rootdevice",
]
WAN_SERVICES = (
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
DESCRIPTION = "SteamFlix"
LEASE = 0                       # 0 means "until it is removed"
FALLBACK_LEASE = 3600           # some routers refuse a permanent mapping


def lan_ip() -> str:
    """This machine's address on the LAN, which is what the router maps to."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))       # a UDP connect sends no packet
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


# --------------------------------------------------------------------------- #
# UPnP
# --------------------------------------------------------------------------- #
def _discover(timeout=3.0):
    """Every IGD description URL that answers an SSDP search."""
    found = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.8)
    try:
        for target in SEARCH_TARGETS:
            msg = ("M-SEARCH * HTTP/1.1\r\n"
                   f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
                   'MAN: "ssdp:discover"\r\n'
                   "MX: 2\r\n"
                   f"ST: {target}\r\n\r\n").encode()
            try:
                sock.sendto(msg, SSDP_ADDR)
            except OSError:
                continue
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            hit = re.search(rb"(?im)^location:\s*(\S+)", data)
            if hit:
                url = hit.group(1).decode("ascii", "replace")
                if url not in found:
                    found.append(url)
    finally:
        sock.close()
    return found


def _control_urls(location):
    """(control url, service type) pairs for the WAN connection service."""
    import requests
    try:
        r = requests.get(location, timeout=4)
        if r.status_code >= 400:
            return []
        xml = r.text
    except Exception:  # noqa: BLE001 - anything on the LAN may answer this
        return []
    out = []
    for block in re.findall(r"<service>(.*?)</service>", xml, re.S | re.I):
        kind = re.search(r"<serviceType>(.*?)</serviceType>", block, re.I)
        path = re.search(r"<controlURL>(.*?)</controlURL>", block, re.I)
        if kind and path and kind.group(1).strip() in WAN_SERVICES:
            out.append((urljoin(location, path.group(1).strip()),
                        kind.group(1).strip()))
    return out


def _soap(control, service, action, args):
    import requests
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args)
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body>'
        "</s:Envelope>")
    r = requests.post(control, data=envelope.encode("utf-8"), timeout=6, headers={
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{service}#{action}"',
    })
    if r.status_code >= 400:
        code = re.search(r"<errorCode>(\d+)</errorCode>", r.text)
        raise OSError(f"{action} refused ({code.group(1) if code else r.status_code})")
    return r.text


def _upnp_map(port, lease=LEASE):
    ip = lan_ip()
    for location in _discover():
        for control, service in _control_urls(location):
            args = [
                ("NewRemoteHost", ""),
                ("NewExternalPort", port),
                ("NewProtocol", "TCP"),
                ("NewInternalPort", port),
                ("NewInternalClient", ip),
                ("NewEnabled", 1),
                ("NewPortMappingDescription", DESCRIPTION),
                ("NewLeaseDuration", lease),
            ]
            got = lease
            try:
                _soap(control, service, "AddPortMapping", args)
            except Exception:  # noqa: BLE001 - retry with a leased mapping
                if lease != LEASE:
                    continue
                try:
                    args[-1] = ("NewLeaseDuration", FALLBACK_LEASE)
                    _soap(control, service, "AddPortMapping", args)
                    got = FALLBACK_LEASE
                except Exception:  # noqa: BLE001
                    continue
            external = None
            try:
                reply = _soap(control, service, "GetExternalIPAddress", [])
                hit = re.search(r"<NewExternalIPAddress>(.*?)</NewExternalIPAddress>",
                                reply)
                external = hit.group(1).strip() if hit else None
            except Exception:  # noqa: BLE001 - the mapping still stands
                pass
            return {"method": "upnp", "port": port, "external_port": port,
                    "external_ip": external, "lease": got,
                    "control": control, "service": service, "internal": ip}
    return None


def _upnp_unmap(mapping):
    try:
        _soap(mapping["control"], mapping["service"], "DeletePortMapping", [
            ("NewRemoteHost", ""),
            ("NewExternalPort", mapping["external_port"]),
            ("NewProtocol", "TCP"),
        ])
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# NAT-PMP
# --------------------------------------------------------------------------- #
def gateway():
    """The default gateway, read from the routing table where possible."""
    cmd = ["route", "print", "-4"] if sys.platform == "win32" else ["netstat", "-rn"]
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=8,
                             **kw).stdout
    except Exception:  # noqa: BLE001
        out = ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] in ("0.0.0.0", "default"):
            for token in parts[1:]:
                if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", token) and token != "0.0.0.0":
                    return token
    # Last guess: the router is nearly always .1 on the local subnet.
    ip = lan_ip().rsplit(".", 1)
    return ip[0] + ".1" if len(ip) == 2 else None


def _natpmp_map(port, lifetime=FALLBACK_LEASE):
    gw = gateway()
    if not gw:
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    try:
        # version 0, opcode 2 (map TCP), reserved, internal, suggested, lifetime
        sock.sendto(struct.pack(">BBHHHI", 0, 2, 0, port, port, lifetime), (gw, 5351))
        data, _ = sock.recvfrom(64)
        if len(data) < 16:
            return None
        _, op, result, _, internal, external, lease = struct.unpack(">BBHIHHI",
                                                                   data[:16])
        if op != 130 or result != 0 or internal != port:
            return None
        return {"method": "natpmp", "port": port, "external_port": external,
                "external_ip": None, "lease": lease, "gateway": gw}
    except Exception:  # noqa: BLE001
        return None
    finally:
        sock.close()


def _natpmp_unmap(mapping):
    gw = mapping.get("gateway") or gateway()
    if not gw:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    try:                                     # lifetime 0 removes the mapping
        sock.sendto(struct.pack(">BBHHHI", 0, 2, 0, mapping["port"], 0, 0), (gw, 5351))
        sock.recvfrom(64)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        sock.close()


# --------------------------------------------------------------------------- #
def open_port(port):
    """Ask the router to forward ``port`` here. None if it would not."""
    try:
        mapping = _upnp_map(port)
    except Exception:  # noqa: BLE001
        mapping = None
    return mapping or _natpmp_map(port)


def refresh(mapping):
    """Re-assert a mapping; a leased one expires otherwise."""
    if not mapping:
        return None
    if mapping["method"] == "natpmp":
        return _natpmp_map(mapping["port"]) or mapping
    if mapping.get("lease"):
        try:
            return _upnp_map(mapping["port"], mapping["lease"]) or mapping
        except Exception:  # noqa: BLE001
            return mapping
    return mapping


def close_port(mapping):
    if not mapping:
        return False
    if mapping["method"] == "natpmp":
        return _natpmp_unmap(mapping)
    return _upnp_unmap(mapping)
