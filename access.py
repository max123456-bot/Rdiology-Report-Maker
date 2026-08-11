"""
Who is allowed in.

On the clinic LAN this is off and the app behaves exactly as before - everyone on
the network is already trusted.

The moment the app is reachable from the internet that stops being true. Anyone
who finds the URL can generate reports, read every doctor's template, and see the
learned vocabulary. So remote access must be gated, and this provides two ways:

  ACCESS_CODE in secrets.toml   a shared passcode. Simple, and enough to stop
                               drive-by access. Everyone shares one code, so it
                               tells you nothing about *who* did something.

  [auth] in secrets.toml        real sign-in through any OIDC provider. Streamlit
                               fills st.user, so the activity log records names.
                               This is what a clinic with several staff wants.

Neither replaces putting the app behind Cloudflare Access or a VPN - this is the
last line, not the first.
"""

from __future__ import annotations

import hmac
import os

import streamlit as st

import storage


def _configured(name: str) -> str:
    try:
        if name in st.secrets:
            return str(st.secrets[name]).strip()
    except Exception:
        pass
    return os.environ.get(name, "").strip()


def oidc_configured() -> bool:
    try:
        return "auth" in st.secrets
    except Exception:
        return False


def signed_in() -> bool:
    try:
        return bool(getattr(st.user, "is_logged_in", False))
    except Exception:
        return False


def require_access() -> bool:
    """
    Gate the app. Returns True when the visitor may continue.

    Call once, at the very top of app.py, before anything is rendered.
    """
    # Real sign-in wins when a provider is configured.
    if oidc_configured():
        if signed_in():
            return True
        _login_screen()
        return False

    code = _configured("ACCESS_CODE")
    if not code:
        return True  # no gate configured - LAN mode

    if st.session_state.get("access_ok"):
        return True

    _passcode_screen(code)
    return False


def _login_screen() -> None:
    st.title("Radiology Report Generator")
    st.write("Sign in to continue.")
    if st.button("Sign in", type="primary"):
        st.login()
    st.caption("Reports and patient details are only visible after signing in.")


def _passcode_screen(expected: str) -> None:
    st.title("Radiology Report Generator")
    st.write("Enter the clinic access code.")

    entered = st.text_input("Access code", type="password", key="access_try")
    if st.button("Enter", type="primary"):
        # compare_digest so a wrong code cannot be guessed a character at a time.
        if hmac.compare_digest(entered.strip(), expected):
            st.session_state["access_ok"] = True
            storage.log("access.granted", "passcode")
            st.rerun()
        else:
            storage.log("access.refused", "passcode")
            st.error("That code is not right.")

    st.caption(
        "This code is shared by everyone, so the activity log cannot say who you are. "
        "Configure sign-in for that."
    )


def whoami() -> str:
    """A label for the current user, for the activity log and the UI."""
    if signed_in():
        try:
            return str(getattr(st.user, "email", "") or getattr(st.user, "name", "") or "signed in")
        except Exception:
            return "signed in"
    if _configured("ACCESS_CODE"):
        return "shared code"
    return ""


def sign_out_control() -> None:
    """A sign-out button, only when there is something to sign out of."""
    if oidc_configured() and signed_in():
        st.sidebar.caption(f"Signed in as {whoami()}")
        if st.sidebar.button("Sign out", width="stretch"):
            st.logout()
    elif _configured("ACCESS_CODE") and st.session_state.get("access_ok"):
        if st.sidebar.button("Lock", width="stretch",
                             help="Require the access code again on this device."):
            st.session_state.pop("access_ok", None)
            st.rerun()


# --------------------------------------------------------------------------- #
# Roles - who may do what
# --------------------------------------------------------------------------- #

# The full role set. A solo clinic never configures any of this and gets
# "attending" - full capability, exactly the behaviour before roles existed.
ROLES = ("attending", "resident", "transcriptionist", "admin", "auditor")

# What each role may do. Signing is the legally meaningful act, so it is the
# tightest: an attending radiologist only. Residents draft; transcriptionists
# type; auditors read.
_CAPABILITIES = {
    "attending":       {"draft", "sign", "deliver", "alert", "manage_templates"},
    "resident":        {"draft"},
    "transcriptionist": {"draft"},
    "admin":           {"draft", "deliver", "manage_templates"},
    "auditor":         set(),
}


def current_role() -> str:
    """
    The signed-in user's role.

    Configured with a ROLE_MAP secret: "a@x.com:attending, b@x.com:resident".
    ROLE_DEFAULT sets everyone else (default: attending, so a clinic that
    never heard of roles keeps working exactly as before). With no sign-in
    there is no identity to restrict, so the default applies.
    """
    fallback = (_configured("ROLE_DEFAULT") or "attending").strip().lower()
    if fallback not in ROLES:
        fallback = "attending"

    email = storage.current_user().strip().lower()
    raw_map = _configured("ROLE_MAP")
    if not email or not raw_map:
        return fallback
    for entry in raw_map.split(","):
        if ":" not in entry:
            continue
        who, _, role = entry.strip().partition(":")
        if who.strip().lower() == email and role.strip().lower() in ROLES:
            return role.strip().lower()
    return fallback


def can(action: str, role: str | None = None) -> bool:
    """May this role perform this action? Unknown roles can do nothing."""
    return action in _CAPABILITIES.get(role or current_role(), set())


class PermissionDenied(RuntimeError):
    """The role is not allowed to perform this action."""

    def __init__(self, action: str, role: str):
        super().__init__(
            f"A {role} cannot {action}. Signing needs an attending radiologist - "
            "that is the legally meaningful act, and the log must show who did it."
        )
        self.action = action
        self.role = role


def require(action: str, role: str | None = None) -> None:
    """Raise PermissionDenied unless the role may perform the action."""
    resolved = role or current_role()
    if not can(action, resolved):
        raise PermissionDenied(action, resolved)
