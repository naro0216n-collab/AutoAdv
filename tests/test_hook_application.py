from unittest import TestCase
from autoadv.hook_application import HookApplication
from autoadv.hook_protocol import HookKey, ThreadSource, HookEvent


def event(kind, seq, gen=2, pid=50, source=None, text='',detail=''):
    return HookEvent(kind,'host-a',gen,seq,1000+seq,pid,source,text,detail)


class HookApplicationContracts(TestCase):
    def setUp(self):
        self.received=[]
        self.invalid=[]
        self.a=HookApplication('stable-game-id','profile-id',self.received.append,self.invalid.append)
        self.a.update_roles(hook_pids={50},audio_pids={51},input_pid=52,input_hwnd=500)
        self.a.select((HookKey('A','text',0x10,0x20),))
        self.a.handle(event('diagnostic',1,0,None,detail='native_host_ready'))
        self.a.handle(event('connected',2))
        self.source=ThreadSource(50,1234,0x10,0x20,'A','text')
        self.a.handle(event('created',3,source=self.source))

    def test_source_created_and_output_only_deliver_after_unique_selection(self):
        self.a.handle(event('output',4,source=self.source,text='大和「こんにちは」'))
        self.assertEqual([x.text for x in self.received],['大和「こんにちは」'])
        self.assertEqual(self.a.status().state,'observing')
        self.assertFalse(self.a.session.authorized)

    def test_ambiguous_match_invalidates_and_stops_all_output(self):
        second=ThreadSource(50,9999,0x10,0x20,'A','text')
        self.a.handle(event('created',4,source=second))
        self.a.handle(event('output',5,source=self.source,text='wrong'))
        self.assertEqual(self.received,[])
        self.assertIn('ambiguous_restored_hook',self.invalid)

    def test_old_generation_foreign_process_and_host_failure_cannot_advance(self):
        self.a.handle(event('output',4,gen=1,source=self.source,text='old'))
        foreign=ThreadSource(80,1234,0x10,0x20,'A','text')
        self.a.handle(event('output',5,pid=80,source=foreign,text='foreign'))
        self.a.host_closed('IPC EOF')
        self.a.handle(event('output',6,source=self.source,text='after'))
        self.assertEqual(self.received,[])
        self.assertEqual(self.a.status().state,'host_closed')

    def test_multi_hook_output_never_guesses_pairing(self):
        self.a.select((HookKey('A','text',0x10,0x20),HookKey('B','speaker',1,2)))
        other=ThreadSource(50,444,1,2,'B','speaker')
        self.a.handle(event('created',4,source=other))
        self.a.handle(event('output',5,source=self.source,text='本文'))
        self.a.handle(event('output',6,source=other,text='話者'))
        self.assertEqual(self.received,[])
        self.assertEqual(self.a.status().state,'preview_only')

    def test_selection_change_drops_active_input_and_uses_existing_native_candidate(self):
        self.a.handle(event('output',4,source=self.source,text='old'))
        self.a.select((HookKey('A','text',0x10,0x20),))
        self.a.handle(event('output',5,source=self.source,text='next'))
        self.assertEqual([x.text for x in self.received],['old','next'])
        self.assertFalse(self.a.session.authorized)
        self.assertIn('hook_selection_changed',self.invalid)
