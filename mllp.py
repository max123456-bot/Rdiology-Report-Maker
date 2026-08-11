"""
HL7 over the wire: MLLP, both directions.

MLLP (Minimal Lower Layer Protocol) is how hospital integration engines
actually move HL7 v2 - one TCP connection, each message framed as

    <VT=0x0B> message <FS=0x1C> <CR=0x0D>

Two capabilities:

  send_oru()      Push a finalised report (interop.hl7_oru) to the RIS/EHR's
                  MLLP port and wait for the ACK. The report is "sent" only
                  when the receiver answered AA - a socket write is not an
                  acknowledgement.

  OrderListener   Accept ORM^O01 order messages from the HIS. Each order
                  becomes a draft worklist record (records-table shape), so
                  the radiologist finds the patient waiting instead of
                  typing demographics. Every message is ACKed: AA when the
                  order was stored, AE when it could not be parsed - an
                  integration engine treats silence as failure and retries
                  forever.

Like the DICOM receiver: the listener needs an open TCP port, so it is for
the clinic-LAN / on-prem install, not for platforms that only proxy HTTPS.
Both directions are proven on loopback in enterprise_check.py.
"""

from __future__ import annotations

import socket
import socketserver
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

VT = b"\x0b"   # start of block
FS = b"\x1c"   # end of block
CR = b"\x0d"

TIMEOUT = 15
MAX_MESSAGE = 1_000_000  # 1 MB of HL7 is not a message, it is an attack


def frame(message: str) -> bytes:
    return VT + message.encode("utf-8") + FS + CR


def unframe(buffer: bytes) -> tuple[str | None, bytes]:
    """(first complete message, remaining buffer). (None, buffer) when incomplete."""
    start = buffer.find(VT)
    if start == -1:
        return None, buffer[-2:]  # keep a possible partial trailer
    end = buffer.find(FS, start)
    if end == -1:
        if len(buffer) > MAX_MESSAGE:
            raise ValueError("MLLP frame exceeds the size limit.")
        return None, buffer
    message = buffer[start + 1:end].decode("utf-8", "replace")
    rest = buffer[end + 1:]
    if rest[:1] == CR:
        rest = rest[1:]
    return message, rest


# --------------------------------------------------------------------------- #
# Small HL7 helpers (hand-rolled, same policy as interop.py: no new deps)
# --------------------------------------------------------------------------- #


def segments_of(message: str) -> list[list[str]]:
    return [seg.split("|") for seg in message.replace("\n", "\r").split("\r")
            if seg.strip()]


def _segment(segments: list[list[str]], name: str) -> list[str]:
    return next((s for s in segments if s and s[0] == name), [])


def _field(segment: list[str], index: int) -> str:
    return segment[index] if len(segment) > index else ""


def message_type(message: str) -> str:
    """'ORM^O01' out of MSH-9, however many components it carries."""
    msh = _segment(segments_of(message), "MSH")
    parts = _field(msh, 8).split("^")
    return "^".join(parts[:2])


def control_id(message: str) -> str:
    return _field(_segment(segments_of(message), "MSH"), 9)


def build_ack(message: str, code: str = "AA", text: str = "") -> str:
    """An ACK for the given message. code: AA accepted | AE error | AR rejected."""
    now = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    original = control_id(message) or "UNKNOWN"
    msa = f"MSA|{code}|{original}"
    if text:
        safe = text.replace("|", " ").replace("\r", " ")[:180]
        msa += f"|{safe}"
    return (f"MSH|^~\\&|HCFORMAT|CLINIC|||{now}||ACK|{original}-ACK|P|2.5\r"
            + msa + "\r")


def orm_to_record(message: str) -> dict:
    """
    An ORM^O01 order to a draft worklist record (records.py shape).

    The record has no report text yet - it is the study WAITING to be
    reported, which is exactly what a worklist item is.
    """
    import records

    segments = segments_of(message)
    pid = _segment(segments, "PID")
    obr = _segment(segments, "OBR")

    name_raw = _field(pid, 5)
    parts = name_raw.split("^")
    patient = " ".join(p for p in (parts[1] if len(parts) > 1 else "",
                                   parts[0] if parts else "") if p).strip()
    sex = _field(pid, 8).strip()
    study_raw = _field(obr, 4)
    study = study_raw.split("^")[1] if "^" in study_raw else study_raw
    referrer_raw = _field(obr, 16)
    referrer = " ".join(p for p in referrer_raw.split("^")[1:3] if p).strip()

    record = records.new_record("", source="hl7-order")
    record.update({
        "patient": patient,
        "age_sex": sex,
        "referrer": referrer,
        "study": study.strip(),
        "modality": records.modality_of(study),
        "patient_key": records.patient_key(patient, sex),
        "order_control_id": control_id(message),
    })
    return record


