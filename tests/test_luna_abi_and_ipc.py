import unittest
from autoadv.hook_host import parse_host_event
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

    def test_abi_definitions_remain_valid(self):
        validate_abi()
        structures = input_types()
        self.assertEqual(len(structures), 3)


if __name__ == '__main__':
    unittest.main()
