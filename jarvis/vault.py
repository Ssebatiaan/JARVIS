"""Secretos en el Administrador de credenciales de Windows (cifrados con la cuenta del usuario)."""

import ctypes as ct
from ctypes import wintypes

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
MAX_SECRET_BYTES = 2560


class _Credential(ct.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ct.POINTER(ct.c_char)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ct.c_void_p),
        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
    ]


class CredentialVault:
    def __init__(self, prefix: str):
        self._prefix = prefix
        api = ct.WinDLL("advapi32.dll", use_last_error=True)
        pointer = ct.POINTER(_Credential)
        self._write, self._read = api.CredWriteW, api.CredReadW
        self._delete, self._enumerate, self._free = api.CredDeleteW, api.CredEnumerateW, api.CredFree
        self._write.argtypes = [pointer, wintypes.DWORD]
        self._read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ct.POINTER(pointer)]
        self._delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._enumerate.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ct.POINTER(wintypes.DWORD),
                                    ct.POINTER(ct.POINTER(pointer))]
        self._free.argtypes = [ct.c_void_p]
        self._free.restype = None
        for function in (self._write, self._read, self._delete, self._enumerate):
            function.restype = wintypes.BOOL

    def set(self, name: str, secret: str) -> None:
        data = secret.encode("utf-8")
        if not 0 < len(data) <= MAX_SECRET_BYTES:
            raise ValueError("El secreto está vacío o es demasiado largo.")
        blob = ct.create_string_buffer(data, len(data))
        credential = _Credential(Type=CRED_TYPE_GENERIC, TargetName=self._prefix + name, CredentialBlobSize=len(data),
                                 CredentialBlob=ct.cast(blob, ct.POINTER(ct.c_char)),
                                 Persist=CRED_PERSIST_LOCAL_MACHINE, UserName="JARVIS")
        if not self._write(ct.byref(credential), 0):
            raise ct.WinError(ct.get_last_error())

    def get(self, name: str) -> str | None:
        found = ct.POINTER(_Credential)()
        if not self._read(self._prefix + name, CRED_TYPE_GENERIC, 0, ct.byref(found)):
            if ct.get_last_error() == ERROR_NOT_FOUND:
                return None
            raise ct.WinError(ct.get_last_error())
        try:
            return ct.string_at(found.contents.CredentialBlob, found.contents.CredentialBlobSize).decode("utf-8")
        finally:
            self._free(found)

    def delete(self, name: str) -> bool:
        if self._delete(self._prefix + name, CRED_TYPE_GENERIC, 0):
            return True
        if ct.get_last_error() == ERROR_NOT_FOUND:
            return False
        raise ct.WinError(ct.get_last_error())

    def names(self) -> list[str]:
        count, items = wintypes.DWORD(), ct.POINTER(ct.POINTER(_Credential))()
        if not self._enumerate(self._prefix + "*", 0, ct.byref(count), ct.byref(items)):
            if ct.get_last_error() == ERROR_NOT_FOUND:
                return []
            raise ct.WinError(ct.get_last_error())
        try:
            return sorted(items[i].contents.TargetName[len(self._prefix):] for i in range(count.value))
        finally:
            self._free(items)