# --------------------------------------------------------------------------- #
# Outbound: push an ORU and wait for the ACK
# --------------------------------------------------------------------------- #


@dataclass
class MllpResult:
    ok: bool
    detail: str
    ack: str = ""


def send_message(host: str, port: int, message: str,
                 timeout: int = TIMEOUT) -> MllpResult:
    """One framed message out, one ACK back. Never raises - reports honestly."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            sock.sendall(frame(message))
            sock.settimeout(timeout)
            buffer = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    return MllpResult(False, "The receiver closed the connection "
                                             "without acknowledging.", "")
                buffer += chunk
                ack, _ = unframe(buffer)
                if ack is not None:
                    break
    except Exception as exc:
        return MllpResult(False, f"{type(exc).__name__}: {exc}", "")

    msa = _segment(segments_of(ack), "MSA")
    code = _field(msa, 1)
    if code == "AA":
        return MllpResult(True, "Accepted (MSA|AA).", ack)
    return MllpResult(False, f"The receiver answered {code or 'nothing'} - "
                             f"{_field(msa, 3) or 'no reason given'}.", ack)


def send_oru(host: str, port: int, record: dict, facility: str = "") -> MllpResult:
    """The finished report, as ORU^R01, to the RIS. ACK-gated."""
    import interop

    return send_message(host, port, interop.hl7_oru(record, facility=facility))


# --------------------------------------------------------------------------- #
# Inbound: the order listener
# --------------------------------------------------------------------------- #


@dataclass
class OrderListener:
    port: int
    server: socketserver.ThreadingTCPServer = field(default=None, repr=False)
    received: int = 0
    errors: int = 0

    def stop(self) -> None:
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass


_listener: OrderListener | None = None
_listener_lock = threading.Lock()


def start_order_listener(port: int, tenant: str | None = None) -> OrderListener:
    """
    Listen for ORM^O01 on `port`. Each order becomes a draft worklist record
    for `tenant` (current tenant when omitted). One listener per process.
    """
    global _listener
    with _listener_lock:
        if _listener is not None:
            raise RuntimeError(
                f"An order listener is already running on port {_listener.port}. "
                "Stop it first."
            )

        import storage

        resolved_tenant = tenant or storage.current_tenant()
        listener = OrderListener(port=int(port))

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                buffer = b""
                self.request.settimeout(TIMEOUT)
                while True:
                    try:
                        chunk = self.request.recv(65536)
                    except Exception:
                        return
                    if not chunk:
                        return
                    buffer += chunk
                    while True:
                        try:
                            message, buffer = unframe(buffer)
                        except ValueError:
                            return  # oversized frame - drop the connection
                        if message is None:
                            break
                        self._respond(message)

            def _respond(self, message: str):
                kind = message_type(message)
                if kind.startswith("ORM"):
                    try:
                        record = orm_to_record(message)
                        storage.get_store().save_report(resolved_tenant, record)
                        storage.log("order.received",
                                    record.get("study") or record.get("id", ""),
                                    detail=record.get("patient", ""))
                        listener.received += 1
                        ack = build_ack(message, "AA")
                    except Exception as exc:
                        listener.errors += 1
                        ack = build_ack(message, "AE", str(exc))
                else:
                    # Politely accept anything else (ADT chatter etc.) without
                    # creating records - AR would make the engine retry forever.
                    ack = build_ack(message, "AA")
                try:
                    self.request.sendall(frame(ack))
                except Exception:
                    pass

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        listener.server = Server(("0.0.0.0", int(port)), Handler)
        threading.Thread(target=listener.server.serve_forever,
                         name="hcformat-mllp", daemon=True).start()
        _listener = listener
        return listener


def stop_order_listener() -> None:
    global _listener
    with _listener_lock:
        if _listener is not None:
            _listener.stop()
            _listener = None


def listener_running() -> OrderListener | None:
    return _listener


def mllp_config() -> dict | None:
    """MLLP_HOST/MLLP_PORT secrets - where finished ORUs get pushed."""
    import os

    host = ""
    port = ""
    try:
        import streamlit as st

        host = str(st.secrets.get("MLLP_HOST", "")).strip()
        port = str(st.secrets.get("MLLP_PORT", "")).strip()
    except Exception:
        pass
    host = host or os.environ.get("MLLP_HOST", "").strip()
    port = port or os.environ.get("MLLP_PORT", "").strip()
    if not host or not port:
        return None
    return {"host": host, "port": int(port)}
