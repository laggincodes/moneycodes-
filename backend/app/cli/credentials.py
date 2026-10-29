"""
Secure Windows Credential Management for WebsiteLead Hunter CLI.
Uses Windows Credential Manager via Advapi32.dll (ctypes).
Credentials are never stored in plaintext files, logs, or environment variables.
"""
import ctypes
import os
import sys
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TARGET = "WebsiteLeadHunter:session"

# Windows Credential Manager Constants
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2

# Pluggable backend for testing / mock verification
_custom_backend = None


class WindowsCredentialStore:
    """Native Windows Credential Manager adapter using advapi32.dll."""

    def __init__(self):
        self._available = False
        if sys.platform == "win32":
            try:
                import ctypes.wintypes
                self.advapi32 = ctypes.windll.advapi32
                self.wintypes = ctypes.wintypes
                self._define_structures()
                self._available = True
            except Exception as exc:
                logger.debug(f"Windows Credential Manager initialization failed: {exc}")
                self._available = False

    def _define_structures(self):
        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", self.wintypes.DWORD),
                ("Type", self.wintypes.DWORD),
                ("TargetName", self.wintypes.LPWSTR),
                ("Comment", self.wintypes.LPWSTR),
                ("LastWritten", self.wintypes.FILETIME),
                ("CredentialBlobSize", self.wintypes.DWORD),
                ("CredentialBlob", ctypes.c_char_p),
                ("Persist", self.wintypes.DWORD),
                ("AttributeCount", self.wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", self.wintypes.LPWSTR),
                ("UserName", self.wintypes.LPWSTR),
            ]
        self.CREDENTIALW = CREDENTIALW
        self.advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), self.wintypes.DWORD]
        self.advapi32.CredWriteW.restype = self.wintypes.BOOL
        self.advapi32.CredReadW.argtypes = [
            self.wintypes.LPCWSTR,
            self.wintypes.DWORD,
            self.wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(CREDENTIALW)),
        ]
        self.advapi32.CredReadW.restype = self.wintypes.BOOL
        self.advapi32.CredFree.argtypes = [ctypes.c_void_p]
        self.advapi32.CredDeleteW.argtypes = [
            self.wintypes.LPCWSTR,
            self.wintypes.DWORD,
            self.wintypes.DWORD,
        ]
        self.advapi32.CredDeleteW.restype = self.wintypes.BOOL

    @property
    def is_available(self) -> bool:
        return self._available

    def write(self, target: str, username: str, secret: str) -> bool:
        if not self._available:
            raise RuntimeError("Windows Credential Manager is not available on this platform.")
        if not target or len(target) > 512:
            raise ValueError("Credential target exceeds maximum length of 512 characters.")
        if not username or len(username) > 512:
            raise ValueError("Credential username exceeds maximum length of 512 characters.")
        blob = secret.encode("utf-8")
        if len(blob) > 512:
            raise ValueError(f"Credential secret size ({len(blob)} bytes) exceeds Windows Credential Manager limit (512 bytes).")

        cred = self.CREDENTIALW(
            Flags=0,
            Type=CRED_TYPE_GENERIC,
            TargetName=target,
            Comment="WebsiteLead Hunter CLI Session Token",
            CredentialBlobSize=len(blob),
            CredentialBlob=blob,
            Persist=CRED_PERSIST_LOCAL_MACHINE,
            AttributeCount=0,
            Attributes=None,
            TargetAlias=None,
            UserName=username,
        )
        res = self.advapi32.CredWriteW(ctypes.byref(cred), 0)
        return bool(res)

    def read(self, target: str) -> Optional[Tuple[str, str]]:
        if not self._available:
            raise RuntimeError("Windows Credential Manager is not available on this platform.")
        if not target or len(target) > 512:
            return None
        p_cred = ctypes.POINTER(self.CREDENTIALW)()
        res = self.advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(p_cred))
        if not res:
            return None
        try:
            if not p_cred.contents.CredentialBlob or p_cred.contents.CredentialBlobSize == 0:
                return None
            username = p_cred.contents.UserName or ""
            blob = ctypes.string_at(p_cred.contents.CredentialBlob, p_cred.contents.CredentialBlobSize)
            return (username, blob.decode("utf-8", errors="replace"))
        except Exception as exc:
            logger.debug(f"Error reading credential blob: {exc}")
            return None
        finally:
            self.advapi32.CredFree(p_cred)

    def delete(self, target: str) -> bool:
        if not self._available:
            raise RuntimeError("Windows Credential Manager is not available on this platform.")
        if not target or len(target) > 512:
            return False
        res = self.advapi32.CredDeleteW(target, CRED_TYPE_GENERIC, 0)
        return bool(res)


_native_store = WindowsCredentialStore()


class InMemoryCredentialStore:
    """In-memory credential store for test isolation and platforms without native Windows APIs."""

    def __init__(self):
        self._store = {}
        self.is_available = True

    def write(self, target: str, username: str, secret: str) -> bool:
        if not target or len(target) > 512:
            raise ValueError("Credential target exceeds maximum length of 512 characters.")
        if not username or len(username) > 512:
            raise ValueError("Credential username exceeds maximum length of 512 characters.")
        blob = secret.encode("utf-8")
        if len(blob) > 512:
            raise ValueError(f"Credential secret size ({len(blob)} bytes) exceeds Windows Credential Manager limit (512 bytes).")
        self._store[target] = (username, secret)
        return True

    def read(self, target: str) -> Optional[Tuple[str, str]]:
        if not target or len(target) > 512:
            return None
        return self._store.get(target)

    def delete(self, target: str) -> bool:
        if target in self._store:
            del self._store[target]
            return True
        return False


def set_credential_backend(backend):
    """Sets a custom credential store backend (useful for isolated unit tests)."""
    global _custom_backend
    _custom_backend = backend


def get_credential_backend():
    """Returns the active credential backend."""
    if _custom_backend is not None:
        return _custom_backend
    return _native_store


def is_credential_store_available() -> bool:
    """Checks whether the credential store backend is functional."""
    backend = get_credential_backend()
    return getattr(backend, "is_available", False)


def save_session_token(email: str, token: str, target: str = DEFAULT_TARGET) -> bool:
    """
    Securely stores session token in Windows Credential Manager under target.
    Never prints or logs the token.
    """
    backend = get_credential_backend()
    return backend.write(target, email, token)


def load_session_token(target: str = DEFAULT_TARGET) -> Optional[Tuple[str, str]]:
    """
    Retrieves (email, token) from Windows Credential Manager.
    Returns None if no credential exists.
    """
    backend = get_credential_backend()
    return backend.read(target)


def clear_session_token(target: str = DEFAULT_TARGET) -> bool:
    """
    Permanently deletes session token from Windows Credential Manager.
    """
    backend = get_credential_backend()
    return backend.delete(target)
