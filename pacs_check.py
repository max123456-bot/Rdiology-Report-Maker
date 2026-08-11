"""
Offline checks for PACS connectivity - all on the loopback interface.

A real C-STORE receiver is started on 127.0.0.1, C-ECHO'd, and sent a real
synthetic CT instance with pynetdicom acting as the modality. No external
PACS, no network beyond localhost.

    python pacs_check.py

Needs pynetdicom + pydicom + numpy (numpy ships with Streamlit).
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile

import pacs

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --------------------------------------------------------------------------- #
print("\nconfiguration honesty")
# --------------------------------------------------------------------------- #

check(pacs.orthanc_config() is None, "no ORTHANC_URL means no Orthanc config")
try:
    pacs.orthanc_system()
    check(False, "an unconfigured Orthanc call did not raise")
except RuntimeError as exc:
    check("not configured" in str(exc), "an unconfigured Orthanc call says why")

os.environ["ORTHANC_URL"] = "http://127.0.0.1:1"
try:
    pacs.orthanc_system()
    check(False, "a dead Orthanc URL did not raise")
except RuntimeError as exc:
    check("Could not reach Orthanc" in str(exc), "a dead Orthanc URL fails plainly")
finally:
    os.environ.pop("ORTHANC_URL", None)

ok, detail = pacs.echo("127.0.0.1", free_port(), "NOBODY")
check(not ok, f"C-ECHO against a closed port fails honestly ({detail[:60]})")

if not pacs.PYNETDICOM_OK:
    print("\npynetdicom is not installed - the receiver tests cannot run.")
    print("Install it:  pip install pynetdicom")
    sys.exit(1)


# --------------------------------------------------------------------------- #
print("\na synthetic CT instance")
# --------------------------------------------------------------------------- #

import numpy as np  # noqa: E402
import pydicom  # noqa: E402
from pydicom.dataset import Dataset, FileMetaDataset  # noqa: E402
from pydicom.uid import ExplicitVRLittleEndian, generate_uid  # noqa: E402
from pynetdicom import AE  # noqa: E402
from pynetdicom.sop_class import CTImageStorage  # noqa: E402

print(f"  info  pydicom {pydicom.__version__}")


def synthetic_ct() -> Dataset:
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = CTImageStorage
    ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.PatientName = "Kumar^Ramesh"
    ds.PatientSex = "M"
    ds.PatientAge = "062Y"
    ds.Modality = "CT"
    ds.StudyDescription = "CT BRAIN TEST"
    ds.Rows = 16
    ds.Columns = 16
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = np.arange(256, dtype=np.uint16).reshape(16, 16).tobytes()
    return ds


import io  # noqa: E402

instance = synthetic_ct()
buffer = io.BytesIO()
pydicom.dcmwrite(buffer, instance, write_like_original=False)
png = pacs.to_png(buffer.getvalue())
check(png.startswith(b"\x89PNG"), "to_png renders a real PNG from the pixel data")
try:
    pacs.to_png(b"junk bytes, definitely not dicom")
    check(False, "junk bytes rendered as an image")
except ValueError:
    check(True, "junk bytes are refused with a clear error")


# --------------------------------------------------------------------------- #
print("\nthe receiver, end to end on loopback")
# --------------------------------------------------------------------------- #

spool = tempfile.mkdtemp(prefix="hc-pacs-check-")
port = free_port()
receiver = None
try:
    receiver = pacs.start_receiver(port, aet="TESTSCP", spool=spool)
    check(pacs.receiver_running() is not None, "the receiver reports itself running")

    ok, detail = pacs.echo("127.0.0.1", port, called_aet="TESTSCP",
                           calling_aet="TESTSCU")
    check(ok, f"our receiver answers C-ECHO ({detail})")

    try:
        pacs.start_receiver(free_port(), aet="SECOND", spool=spool)
        check(False, "a second receiver was allowed to start")
    except RuntimeError as exc:
        check("already listening" in str(exc), "a second receiver is refused")

    # Act as the modality: push the instance with C-STORE.
    scu = AE(ae_title="TESTSCU")
    scu.add_requested_context(CTImageStorage)
    assoc = scu.associate("127.0.0.1", port, ae_title="TESTSCP")
    check(assoc.is_established, "a storage association is accepted")
    status = assoc.send_c_store(instance)
    assoc.release()
    check(status and status.Status == 0x0000,
          f"C-STORE succeeds (status {getattr(status, 'Status', None)})")
    check(receiver.received == 1, "the receiver counted the instance")

    studies = pacs.received_studies(spool)
    check(len(studies) == 1, f"one received study is indexed ({len(studies)})")
    study = studies[0]
    check(study["patient"] == "Kumar Ramesh", f"patient name indexed ({study['patient']!r})")
    check(study["modality"] == "CT" and study["description"] == "CT BRAIN TEST",
          "modality and description indexed")
    check(len(study["instances"]) == 1 and os.path.exists(study["instances"][0]),
          "the .dcm file is on disk")

    # The spooled file round-trips through the existing metadata cross-check.
    import dicom_meta

    with open(study["instances"][0], "rb") as fh:
        meta = dicom_meta.read_meta(fh.read())
    check(meta.patient_name == "Kumar Ramesh" and meta.modality == "CT",
          "the spooled file parses with dicom_meta")

    # And through the pixel renderer.
    with open(study["instances"][0], "rb") as fh:
        check(pacs.to_png(fh.read()).startswith(b"\x89PNG"),
              "the spooled file renders to PNG for the AI pre-read")

    # C-FIND against a storage-only SCP must fail plainly, not hang or lie.
    try:
        pacs.find_studies("127.0.0.1", port, called_aet="TESTSCP")
        check(False, "C-FIND claimed success against a storage-only SCP")
    except RuntimeError:
        check(True, "C-FIND against a storage-only SCP fails plainly")

    pacs.clear_received(study["study_uid"], spool)
    check(pacs.received_studies(spool) == [], "clear_received empties the study")
finally:
    pacs.stop_receiver()
    shutil.rmtree(spool, ignore_errors=True)

check(pacs.receiver_running() is None, "stop_receiver clears the singleton")
ok, _ = pacs.echo("127.0.0.1", port, called_aet="TESTSCP")
check(not ok, "the port is closed after stop")


# --------------------------------------------------------------------------- #
print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All PACS checks passed.")
