"""
PACS connectivity - the last mile between this system and the scanners.

Two independent paths, use whichever the clinic has:

  * Orthanc REST client - the clinic runs an Orthanc DICOM server (free,
    one .exe); this app talks to it over HTTPS. Works from Streamlit Cloud
    too, because the app dials out - nothing needs to reach the app.
    Configure with ORTHANC_URL (+ ORTHANC_USERNAME / ORTHANC_PASSWORD).

  * pynetdicom - native DICOM networking from Python:
      - C-ECHO   verify a PACS answers  (the DICOM "ping")
      - C-FIND   query studies on a PACS by patient name / date
      - C-STORE receiver - modalities push studies directly to this machine;
        each file is spooled to disk and indexed for the Worklist tab.
    The receiver needs an open TCP port, so it is for the clinic-LAN install,
    not Streamlit Cloud - the UI says so instead of pretending.

Everything degrades honestly: no ORTHANC_URL means the Orthanc panel shows
setup instructions; pynetdicom not installed means the receiver panel says
`pip install pynetdicom`. Nothing here interprets images - pixels go to the
AI pre-read only when the user clicks, and metadata goes to dicom_meta's
cross-checks.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SPOOL_DIR = os.path.join(HERE, "incoming_dicom")
TIMEOUT = 10
DEFAULT_AET = "HCFORMAT"

VERIFICATION_SOP = "1.2.840.10008.1.1"

try:
    import pynetdicom  # noqa: F401

    PYNETDICOM_OK = True
    PYNETDICOM_ERROR = ""
except ImportError as _exc:
    PYNETDICOM_OK = False
    PYNETDICOM_ERROR = str(_exc)


def _secret(name: str) -> str:
    try:
        import streamlit as st

        if name in st.secrets:
            return str(st.secrets[name]).strip()
    except Exception:
        pass
    return os.environ.get(name, "").strip()


# --------------------------------------------------------------------------- #
# Orthanc REST client
# --------------------------------------------------------------------------- #


def orthanc_config() -> dict | None:
    url = _secret("ORTHANC_URL").rstrip("/")
    if not url:
        return None
    return {
        "url": url,
        "username": _secret("ORTHANC_USERNAME"),
        "password": _secret("ORTHANC_PASSWORD"),
    }


def _orthanc_request(path: str, raw: bool = False):
    config = orthanc_config()
    if not config:
        raise RuntimeError("Orthanc is not configured - set ORTHANC_URL in secrets.")
    request = urllib.request.Request(config["url"] + path)
    if config["username"]:
        credentials = f"{config['username']}:{config['password']}".encode()
        request.add_header("Authorization",
                           f"Basic {base64.b64encode(credentials).decode()}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Orthanc refused {path}: HTTP {exc.code}") from exc
    except Exception as exc:
        raise RuntimeError(f"Could not reach Orthanc at {config['url']}: {exc}") from exc
    if raw:
        return body
    return json.loads(body.decode("utf-8", "replace"))


def orthanc_system() -> dict:
    """/system - proves the URL and credentials work, returns the version."""
    return _orthanc_request("/system")


def orthanc_recent_studies(limit: int = 15) -> list[dict]:
    """
    The newest studies on the server, one flat dict each:
    {id, patient, sex, birth_date, study_date, description, modalities, instances}
    """
    ids = _orthanc_request("/studies")
    out: list[dict] = []
    for study_id in list(ids)[-limit:][::-1]:
        try:
            study = _orthanc_request(f"/studies/{study_id}")
        except RuntimeError:
            continue
        main = study.get("MainDicomTags", {})
        patient = study.get("PatientMainDicomTags", {})
        out.append({
            "id": study_id,
            "patient": str(patient.get("PatientName", "")).replace("^", " ").strip(),
            "sex": patient.get("PatientSex", ""),
            "birth_date": patient.get("PatientBirthDate", ""),
            "study_date": main.get("StudyDate", ""),
            "description": main.get("StudyDescription", ""),
            "series": study.get("Series", []),
        })
    return out


def orthanc_first_instance(study_id: str) -> str:
    """The instance id to fetch for a preview / metadata check."""
    study = _orthanc_request(f"/studies/{study_id}")
    for series_id in study.get("Series", []):
        series = _orthanc_request(f"/series/{series_id}")
        instances = series.get("Instances", [])
        if instances:
            return instances[0]
    raise RuntimeError("This study has no instances.")


def orthanc_instance_file(instance_id: str) -> bytes:
    """The raw .dcm - feed it to dicom_meta.read_meta / cross_check."""
    return _orthanc_request(f"/instances/{instance_id}/file", raw=True)


def orthanc_preview_png(instance_id: str) -> bytes:
    """
    A rendered PNG of the instance, windowed by Orthanc itself - ready for
    the AI pre-read without any pixel handling on our side.
    """
    return _orthanc_request(f"/instances/{instance_id}/preview", raw=True)


# --------------------------------------------------------------------------- #
# pynetdicom - SCU side (we call a PACS)
# --------------------------------------------------------------------------- #


def echo(host: str, port: int, called_aet: str = "ANY-SCP",
         calling_aet: str = DEFAULT_AET) -> tuple[bool, str]:
    """C-ECHO: does the PACS at host:port answer? The DICOM ping."""
    if not PYNETDICOM_OK:
        return False, "pynetdicom is not installed: pip install pynetdicom"
    from pynetdicom import AE

    ae = AE(ae_title=calling_aet)
    ae.add_requested_context(VERIFICATION_SOP)
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = TIMEOUT
    try:
        assoc = ae.associate(host, int(port), ae_title=called_aet)
    except Exception as exc:
        return False, f"Could not associate: {exc}"
    if not assoc.is_established:
        return False, "The PACS rejected the association - check host, port and AE titles."
    try:
        status = assoc.send_c_echo()
        if status and status.Status == 0x0000:
            return True, f"{called_aet}@{host}:{port} answered C-ECHO."
        return False, f"C-ECHO returned status {getattr(status, 'Status', None)}."
    finally:
        assoc.release()


def find_studies(host: str, port: int, called_aet: str = "ANY-SCP",
                 calling_aet: str = DEFAULT_AET, patient_name: str = "",
                 study_date: str = "") -> list[dict]:
    """
    C-FIND at study level. patient_name takes DICOM wildcards ("SHARMA*"),
    study_date takes YYYYMMDD or a YYYYMMDD-YYYYMMDD range.
    Raises RuntimeError with a plain-language reason on any failure.
    """
    if not PYNETDICOM_OK:
        raise RuntimeError("pynetdicom is not installed: pip install pynetdicom")
    from pydicom.dataset import Dataset
    from pynetdicom import AE
    from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelFind

    ae = AE(ae_title=calling_aet)
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = TIMEOUT

    query = Dataset()
    query.QueryRetrieveLevel = "STUDY"
    query.PatientName = patient_name or "*"
    query.StudyDate = study_date or ""
    query.PatientSex = ""
    query.StudyInstanceUID = ""
    query.StudyDescription = ""
    query.ModalitiesInStudy = ""

    try:
        assoc = ae.associate(host, int(port), ae_title=called_aet)
    except Exception as exc:
        raise RuntimeError(f"Could not associate with the PACS: {exc}") from exc
    if not assoc.is_established:
        raise RuntimeError("The PACS rejected the association - check host, port "
                           "and AE titles.")
    results: list[dict] = []
    try:
        for status, identifier in assoc.send_c_find(
            query, StudyRootQueryRetrieveInformationModelFind
        ):
            if status is None:
                raise RuntimeError("The connection dropped mid-query.")
            if status.Status in (0xFF00, 0xFF01) and identifier is not None:
                results.append({
                    "patient": str(getattr(identifier, "PatientName", "")).replace("^", " "),
                    "sex": str(getattr(identifier, "PatientSex", "")),
                    "study_date": str(getattr(identifier, "StudyDate", "")),
                    "description": str(getattr(identifier, "StudyDescription", "")),
                    "modalities": str(getattr(identifier, "ModalitiesInStudy", "")),
                    "study_uid": str(getattr(identifier, "StudyInstanceUID", "")),
                })
    finally:
        assoc.release()
    return results


def mwl_query(host: str, port: int, called_aet: str = "ANY-SCP",
              calling_aet: str = DEFAULT_AET, modality: str = "",
              date: str = "") -> list[dict]:
    """
    Modality Worklist query: what is SCHEDULED, so the radiologist picks the
    patient instead of typing demographics. `date` is DICOM YYYYMMDD (empty =
    the server's default, usually today). Raises RuntimeError with a plain
    reason on failure.
    """
    if not PYNETDICOM_OK:
        raise RuntimeError("pynetdicom is not installed: pip install pynetdicom")
    from pydicom.dataset import Dataset
    from pynetdicom import AE
    from pynetdicom.sop_class import ModalityWorklistInformationFind

    ae = AE(ae_title=calling_aet)
    ae.add_requested_context(ModalityWorklistInformationFind)
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = TIMEOUT

    step = Dataset()
    step.Modality = modality or ""
    step.ScheduledProcedureStepStartDate = date or ""
    step.ScheduledProcedureStepDescription = ""
    query = Dataset()
    query.PatientName = ""
    query.PatientID = ""
    query.PatientSex = ""
    query.PatientBirthDate = ""
    query.AccessionNumber = ""
    query.ReferringPhysicianName = ""
    query.RequestedProcedureDescription = ""
    query.ScheduledProcedureStepSequence = [step]

    try:
        assoc = ae.associate(host, int(port), ae_title=called_aet)
    except Exception as exc:
        raise RuntimeError(f"Could not associate with the worklist SCP: {exc}") from exc
    if not assoc.is_established:
        raise RuntimeError("The worklist SCP rejected the association - check "
                           "host, port and AE titles.")
    results: list[dict] = []
    try:
        for status, identifier in assoc.send_c_find(
            query, ModalityWorklistInformationFind
        ):
            if status is None:
                raise RuntimeError("The connection dropped mid-query.")
            if status.Status in (0xFF00, 0xFF01) and identifier is not None:
                sps = (identifier.ScheduledProcedureStepSequence[0]
                       if getattr(identifier, "ScheduledProcedureStepSequence", None)
                       else None)
                results.append({
                    "patient": str(getattr(identifier, "PatientName", "")).replace("^", " "),
                    "patient_id": str(getattr(identifier, "PatientID", "")),
                    "sex": str(getattr(identifier, "PatientSex", "")),
                    "birth_date": str(getattr(identifier, "PatientBirthDate", "")),
                    "accession": str(getattr(identifier, "AccessionNumber", "")),
                    "referrer": str(getattr(identifier, "ReferringPhysicianName", "")).replace("^", " "),
                    "procedure": str(getattr(identifier, "RequestedProcedureDescription", "")),
                    "modality": str(getattr(sps, "Modality", "")) if sps else "",
                    "scheduled": str(getattr(sps, "ScheduledProcedureStepStartDate", "")) if sps else "",
                })
    finally:
        assoc.release()
    return results


# --------------------------------------------------------------------------- #
# DICOMweb - QIDO-RS / WADO-RS over plain HTTPS
# --------------------------------------------------------------------------- #

# DICOM tag keywords in QIDO JSON responses.
_Q = {
    "patient": "00100010", "patient_id": "00100020", "sex": "00100040",
    "study_date": "00080020", "description": "00081030",
    "modalities": "00080061", "study_uid": "0020000D",
}


def dicomweb_config() -> dict | None:
    """DICOMWEB_URL secret - e.g. an Orthanc's /dicom-web root."""
    url = _secret("DICOMWEB_URL").rstrip("/")
    if not url:
        return None
    return {
        "url": url,
        "username": _secret("DICOMWEB_USERNAME") or _secret("ORTHANC_USERNAME"),
        "password": _secret("DICOMWEB_PASSWORD") or _secret("ORTHANC_PASSWORD"),
    }


def _dicomweb_request(path: str, accept: str) -> bytes:
    config = dicomweb_config()
    if not config:
        raise RuntimeError("DICOMweb is not configured - set DICOMWEB_URL in secrets.")
    request = urllib.request.Request(config["url"] + path)
    request.add_header("Accept", accept)
    if config["username"]:
        credentials = f"{config['username']}:{config['password']}".encode()
        request.add_header("Authorization",
                           f"Basic {base64.b64encode(credentials).decode()}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"DICOMweb refused {path}: HTTP {exc.code}") from exc
    except Exception as exc:
        raise RuntimeError(f"Could not reach DICOMweb at {config['url']}: {exc}") from exc


def _qido_value(entry: dict, tag: str) -> str:
    values = (entry.get(tag) or {}).get("Value") or []
    if not values:
        return ""
    first = values[0]
    if isinstance(first, dict):  # PN values: {"Alphabetic": "DEVI^SUNITA"}
        return str(first.get("Alphabetic", "")).replace("^", " ")
    return str(first)


def qido_studies(limit: int = 15, patient_name: str = "") -> list[dict]:
    """QIDO-RS study search - the standards-based way to list studies."""
    path = f"/studies?limit={int(limit)}&includefield=00081030"
    if patient_name:
        path += "&PatientName=" + urllib.parse.quote(patient_name)
    body = _dicomweb_request(path, "application/dicom+json")
    entries = json.loads(body.decode("utf-8", "replace") or "[]")
    return [
        {name: _qido_value(entry, tag) for name, tag in _Q.items()}
        for entry in entries if isinstance(entry, dict)
    ]


def wado_instance(study_uid: str, series_uid: str, instance_uid: str) -> bytes:
    """
    WADO-RS: one instance's .dcm bytes. The response is multipart/related;
    the DICOM part is extracted here so callers just get the file.
    """
    path = (f"/studies/{urllib.parse.quote(study_uid)}"
            f"/series/{urllib.parse.quote(series_uid)}"
            f"/instances/{urllib.parse.quote(instance_uid)}")
    body = _dicomweb_request(
        path, 'multipart/related; type="application/dicom"')
    return _first_multipart_part(body)


def _first_multipart_part(body: bytes) -> bytes:
    """The payload of the first part of a multipart/related response."""
    if not body.startswith(b"--"):
        return body  # some servers answer single-part; take it as-is
    newline = body.find(b"\r\n")
    boundary = body[:newline]
    part = body.split(boundary)[1]
    header_end = part.find(b"\r\n\r\n")
    if header_end == -1:
        raise RuntimeError("Malformed multipart response from WADO-RS.")
    payload = part[header_end + 4:]
    return payload.rstrip(b"\r\n-")


# --------------------------------------------------------------------------- #
# pynetdicom - SCP side (modalities push to us)
# --------------------------------------------------------------------------- #


@dataclass
class Receiver:
    """One running C-STORE listener. Module-level singleton via start_receiver."""

    aet: str
    port: int
    spool: str
    server: object = field(default=None, repr=False)
    received: int = 0

    def stop(self) -> None:
        try:
            self.server.shutdown()
        except Exception:
            pass


_receiver: Receiver | None = None
_receiver_lock = threading.Lock()


def _index_path(spool: str) -> str:
    return os.path.join(spool, "index.jsonl")


def _safe_uid(uid: str) -> str:
    return re.sub(r"[^0-9.]", "", str(uid))[:64] or "unknown"


def start_receiver(port: int, aet: str = DEFAULT_AET,
                   spool: str = SPOOL_DIR) -> Receiver:
    """
    Listen for C-STORE on `port`. Each received instance is written to
    spool/<StudyUID>/<SOPUID>.dcm and indexed in spool/index.jsonl with the
    metadata the Worklist needs. Returns the running Receiver.
    """
    global _receiver
    if not PYNETDICOM_OK:
        raise RuntimeError("pynetdicom is not installed: pip install pynetdicom")
    from pynetdicom import AE, AllStoragePresentationContexts, evt

    with _receiver_lock:
        if _receiver is not None:
            raise RuntimeError(
                f"A receiver is already listening as {_receiver.aet} on port "
                f"{_receiver.port}. Stop it first."
            )

        os.makedirs(spool, exist_ok=True)
        receiver = Receiver(aet=aet, port=int(port), spool=spool)

        def handle_store(event):
            try:
                ds = event.dataset
                ds.file_meta = event.file_meta
                study_uid = _safe_uid(getattr(ds, "StudyInstanceUID", "unknown"))
                sop_uid = _safe_uid(getattr(ds, "SOPInstanceUID", "unknown"))
                folder = os.path.join(spool, study_uid)
                os.makedirs(folder, exist_ok=True)
                path = os.path.join(folder, f"{sop_uid}.dcm")
                ds.save_as(path, write_like_original=False)
                entry = {
                    "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "study_uid": study_uid,
                    "sop_uid": sop_uid,
                    "patient": str(getattr(ds, "PatientName", "")).replace("^", " "),
                    "sex": str(getattr(ds, "PatientSex", "")),
                    "age": str(getattr(ds, "PatientAge", "")),
                    "modality": str(getattr(ds, "Modality", "")),
                    "description": str(getattr(ds, "StudyDescription", "")),
                    "path": path,
                }
                with open(_index_path(spool), "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                receiver.received += 1
                return 0x0000          # success
            except Exception:
                return 0xC001          # cannot understand / processing failure

        ae = AE(ae_title=aet)
        ae.supported_contexts = AllStoragePresentationContexts
        ae.add_supported_context(VERIFICATION_SOP)
        receiver.server = ae.start_server(
            ("0.0.0.0", int(port)), block=False,
            evt_handlers=[(evt.EVT_C_STORE, handle_store)],
        )
        _receiver = receiver
        return receiver


def stop_receiver() -> None:
    global _receiver
    with _receiver_lock:
        if _receiver is not None:
            _receiver.stop()
            _receiver = None


def receiver_running() -> Receiver | None:
    return _receiver


def received_studies(spool: str = SPOOL_DIR) -> list[dict]:
    """
    What has been pushed to us, grouped by study, newest first:
    {study_uid, patient, sex, age, modality, description, when, instances: [paths]}
    """
    index = _index_path(spool)
    if not os.path.exists(index):
        return []
    studies: dict[str, dict] = {}
    try:
        with open(index, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    for line in lines:
        try:
            entry = json.loads(line)
        except Exception:
            continue
        uid = entry.get("study_uid") or "unknown"
        study = studies.setdefault(uid, {
            "study_uid": uid, "patient": entry.get("patient", ""),
            "sex": entry.get("sex", ""), "age": entry.get("age", ""),
            "modality": entry.get("modality", ""),
            "description": entry.get("description", ""),
            "when": entry.get("when", ""), "instances": [],
        })
        if entry.get("path") and os.path.exists(entry["path"]):
            study["instances"].append(entry["path"])
        study["when"] = max(study["when"], entry.get("when", ""))
    rows = [s for s in studies.values() if s["instances"]]
    rows.sort(key=lambda s: s["when"], reverse=True)
    return rows


def clear_received(study_uid: str, spool: str = SPOOL_DIR) -> None:
    """Drop one received study from the spool and the index."""
    import shutil

    folder = os.path.join(spool, _safe_uid(study_uid))
    shutil.rmtree(folder, ignore_errors=True)
    index = _index_path(spool)
    if not os.path.exists(index):
        return
    try:
        with open(index, encoding="utf-8") as fh:
            lines = fh.readlines()
        kept = [ln for ln in lines
                if json.loads(ln).get("study_uid") != _safe_uid(study_uid)]
        with open(index, "w", encoding="utf-8") as fh:
            fh.writelines(kept)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Pixels -> PNG, for the AI pre-read of a received .dcm
# --------------------------------------------------------------------------- #


def to_png(dcm_bytes: bytes) -> bytes:
    """
    Render the first frame of a DICOM file as an 8-bit PNG.

    Uses pydicom + numpy + Pillow (all already present under Streamlit).
    VOI LUT / windowing is applied when the file carries one, so a CT slice
    is not just a grey smear. Raises ValueError with the reason otherwise.
    """
    try:
        import numpy as np
        import pydicom
        from PIL import Image
    except ImportError as exc:
        raise ValueError(
            "Rendering DICOM pixels needs pydicom, numpy and Pillow: "
            "pip install pydicom numpy pillow"
        ) from exc

    try:
        ds = pydicom.dcmread(io.BytesIO(dcm_bytes))
        array = ds.pixel_array
    except Exception as exc:
        raise ValueError(f"Could not decode the pixel data: {exc}") from exc

    try:
        from pydicom.pixel_data_handlers.util import apply_voi_lut

        array = apply_voi_lut(array, ds)
    except Exception:
        pass  # no VOI LUT - min/max scaling below still gives a readable image

    array = array.astype("float32")
    if array.ndim > 2:
        array = array[0] if array.shape[0] < array.shape[-1] else array[..., 0]
    low, high = float(array.min()), float(array.max())
    if high <= low:
        raise ValueError("The image is a single flat value - nothing to render.")
    scaled = ((array - low) / (high - low) * 255.0).clip(0, 255).astype("uint8")
    if str(getattr(ds, "PhotometricInterpretation", "")) == "MONOCHROME1":
        scaled = 255 - scaled  # MONOCHROME1 is inverted

    buffer = io.BytesIO()
    Image.fromarray(scaled).save(buffer, format="PNG")
    return buffer.getvalue()
