import ctypes as ct
from contextlib import contextmanager
from ctypes import wintypes
from uuid import UUID

from .tools import AudioOutputUnavailable, VolumeStatus

HRESULT = ct.c_int32
INTERFACE = ct.c_void_p
INTERFACE_OUT = ct.POINTER(INTERFACE)

CLSCTX_ALL = 23
COINIT_APARTMENTTHREADED = 2
RPC_E_CHANGED_MODE = 0x80010106
E_NOTFOUND = 0x80070490
E_RENDER = 0
E_MULTIMEDIA = 1

# Posiciones en las tablas de métodos de las interfaces del SDK de Windows.
RELEASE = 2
GET_DEFAULT_AUDIO_ENDPOINT = 4
ACTIVATE = 3
SET_MASTER_VOLUME_SCALAR = 7
GET_MASTER_VOLUME_SCALAR = 9
GET_MUTE = 15


class Guid(ct.Structure):
    _fields_ = [
        ("data1", ct.c_uint32),
        ("data2", ct.c_uint16),
        ("data3", ct.c_uint16),
        ("data4", ct.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, value: str):
        return cls.from_buffer_copy(UUID(value).bytes_le)


CLS_ID_DEVICE_ENUMERATOR = Guid.parse("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_DEVICE_ENUMERATOR = Guid.parse("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_ENDPOINT_VOLUME = Guid.parse("5CDF2C82-841E-4546-9722-0CF74078229A")


def _method(interface, slot, result_type, *argument_types):
    vtable = ct.cast(interface, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
    return ct.WINFUNCTYPE(result_type, INTERFACE, *argument_types)(vtable[slot])


def _check(result: int) -> None:
    if result < 0:
        raise OSError(result & 0xFFFFFFFF, "Falló una operación de Windows Core Audio.")


@contextmanager
def _default_endpoint():
    ole32 = ct.WinDLL("ole32.dll")
    initialize = ole32.CoInitializeEx
    initialize.argtypes = [ct.c_void_p, wintypes.DWORD]
    initialize.restype = HRESULT
    uninitialize = ole32.CoUninitialize
    uninitialize.argtypes = []
    uninitialize.restype = None
    create_instance = ole32.CoCreateInstance
    create_instance.argtypes = [
        ct.POINTER(Guid), INTERFACE, wintypes.DWORD, ct.POINTER(Guid), INTERFACE_OUT
    ]
    create_instance.restype = HRESULT

    enumerator, device, endpoint = INTERFACE(), INTERFACE(), INTERFACE()
    initialized = initialize(None, COINIT_APARTMENTTHREADED)
    if initialized & 0xFFFFFFFF != RPC_E_CHANGED_MODE:
        _check(initialized)

    try:
        _check(create_instance(
            ct.byref(CLS_ID_DEVICE_ENUMERATOR), None, CLSCTX_ALL,
            ct.byref(IID_DEVICE_ENUMERATOR), ct.byref(enumerator),
        ))
        get_device = _method(
            enumerator, GET_DEFAULT_AUDIO_ENDPOINT, HRESULT, ct.c_int, ct.c_int, INTERFACE_OUT
        )
        result = get_device(enumerator, E_RENDER, E_MULTIMEDIA, ct.byref(device))
        if result & 0xFFFFFFFF == E_NOTFOUND:
            raise AudioOutputUnavailable()
        _check(result)

        activate = _method(
            device, ACTIVATE, HRESULT, ct.POINTER(Guid), wintypes.DWORD, ct.c_void_p, INTERFACE_OUT
        )
        _check(activate(
            device, ct.byref(IID_ENDPOINT_VOLUME), CLSCTX_ALL, None, ct.byref(endpoint)
        ))
        yield endpoint
    finally:
        for interface in (endpoint, device, enumerator):
            if interface:
                _method(interface, RELEASE, wintypes.ULONG)(interface)
        if initialized >= 0:
            uninitialize()


def _read_volume(endpoint) -> VolumeStatus:
    level = ct.c_float()
    muted = wintypes.BOOL()
    get_level = _method(endpoint, GET_MASTER_VOLUME_SCALAR, HRESULT, ct.POINTER(ct.c_float))
    get_mute = _method(endpoint, GET_MUTE, HRESULT, ct.POINTER(wintypes.BOOL))
    _check(get_level(endpoint, ct.byref(level)))
    _check(get_mute(endpoint, ct.byref(muted)))
    return VolumeStatus(round(level.value * 100), bool(muted.value))


def read_default_volume() -> VolumeStatus:
    with _default_endpoint() as endpoint:
        return _read_volume(endpoint)


def set_default_volume(percent: int) -> VolumeStatus:
    if not 0 <= percent <= 100:
        raise ValueError("El volumen debe estar entre 0 y 100.")

    with _default_endpoint() as endpoint:
        set_level = _method(endpoint, SET_MASTER_VOLUME_SCALAR, HRESULT, ct.c_float, ct.POINTER(Guid))
        _check(set_level(endpoint, percent / 100, None))
        return _read_volume(endpoint)
