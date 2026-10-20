"""ctypes declarations verified against LunaTranslator @51884b6 (LunaHostDll.cpp).

These declarations belong in the isolated Host process. They must not load a
DLL into the PySide6 GUI process. Luna_Start exports TEN callbacks; upstream's
older Python argtypes declaration lists nine despite passing ten.
"""
from __future__ import annotations
import ctypes as C

class ThreadParam(C.Structure):
    _fields_ = [('processId',C.c_uint32),('addr',C.c_uint64),('ctx',C.c_uint64),('ctx2',C.c_uint64)]

class SearchParam(C.Structure):
    _fields_ = [
        ('pattern',C.c_char*30),('address_method',C.c_int),('search_method',C.c_int),
        ('length',C.c_int),('offset',C.c_int),('searchTime',C.c_int),
        ('maxRecords',C.c_int),('codepage',C.c_int),('padding',C.c_int64),
        ('minAddress',C.c_uint64),('maxAddress',C.c_uint64),
        ('boundaryModule',C.c_wchar*120),('exportModule',C.c_wchar*120),
        ('text',C.c_wchar*30),('isjithook',C.c_bool),
        ('sharememname',C.c_wchar*64),('sharememsize',C.c_uint64)]

PROCESS = C.CFUNCTYPE(None,C.c_uint32)
CREATE = C.CFUNCTYPE(None,C.c_wchar_p,C.c_char_p,ThreadParam,C.c_bool)
THREAD = C.CFUNCTYPE(None,C.c_wchar_p,C.c_char_p,ThreadParam)
OUTPUT = C.CFUNCTYPE(None,C.c_wchar_p,C.c_char_p,ThreadParam,C.c_wchar_p)
INFO = C.CFUNCTYPE(None,C.c_int,C.c_wchar_p)
INSERT = C.CFUNCTYPE(None,C.c_uint32,C.c_uint64,C.c_wchar_p)
EMBED = C.CFUNCTYPE(None,C.c_wchar_p,ThreadParam)
I18N = C.CFUNCTYPE(C.c_void_p,C.c_wchar_p)
EMU = C.CFUNCTYPE(None,C.c_wchar_p,C.c_wchar_p,C.c_wchar_p)
FIND = C.CFUNCTYPE(None,C.c_wchar_p,C.c_wchar_p)
HISTORY = C.CFUNCTYPE(None,C.c_wchar_p)
START_CALLBACKS = (PROCESS,PROCESS,CREATE,THREAD,OUTPUT,INFO,INSERT,EMBED,I18N,EMU)


def load_exports(dll):
    """Bind ABI signatures in the process that owns the DLL."""
    definitions={
        'Luna_Start':(START_CALLBACKS,None),
        'Luna_ConnectProcess':((C.c_uint32,),None),
        'Luna_DetachProcess':((C.c_uint32,),None),
        'Luna_CheckIfNeedInject':((C.c_uint32,),C.c_bool),
        'Luna_InsertHookCode':((C.c_uint32,C.c_wchar_p),None),
        'Luna_RemoveHook':((C.c_uint32,C.c_uint64),None),
        'Luna_Settings':((C.c_int,C.c_int,C.c_int,C.c_int,C.c_bool),None),
        'Luna_QueryThreadHistory':((ThreadParam,C.c_bool,HISTORY),None),
        'Luna_FindHooks':((C.c_uint32,SearchParam,FIND,C.c_wchar_p),None),
        'Luna_SyncThread':((ThreadParam,C.c_bool),None),
    }
    for symbol,(argtypes,restype) in definitions.items():
        method=getattr(dll,symbol)
        method.argtypes=argtypes
        method.restype=restype
    return dll


def validate_abi():
    # 64bit Windows: DWORD+pad and 3xUINT64. 32bit native MSVC may align
    # UINT64 on a 8-byte struct boundary; verify the target build in Windows CI.
    if C.sizeof(ThreadParam) != 32 or [getattr(ThreadParam,n).offset for n in ('processId','addr','ctx','ctx2')] != [0,8,16,24]:
        raise RuntimeError('Luna ThreadParam ABI mismatch; inspect native build')
    if len(START_CALLBACKS)!=10:
        raise RuntimeError('Luna_Start callback ABI mismatch')
