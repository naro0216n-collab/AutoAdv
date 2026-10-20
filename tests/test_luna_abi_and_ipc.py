import ctypes
from unittest import TestCase
from autoadv import luna_abi
from autoadv.hook_host import parse_host_event

class HostContractTests(TestCase):
    def test_abi_uses_all_ten_callbacks(self):
        self.assertEqual(len(luna_abi.START_CALLBACKS),10)
        self.assertEqual(ctypes.sizeof(luna_abi.ThreadParam),32)
        self.assertEqual([getattr(luna_abi.ThreadParam,k).offset for k in ('processId','addr','ctx','ctx2')],[0,8,16,24])
        class Method:
            pass
        class DLL:
            def __getattr__(self,name):
                if name not in self.__dict__:setattr(self,name,Method())
                return object.__getattribute__(self,name)
        dll=luna_abi.load_exports(DLL())
        self.assertEqual(len(dll.Luna_Start.argtypes),10)
        self.assertEqual(len(dll.Luna_FindHooks.argtypes),4)

    def test_provenance_preserved_through_ipc(self):
        p={'kind':'output','host_generation':'abc','connection_generation':3,
           'sequence':2,'received_ns':150,'process_id':123,
           'source':{'process_id':123,'address':5,'context':4,'context2':6,
                    'hook_code':'/HS1','hook_name':'text'},'text':'日本語','detail':''}
        event=parse_host_event(p)
        self.assertEqual(event.text,'日本語')
        self.assertEqual(event.source.key.code,'/HS1')
        p['process_id']=999
        with self.assertRaises(ValueError):parse_host_event(p)
        with self.assertRaises(RuntimeError):parse_host_event({'kind':'fatal','reason':'DLL not found'})
