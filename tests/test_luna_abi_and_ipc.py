import unittest
import io
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace
from autoadv.hook_host_worker import NativeHost
from autoadv.luna_abi import load_exports
from autoadv.hook_host import parse_host_event, HookHost
from autoadv.luna_abi import validate_abi
from autoadv.windows_input import input_types


class NativeBoundary(unittest.TestCase):
    def test_native_packet_requires_provenance(self):
        packet = dict(kind='output', host_generation='h', connection_generation=1,
                      sequence=2, received_ns=5, process_id=31,
                      source=dict(process_id=31, address=5, context=3, context2=4,
                                  hook_code='AA', hook_name='text'), text='Hello')
        event = parse_host_event(packet)
        self.assertEqual(event.source.identity, 'AA|3|4')
        packet['process_id'] = 32
        with self.assertRaises(ValueError):
            parse_host_event(packet)

    def test_upstream_pc_hook_insertion_uses_both_native_families(self):
        # The native helper must request Luna's existing hooks, not invent any
        # text source or select a thread from the result.
        host = NativeHost.__new__(NativeHost)
        host.connected = {31}
        host.dll = SimpleNamespace(Luna_InsertPCHooks=Mock())
        host.emit = Mock()
        self.assertTrue(host.command({'action': 'insert_pc_hooks', 'pid': 31}))
        self.assertEqual(host.dll.Luna_InsertPCHooks.call_args_list,
                         [unittest.mock.call(31, 0), unittest.mock.call(31, 1)])
        host.emit.assert_called_once_with('diagnostic', pid=31,
                                          detail='upstream_pc_hooks_requested')
        with self.assertRaisesRegex(ValueError, 'attached process'):
            host.command({'action': 'insert_pc_hooks', 'pid': 32})
        self.assertEqual(host.dll.Luna_InsertPCHooks.call_count, 2)

    def test_upstream_native_pc_hook_api_is_bound(self):
        dll = SimpleNamespace(**{name: Mock() for name in (
            'Luna_Start', 'Luna_ConnectProcess', 'Luna_DetachProcess',
            'Luna_CheckIfNeedInject', 'Luna_InsertHookCode',
            'Luna_InsertPCHooks', 'Luna_RemoveHook', 'Luna_Settings',
            'Luna_QueryThreadHistory', 'Luna_FindHooks', 'Luna_SyncThread')})
        load_exports(dll)
        self.assertEqual(len(dll.Luna_InsertPCHooks.argtypes), 2)

    def test_ctypes_callback_failures_are_not_discarded(self):
        messages = []
        host = HookHost(Path('LunaHost64.dll'), lambda _: None, lambda _: None,
                        messages.append)
        host.child = SimpleNamespace(stderr=io.StringIO('callback failed\ntraceback line\n'))
        host._read_errors()
        self.assertEqual(messages, ['callback failed', 'traceback line'])

    def test_abi_definitions_remain_valid(self):
        validate_abi()
        structures = input_types()
        self.assertEqual(len(structures), 3)


if __name__ == '__main__':
    unittest.main()
