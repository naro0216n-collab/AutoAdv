"""Isolated upstream LunaHost/Hook subprocess. Never controls game input."""
from __future__ import annotations
import argparse
import ctypes
import json
import os
import sys
import time
import threading
import uuid
from pathlib import Path
from .luna_runtime import LunaRuntime, inject, is_process_64bit
from .luna_abi import (ThreadParam,SearchParam,PROCESS,CREATE,THREAD,OUTPUT,INFO,
                       INSERT,EMBED,I18N,EMU,FIND,HISTORY,load_exports,validate_abi)

class NativeHost:
    def __init__(self,dll_path:Path,send):
        self.send=send
        self.generation=uuid.uuid4().hex
        self.connection_generation=0
        self.sequence=0
        self.lock=threading.Lock()
        self.callbacks=[]
        self.connected=set()
        self.managed=set()
        self.find_callbacks=[]
        self.dll_path=dll_path
        self.runtime = LunaRuntime(dll_path.parent)
        self.runtime.verify()
        if sys.platform!='win32':raise OSError('LunaHost requires Windows')
        validate_abi()
        if not dll_path.is_file():raise FileNotFoundError(dll_path)
        self.dll=load_exports(ctypes.CDLL(str(dll_path)))

    def emit(self,kind,*,pid=None,tp=None,code='',name='',text='',detail=''):
        with self.lock:
            self.sequence+=1
            src=None
            if tp is not None:
                src={'process_id':int(tp.processId),'address':int(tp.addr),
                     'context':int(tp.ctx),'context2':int(tp.ctx2),
                     'hook_code':code or '', 'hook_name':name or ''}
            packet={'kind':kind,'host_generation':self.generation,
                    'connection_generation':self.connection_generation,
                    'sequence':self.sequence,'received_ns':time.monotonic_ns(),
                    'process_id':int(pid) if pid is not None else (src['process_id'] if src else None),
                    'source':src,'text':text or '', 'detail':detail or ''}
            self.send(packet)

    def start(self):
        def name(raw):
            return raw.decode('utf-8','replace') if raw else ''
        self.callbacks=[
            PROCESS(lambda pid:self._connected(pid)),
            PROCESS(lambda pid:self._disconnected(pid)),
            CREATE(lambda code,n,tp,emb:self.emit('created',tp=tp,code=code,name=name(n),detail=str(bool(emb)))),
            THREAD(lambda code,n,tp:self.emit('removed',tp=tp,code=code,name=name(n))),
            OUTPUT(lambda code,n,tp,s:self.emit('output',tp=tp,code=code,name=name(n),text=s)),
            INFO(lambda t,s:self.emit('diagnostic',detail=f'{t}: {s}')),
            INSERT(lambda pid,address,code:self.emit('inserted',pid=pid,code=code,detail=str(address))),
            EMBED(lambda s,tp:self.emit('diagnostic',tp=tp,detail='embed_callback_ignored')),
            I18N(lambda s:0), # Null -> no translation; never free Python memory from native.
            EMU(lambda game,title,version:self.emit('emulator',text=game,detail=json.dumps({'title':title,'version':version},ensure_ascii=False))),
        ]
        self.dll.Luna_Start(*self.callbacks)
        self.dll.Luna_Settings(500,932,3000,1000000,False)
        self.codepage=932
        self.emit('diagnostic',detail='native_host_ready')

    def _connected(self,pid):
        if pid in self.managed:
            self.connected.add(pid)
            self.emit('connected',pid=pid)
        else:
            self.emit('diagnostic',pid=pid,detail='unmanaged_connection_ignored')

    def _disconnected(self,pid):
        self.connected.discard(pid)
        self.managed.discard(pid)
        self.emit('disconnected',pid=pid)

    def command(self,message):
        action=message.get('action')
        pid=message.get('pid')
        if action=='settings':
            values = tuple(message[key] for key in ('flush_ms', 'codepage', 'max_buffer', 'max_history'))
            if (any(type(v) is not int for v in values) or not 0 <= values[0] <= 10000
                    or not 0 <= values[1] <= 65535 or not 1 <= values[2] <= 1000000
                    or not 1 <= values[3] <= 10000000):
                raise ValueError('invalid Luna text thread settings')
            self.dll.Luna_Settings(*values, False)
            self.codepage=values[1]
        elif action=='connect':
            if type(pid) is not int or pid<=0:raise ValueError('positive pid required')
            if pid in self.managed:
                raise ValueError('PID already managed')
            # Preflight BEFORE creating the upstream Named Pipes. If a Luna
            # hook is already injected, never attach, detach or overwrite it.
            if not self.dll.Luna_CheckIfNeedInject(pid):
                reason = ('cannot_attach_to_host_process' if pid == os.getpid()
                          else 'existing_luna_injection_detected')
                self.emit('diagnostic',pid=pid,detail=reason)
                return True
            self.connection_generation+=1
            self.managed.add(pid)
            self.dll.Luna_ConnectProcess(pid)
            try:
                inject(pid, self.runtime)
            except Exception:
                self.managed.discard(pid)
                self.connection_generation += 1
                raise
            self.emit('diagnostic',pid=pid,detail='upstream_injection_requested')
        elif action=='detach':
            if type(pid) is not int or pid<=0:raise ValueError('positive pid required')
            # Detach only a connection that this Host actually observed.
            if pid in self.managed and pid in self.connected:self.dll.Luna_DetachProcess(pid)
            self.managed.discard(pid)
            self.connected.discard(pid)
            self.connection_generation+=1
            self.emit('diagnostic',pid=pid,detail='detached')
        elif action=='insert_pc_hooks':
            if type(pid) is not int or pid not in self.connected:
                raise ValueError('PC hook insertion requires attached process')
            # Use upstream Luna's two existing PC API families. This does not
            # select a text source or infer game-specific hook codes.
            self.dll.Luna_InsertPCHooks(pid, 0)
            self.dll.Luna_InsertPCHooks(pid, 1)
            self.emit('diagnostic',pid=pid,detail='upstream_pc_hooks_requested')
        elif action=='insert_hook':
            if type(pid) is not int or pid not in self.connected or not isinstance(message.get('code'),str) or not message['code']:
                raise ValueError('requires attached pid and nonempty code')
            self.dll.Luna_InsertHookCode(pid,message['code'])
        elif action=='sync_thread':
            src=message['source']
            tp=ThreadParam(src['process_id'],src['address'],src['context'],src['context2'])
            if tp.processId not in self.connected:raise ValueError('thread is not attached')
            self.dll.Luna_SyncThread(tp,bool(message.get('enabled',True)))
        elif action=='find_hooks':
            if type(pid) is not int or pid not in self.connected:raise ValueError('search requires attached process')
            parameters=message.get('parameters') or {}
            valid={field for field,_ in SearchParam._fields_}
            if set(parameters)-valid:raise ValueError('invalid hook search parameter')
            sp=SearchParam()
            # The 64-bit Host may be attached to a 32-bit game. Match the
            # search pattern and address range to the *target*, as Luna does.
            target_64bit = is_process_64bit(pid)
            sp.pattern=(bytes([0xCC,0xCC,0x48,0x89]) if target_64bit
                        else bytes([0x55,0x8B,0xEC]))
            sp.length=4 if target_64bit else 3
            sp.offset=2 if target_64bit else 0
            sp.maxAddress=0xFFFFFFFFFFFFFFFF if target_64bit else 0xFFFFFFFF
            sp.searchTime=30000
            sp.maxRecords=100000
            sp.codepage=self.codepage
            module=message.get('boundary_module', '')
            if not isinstance(module, str) or len(module)>119 or ('\\' in module or '/' in module):
                raise ValueError('search boundary_module must be an executable basename')
            sp.boundaryModule=module
            for field,value in parameters.items():
                setattr(sp,field,value)
            def found(code,sample):
                self.emit('hook_found',pid=pid,text=sample,detail=code)
            cb=FIND(found)
            self.find_callbacks.append(cb)  # Native search may finish asynchronously.
            self.dll.Luna_FindHooks(pid,sp,cb,message.get('addresses',''))
        elif action=='history':
            source=message['source']
            tp=ThreadParam(source['process_id'],source['address'],source['context'],source['context2'])
            cb=HISTORY(lambda s:self.emit('diagnostic',pid=tp.processId,detail='history',text=s))
            self.dll.Luna_QueryThreadHistory(tp,bool(message.get('latest',True)),cb)
        elif action=='stop':
            for connected_pid in tuple(self.connected & self.managed):
                self.dll.Luna_DetachProcess(connected_pid)
            self.connected.clear()
            self.managed.clear()
            return False
        else:raise ValueError('unknown native Host command')
        return True

def ipc_json(packet: dict) -> str:
    """Keep the worker-to-GUI pipe ASCII-clean on CP932 Windows consoles.

    The parent decodes the JSON back to Unicode. Native callbacks must never
    throw merely because an upstream warning contains non-Japanese glyphs.
    """
    return json.dumps(packet, ensure_ascii=True)


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--dll',required=True,type=Path)
    args=parser.parse_args(argv)
    out_lock=threading.Lock()
    def send(obj):
        with out_lock:
            print(ipc_json(obj),flush=True)
    try:
        host=NativeHost(args.dll,send)
        host.start()
        for line in sys.stdin:
            try:
                if not host.command(json.loads(line)):break
            except Exception as error:
                host.emit('diagnostic',detail=f'command_failed: {type(error).__name__}: {error}')
        return 0
    except Exception as error:
        print(ipc_json({'kind':'fatal','reason':f'{type(error).__name__}: {error}'}),flush=True)
        return 1

if __name__=='__main__':raise SystemExit(main())
