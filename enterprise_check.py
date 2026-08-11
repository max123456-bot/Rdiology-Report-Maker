"""
Offline checks for the institutional layer:

    access.py   - RBAC: only an attending signs
    records.py  - justification hard-stop, HMAC signature on sign
    crypto.py   - AES-256-GCM PHI encryption at rest, both stores
    verify.py   - keyed (HMAC) attestation
    mllp.py     - HL7 MLLP: ORM in -> worklist record; ORU out -> ACK. Loopback.
    pacs.py     - DICOM MWL query (loopback SCP), DICOMweb QIDO/WADO (mock HTTP)
    deid.py     - de-identification round trip
    ollama.py   - the air-gapped provider (mock HTTP)

    python enterprise_check.py

No external services. Everything runs on 127.0.0.1 or in-process.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import threading

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
print("\naccess - RBAC")
# --------------------------------------------------------------------------- #

import access  # noqa: E402

check(access.current_role() == "attending",
      "an unconfigured deployment defaults to attending (pre-RBAC behaviour)")
check(access.can("sign", "attending"), "an attending can sign")
check(not access.can("sign", "resident"), "a resident cannot sign")
check(not access.can("sign", "transcriptionist"), "a transcriptionist cannot sign")
check(not access.can("sign", "auditor"), "an auditor can do nothing mutating")
check(access.can("deliver", "admin"), "an admin can deliver")
check(not access.can("sign", "admin"), "even an admin cannot sign")
try:
    access.require("sign", "resident")
    check(False, "require() let a resident sign")
except access.PermissionDenied as exc:
    check("resident" in str(exc), "PermissionDenied names the role")

os.environ["ROLE_DEFAULT"] = "resident"
check(access.current_role() == "resident", "ROLE_DEFAULT applies to everyone unmapped")
os.environ.pop("ROLE_DEFAULT", None)


# --------------------------------------------------------------------------- #
print("\nrecords - sign gating and the justification hard-stop")
# --------------------------------------------------------------------------- #

import records  # noqa: E402
import storage  # noqa: E402
from hc_format import parse_report  # noqa: E402

TEXT = ("USG ABDOMEN REPORT\n\nPATIENT NAME: Mrs. Sunita Devi\nAGE/SEX: 45Y/F\n\n"
        "FINDINGS:\nLiver measures 16.2 cm.\n\nIMPRESSION:\n- Hepatomegaly.")
_tmp = tempfile.mkdtemp(prefix="hc-enterprise-")
storage._store = storage.FileStore(os.path.join(_tmp, "store"))

rec = records.new_record(TEXT, parse_report(TEXT).blocks)
try:
    records.sign(dict(rec), user="r1", role="resident")
    check(False, "a resident was allowed to sign")
except access.PermissionDenied:
    check(True, "a resident signing raises PermissionDenied")

try:
    records.sign(dict(rec), user="a1", role="attending", critical=True)
    check(False, "a critical flag was signed past without justification")
except ValueError as exc:
    check("justification" in str(exc).lower(),
          "signing past a critical flag demands a justification")

justified = records.sign(dict(rec), user="a1", role="attending", critical=True,
                         justification="Reviewed the images personally; artefact.")
check(justified["status"] == "signed"
      and justified.get("sign_justification", "").startswith("Reviewed"),
      "a justified critical sign goes through and is recorded")
check(any("signed past critical flag" in t.get("what", "")
          for t in justified["trail"]),
      "the override is on the record's own trail")

plain = records.sign(dict(rec), user="a1", role="attending")
check(plain["status"] == "signed", "a clean sign needs no justification")


# --------------------------------------------------------------------------- #
print("\nverify - keyed signatures")
# --------------------------------------------------------------------------- #

import verify  # noqa: E402

check(verify.hmac_signature("text") == "",
      "no ATTEST_KEY means no signature - never a fake one")

os.environ["ATTEST_KEY"] = "test-signing-key"
signature = verify.hmac_signature("The report text")
check(len(signature) == 64, "with a key, an HMAC-SHA256 signature is produced")
check(verify.verify_signature("The report text", signature),
      "the signature verifies against the same text")
check(not verify.verify_signature("The report text.", signature),
      "one changed character breaks the signature")

signed_rec = records.sign(dict(rec), user="a1", role="attending")
check(verify.verify_signature(signed_rec["report_text"], signed_rec["signature"]),
      "a signed record carries a verifiable signature over its exact text")

attest = verify.attestation("src", b"docx", True)
check("signature" in attest and verify.verify_signature(attest["chain"],
                                                        attest["signature"]),
      "the attestation chain is HMAC-signed when the key is present")

link2 = verify.attestation("src2", b"docx2", False, previous_chain=attest["chain"])
link3 = verify.attestation("src3", b"docx3", True, previous_chain=link2["chain"])
status = verify.audit_chain_status([attest, link2, link3])
check(status["intact"] and status["signed_ok"] == 3 and not status["signed_bad"],
      "an untouched chain recomputes clean, every signature verified")
tampered = dict(link2)
tampered["verdict"] = "PASS"  # someone flips a FAIL after the fact
status = verify.audit_chain_status([attest, tampered, link3])
check(not status["intact"] and status["first_break"] == 1,
      "flipping one verdict is caught at exactly that link")
os.environ.pop("ATTEST_KEY", None)


# --------------------------------------------------------------------------- #
print("\ncrypto - PHI at rest")
# --------------------------------------------------------------------------- #

import crypto  # noqa: E402

check(not crypto.enabled(), "no PHI_KEY means encryption is off")
check(crypto.encrypt_text("Sunita") == "Sunita", "and text passes through untouched")

os.environ["PHI_KEY"] = "clinic-secret-passphrase"
try:
    sealed = crypto.encrypt_text("Mrs. Sunita Devi")
    check(sealed.startswith("enc:v1:") and "Sunita" not in sealed,
          "with a key, the name becomes ciphertext")
    check(crypto.decrypt_text(sealed) == "Mrs. Sunita Devi", "and round-trips")
    check(crypto.encrypt_text(sealed) == sealed, "ciphertext is never double-encrypted")
    check(crypto.decrypt_text("plain old text") == "plain old text",
          "pre-encryption plaintext rows still read fine")

    # Store-level: what hits the disk is ciphertext, what comes back is not.
    store = storage.FileStore(os.path.join(_tmp, "encstore"))
    enc_rec = records.new_record(TEXT, parse_report(TEXT).blocks)
    store.save_report("clinic", enc_rec)
    raw_on_disk = open(
        store._report_path("clinic", enc_rec["id"]), encoding="utf-8").read()
    check("Sunita" not in raw_on_disk and "16.2" not in raw_on_disk,
          "the JSON on disk contains no readable PHI")
    check("enc:v1:" in raw_on_disk, "the payload fields are ciphertext")
    loaded = store.get_report("clinic", enc_rec["id"])
    check(loaded["patient"] == "Mrs. Sunita Devi"
          and "16.2 cm" in loaded["report_text"],
          "reading it back decrypts transparently")
    check(loaded["patient_key"] == enc_rec["patient_key"],
          "the pseudonymous filter column is untouched")

    listed = store.list_reports("clinic")
    check(listed and listed[0]["patient"] == "Mrs. Sunita Devi",
          "list_reports decrypts too")

    os.environ["PHI_KEY"] = "the-wrong-key"
    locked = store.get_report("clinic", enc_rec["id"])
    check("cannot open" in locked["patient"],
          "a wrong key yields a visible marker, not a crash")
finally:
    os.environ.pop("PHI_KEY", None)


# --------------------------------------------------------------------------- #
print("\nmllp - HL7 over TCP, loopback")
# --------------------------------------------------------------------------- #

import mllp  # noqa: E402

message, rest = mllp.unframe(mllp.frame("MSH|^~\\&|X") + b"extra")
check(message == "MSH|^~\\&|X" and rest == b"extra", "framing round-trips")
check(mllp.unframe(b"\x0bpartial")[0] is None, "a partial frame waits for more")

ORM = ("MSH|^~\\&|HIS|HOSP|HCFORMAT|CLINIC|20260811||ORM^O01|MSG001|P|2.5\r"
       "PID|1||MRN123||Devi^Sunita||19810101|F\r"
       "ORC|NW\r"
       "OBR|1|PLACER1||USG^USG ABDOMEN PELVIS|||20260811||||||||"
       "|Mehta^Rakesh\r")

check(mllp.message_type(ORM) == "ORM^O01", "the message type parses")
order_rec = mllp.orm_to_record(ORM)
check(order_rec["patient"] == "Sunita Devi", f"PID name inverts ({order_rec['patient']!r})")
check(order_rec["study"] == "USG ABDOMEN PELVIS", "OBR-4 becomes the study")
check(order_rec["modality"] == "USG", "modality guessed from the order")
check(order_rec["status"] == "draft" and order_rec["source"] == "hl7-order",
      "an order lands as a draft worklist item")

port = free_port()
listener = None
try:
    listener = mllp.start_order_listener(port, tenant="hl7-clinic")
    result = mllp.send_message("127.0.0.1", port, ORM)
    check(result.ok, f"the listener ACKs an order (AA) - {result.detail}")
    check(listener.received == 1, "the listener counted the order")
    rows = storage.get_store().list_reports("hl7-clinic")
    check(len(rows) == 1 and rows[0]["patient"] == "Sunita Devi",
          "the order is on the worklist")
    check("MSA|AA|MSG001" in result.ack, "the ACK quotes the original control id")

    adt = ORM.replace("ORM^O01", "ADT^A01")
    result = mllp.send_message("127.0.0.1", port, adt)
    check(result.ok and listener.received == 1,
          "non-order chatter is ACKed politely and creates nothing")

    try:
        mllp.start_order_listener(free_port())
        check(False, "a second listener was allowed")
    except RuntimeError:
        check(True, "a second listener is refused")
finally:
    mllp.stop_order_listener()

result = mllp.send_message("127.0.0.1", port, ORM)
check(not result.ok, "sending to a stopped listener fails honestly")
check(mllp.mllp_config() is None, "no MLLP_HOST means outbound push is unconfigured")


# --------------------------------------------------------------------------- #
print("\npacs - Modality Worklist, loopback SCP")
# --------------------------------------------------------------------------- #

import pacs  # noqa: E402

if not pacs.PYNETDICOM_OK:
    print("  skip  pynetdicom missing")
else:
    from pydicom.dataset import Dataset
    from pynetdicom import AE, evt
    from pynetdicom.sop_class import ModalityWorklistInformationFind

    def handle_mwl(event):
        item = Dataset()
        item.PatientName = "Devi^Sunita"
        item.PatientID = "MRN123"
        item.PatientSex = "F"
        item.PatientBirthDate = "19810101"
        item.AccessionNumber = "ACC42"
        item.ReferringPhysicianName = "Mehta^Rakesh"
        item.RequestedProcedureDescription = "USG ABDOMEN"
        sps = Dataset()
        sps.Modality = "US"
        sps.ScheduledProcedureStepStartDate = "20260811"
        sps.ScheduledProcedureStepDescription = "USG ABDOMEN"
        item.ScheduledProcedureStepSequence = [sps]
        yield 0xFF00, item

    mwl_port = free_port()
    ae = AE(ae_title="MWLSCP")
    ae.add_supported_context(ModalityWorklistInformationFind)
    scp = ae.start_server(("127.0.0.1", mwl_port), block=False,
                          evt_handlers=[(evt.EVT_C_FIND, handle_mwl)])
    try:
        worklist = pacs.mwl_query("127.0.0.1", mwl_port, called_aet="MWLSCP")
        check(len(worklist) == 1, f"the MWL answers ({len(worklist)} item)")
        item = worklist[0]
        check(item["patient"] == "Devi Sunita" and item["accession"] == "ACC42",
              "patient and accession come through")
        check(item["modality"] == "US" and item["scheduled"] == "20260811",
              "the scheduled step parses")
    finally:
        scp.shutdown()

    try:
        pacs.mwl_query("127.0.0.1", free_port())
        check(False, "a dead MWL host did not raise")
    except RuntimeError:
        check(True, "a dead MWL host fails plainly")


# --------------------------------------------------------------------------- #
print("\npacs - DICOMweb QIDO/WADO against a mock server")
# --------------------------------------------------------------------------- #

import http.server  # noqa: E402

QIDO_BODY = json.dumps([{
    "00100010": {"vr": "PN", "Value": [{"Alphabetic": "Devi^Sunita"}]},
    "00100020": {"vr": "LO", "Value": ["MRN123"]},
    "00100040": {"vr": "CS", "Value": ["F"]},
    "00080020": {"vr": "DA", "Value": ["20260811"]},
    "00081030": {"vr": "LO", "Value": ["USG ABDOMEN"]},
    "00080061": {"vr": "CS", "Value": ["US"]},
    "0020000D": {"vr": "UI", "Value": ["1.2.3.4"]},
}]).encode()

QIDO_INSTANCES_BODY = json.dumps([{
    "0020000E": {"vr": "UI", "Value": ["1.2.3.4.1"]},
    "00080018": {"vr": "UI", "Value": ["1.2.3.4.1.1"]},
}]).encode()

BOUNDARY = b"--BOUNDARY-X"
DICOM_BYTES = b"DICM-fake-instance-bytes"
WADO_BODY = (BOUNDARY + b"\r\nContent-Type: application/dicom\r\n\r\n"
             + DICOM_BYTES + b"\r\n" + BOUNDARY + b"--")


class MockDicomWeb(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/dicom-web/studies?"):
            body, ctype = QIDO_BODY, "application/dicom+json"
        elif "/instances?" in self.path:
            body, ctype = QIDO_INSTANCES_BODY, "application/dicom+json"
        else:
            body, ctype = WADO_BODY, 'multipart/related; type="application/dicom"'
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


web_port = free_port()
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", web_port), MockDicomWeb)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
os.environ["DICOMWEB_URL"] = f"http://127.0.0.1:{web_port}/dicom-web"
try:
    studies = pacs.qido_studies(limit=5)
    check(len(studies) == 1 and studies[0]["patient"] == "Devi Sunita",
          "QIDO-RS parses the DICOM JSON (PN included)")
    check(studies[0]["study_uid"] == "1.2.3.4", "the study UID comes through")
    instance = pacs.wado_instance("1.2.3.4", "1.2.3.4.1", "1.2.3.4.1.1")
    check(instance == DICOM_BYTES,
          "WADO-RS extracts the DICOM part from the multipart body")
    first = pacs.wado_first_instance("1.2.3.4")
    check(first == DICOM_BYTES,
          "wado_first_instance goes QIDO instances -> WADO from a study UID alone")
finally:
    os.environ.pop("DICOMWEB_URL", None)
    httpd.shutdown()

check(pacs.dicomweb_config() is None, "no DICOMWEB_URL means unconfigured, not broken")


# --------------------------------------------------------------------------- #
print("\ndeid - de-identification round trip")
# --------------------------------------------------------------------------- #

import deid  # noqa: E402

SOURCE = ("Patient Mrs. Sunita Devi, MRN: AB1234, contacted on 9876543210, "
          "scanned on 11/08/2026. Mrs. Sunita Devi has hepatomegaly. "
          "Report to sunita@example.com.")
result = deid.deidentify(SOURCE, known_names=["Mrs. Sunita Devi"])
check("Sunita" not in result.text, "the known name is gone - every occurrence")
check("AB1234" not in result.text, "the labelled MRN is gone")
check("9876543210" not in result.text, "the phone number is gone")
check("11/08/2026" not in result.text, "the date is gone")
check("sunita@example.com" not in result.text, "the email is gone")
check("hepatomegaly" in result.text, "the clinical content is untouched")
check(result.text.count("[NAME-1]") == 2, "the same name maps to one placeholder")
restored = deid.reidentify(result.text, result.mapping)
check(restored == SOURCE, "re-identification restores the exact original")

check(not deid.cloud_deid_enabled(), "DEID_CLOUD is off by default")
passthrough = deid.for_cloud(SOURCE, ["Mrs. Sunita Devi"])
check(passthrough.text == SOURCE and not passthrough.changed,
      "with the flag off, for_cloud passes through")
os.environ["DEID_CLOUD"] = "true"
shielded = deid.for_cloud(SOURCE, ["Mrs. Sunita Devi"])
check(shielded.changed and "Sunita" not in shielded.text,
      "with the flag on, for_cloud shields")
os.environ.pop("DEID_CLOUD", None)


# --------------------------------------------------------------------------- #
print("\nollama - the air-gapped provider")
# --------------------------------------------------------------------------- #

import ollama  # noqa: E402
import providers  # noqa: E402

check(ollama.config() is None, "no OLLAMA_URL means unconfigured")
check("ollama" not in providers.available(),
      "the provider registry does not list an unconfigured ollama")


class MockOllama(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length))
        MockOllama.last_request = request
        answer = json.dumps({"response": json.dumps(
            {"impression": ["Hepatomegaly", "No free fluid"]})}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *args):
        pass


ollama_port = free_port()
mock = http.server.ThreadingHTTPServer(("127.0.0.1", ollama_port), MockOllama)
threading.Thread(target=mock.serve_forever, daemon=True).start()
os.environ["OLLAMA_URL"] = f"http://127.0.0.1:{ollama_port}"
os.environ["OLLAMA_MODEL"] = "test-model"
try:
    providers._registry.pop("ollama", None)
    check("ollama" in providers.available(), "a configured ollama registers")

    points = ollama.draft_impression(
        "Liver is enlarged, 16.8 cm. No free fluid.", "", "gemini-flash", None)
    check(points == ["Hepatomegaly", "No free fluid"],
          "impressions come back through the local model")
    check(MockOllama.last_request["model"] == "test-model",
          "a Gemini model name is swapped for OLLAMA_MODEL")
    check(MockOllama.last_request.get("format") == "json",
          "JSON mode is requested")
finally:
    os.environ.pop("OLLAMA_URL", None)
    os.environ.pop("OLLAMA_MODEL", None)
    providers._registry.pop("ollama", None)
    mock.shutdown()

try:
    os.environ["OLLAMA_URL"] = "http://127.0.0.1:1"
    ollama.generate("hello")
    check(False, "a dead Ollama did not raise")
except RuntimeError as exc:
    check("Could not reach Ollama" in str(exc), "a dead Ollama fails plainly")
finally:
    os.environ.pop("OLLAMA_URL", None)


# --------------------------------------------------------------------------- #
storage._store = None
shutil.rmtree(_tmp, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All enterprise checks passed.")
