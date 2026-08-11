"""
Field-level encryption for PHI at rest.

With a PHI_KEY configured, the fields of a report record that identify a
patient - name, age/sex, referrer, and the report text itself - are
AES-256-GCM encrypted before they touch any store, and decrypted on the way
out. The database (or the JSON file on a stolen laptop) then holds
ciphertext; the running app is unchanged.

Design constraints, in order:

  * Backward compatible both ways. No key -> plaintext, exactly as before.
    Key added later -> old plaintext rows still read fine (decrypt passes
    them through) and re-encrypt on their next save. Key removed -> the
    ciphertext is unreadable and SAYS SO in the field, rather than crashing
    the worklist.
  * The indexed columns (tenant, status, urgency, patient_key) stay
    plaintext: patient_key is already a pseudonymous slug and the store
    filters on it in SQL. Full-name PHI never goes in a column, only inside
    the payload - that was true before encryption too.
  * AES-256-GCM with a random 96-bit nonce per field per save. The key is
    the SHA-256 of PHI_KEY, so any passphrase works and is always 32 bytes.

What this is NOT: transport security (TLS is the host's job) or protection
from someone who has the key. It closes one specific hole - a copied
database file being readable.
"""

from __future__ import annotations

import base64
import hashlib
import os

_PREFIX = "enc:v1:"
PHI_FIELDS = ("patient", "age_sex", "referrer", "report_text",
              "sign_justification")


def _configured_key() -> bytes:
    value = ""
    try:
        import streamlit as st

        if "PHI_KEY" in st.secrets:
            value = str(st.secrets["PHI_KEY"]).strip()
    except Exception:
        pass
    value = value or os.environ.get("PHI_KEY", "").strip()
    if not value:
        return b""
    return hashlib.sha256(value.encode("utf-8")).digest()  # always 32 bytes


def enabled() -> bool:
    return bool(_configured_key())


def encrypt_text(plain: str) -> str:
    """One field. Returns the input untouched when no key is configured."""
    key = _configured_key()
    if not key or not plain:
        return plain
    if plain.startswith(_PREFIX):
        return plain  # already ciphertext - never double-encrypt
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    sealed = AESGCM(key).encrypt(nonce, plain.encode("utf-8"), None)
    return _PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode("ascii")


def decrypt_text(value: str) -> str:
    """
    One field back. Plaintext passes through (pre-encryption rows); a wrong
    or missing key yields a visible marker instead of an exception, because
    a locked field must not take the whole worklist down.
    """
    if not isinstance(value, str) or not value.startswith(_PREFIX):
        return value
    key = _configured_key()
    if not key:
        return "[encrypted - PHI_KEY is not configured on this deployment]"
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        raw = base64.urlsafe_b64decode(value[len(_PREFIX):].encode("ascii"))
        return AESGCM(key).decrypt(raw[:12], raw[12:], None).decode("utf-8")
    except Exception:
        return "[encrypted - the configured PHI_KEY cannot open this field]"


# What stays plaintext: exactly the columns the stores index and filter on.
# Everything else - the name, the text, the measurements, the trail - goes
# inside one sealed blob, so no field can leak by being forgotten in a list.
_INDEX_FIELDS = ("id", "status", "urgency", "updated", "created", "patient_key")


def protect_key(slug: str) -> str:
    """
    The patient_key slug is derived from the patient's NAME, so it cannot sit
    in a plaintext column. A keyed HMAC keeps it deterministic - equality
    filtering still works - while saying nothing about the name. Passthrough
    when encryption is off.
    """
    key = _configured_key()
    if not key or not slug:
        return slug
    import hmac as _hmac

    return "pk1-" + _hmac.new(key, slug.encode("utf-8"),
                              hashlib.sha256).hexdigest()[:40]


def encrypt_record(record: dict) -> dict:
    """
    The stored shape: index columns plaintext (patient_key keyed-hashed),
    everything else - including the original patient_key - inside one
    AES-256-GCM sealed blob. No key -> the record unchanged.
    """
    if not enabled():
        return record
    import json

    out = {k: record[k] for k in _INDEX_FIELDS if k in record}
    out["patient_key"] = protect_key(str(record.get("patient_key") or ""))
    out["sealed"] = encrypt_text(json.dumps(record, ensure_ascii=False))
    return out


def decrypt_record(record: dict) -> dict:
    """The full record back. A wrong key yields marked fields, not a crash."""
    if not isinstance(record, dict):
        return record

    if isinstance(record.get("sealed"), str):
        import json

        opened = decrypt_text(record["sealed"])
        if opened.startswith("[encrypted"):
            base = {k: v for k, v in record.items() if k != "sealed"}
            base.setdefault("id", "")
            base.update({"patient": opened, "age_sex": "", "referrer": "",
                         "study": opened, "report_text": opened,
                         "triage_terms": [], "measurements": [], "trail": []})
            return base
        data = json.loads(opened)
        # The index columns are the live ones (a status update rewrites both,
        # but plaintext wins if they ever diverge). The original patient_key
        # comes back from inside the seal.
        for field in ("status", "urgency", "updated"):
            if field in record:
                data[field] = record[field]
        return data

    # Legacy per-field shape (enc:v1 on individual fields), and plaintext.
    if not any(isinstance(record.get(f), str) and record[f].startswith(_PREFIX)
               for f in PHI_FIELDS):
        return record
    out = dict(record)
    for field in PHI_FIELDS:
        if isinstance(out.get(field), str):
            out[field] = decrypt_text(out[field])
    return out
