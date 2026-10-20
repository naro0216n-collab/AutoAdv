"""Subprocess owning LunaHost.dll; NDJSON IPC, never controller or input.

Injection uses the pinned upstream Host implementation, enabled in the
verifiable build copy. Collision with another Host stops before pipe creation.
"""
from __future__ import annotations
import argparse
import ctypes
import json
import sys
import time
import threading
import uuid
from pathlib import Path
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
        self.dll.Luna_Settings(500,932,3000,100000,False)
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
        if action=='connect':
            if type(pid) is not int or pid<=0:raise ValueError('positive pid required')
            if pid in self.managed:
                raise ValueError('PID already managed')
            # Preflight BEFORE creating the upstream Named Pipes. If a Luna
            # hook is already injected, never attach, detach or overwrite it.
            if not self.dll.Luna_CheckIfNeedInject(pid):
                self.emit('diagnostic',pid=pid,detail='injection_conflict_or_self')
                return True
            self.connection_generation+=1
            self.managed.add(pid)
            self.dll.Luna_ConnectAndInjectProcess(pid)
            self.emit('diagnostic',pid=pid,detail='native_injection_requested')
        elif action=='detach':
            if type(pid) is not int or pid<=0:raise ValueError('positive pid required')
            # Detach only a connection that this Host actually observed.
            if pid in self.managed and pid in self.connected:self.dll.Luna_DetachProcess(pid)
            self.managed.discard(pid)
            self.connected.discard(pid)
            self.connection_generation+=1
            self.emit('diagnostic',pid=pid,detail='detached')
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
            sp.pattern=bytes([0xCC,0xCC,0x48,0x89]) if ctypes.sizeof(ctypes.c_void_p)==8 else bytes([0x55,0x8B,0xEC])
            sp.length=4 if ctypes.sizeof(ctypes.c_void_p)==8 else 3
            sp.offset=2 if ctypes.sizeof(ctypes.c_void_p)==8 else 0
            sp.maxAddress=(1<<(8*ctypes.sizeof(ctypes.c_void_p)))-1
            sp.searchTime=30000
            sp.maxRecords=100000
            sp.codepage=932
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

def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--dll',required=True,type=Path)
    args=parser.parse_args(argv)
    out_lock=threading.Lock()
    def send(obj):
        with out_lock:
            print(json.dumps(obj,ensure_ascii=False),flush=True)
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
        print(json.dumps({'kind':'fatal','reason':f'{type(error).__name__}: {error}'}),flush=True)
        return 1

if __name__=='__main__':raise SystemExit(main())
