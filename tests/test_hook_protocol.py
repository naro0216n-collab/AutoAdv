from unittest import TestCase
from autoadv.hook_protocol import HookKey, ThreadSource, HookEvent, SessionIdentity

class HookProtocolTests(TestCase):
    def test_compatibility_does_not_grant_or_remove_generation_restrictions(self):
        a=HookKey('/HS1','text',0x12340001,0x15)
        b=HookKey('/HS1','text',0x99990001,0x15)
        self.assertTrue(a.compatible(b))
        session=SessionIdentity('game','profile','host-a')
        session.bind_process_roles(hook_pids={7},audio_pids={8},input_pid=9,input_hwnd=10)
        session.choose((a,))
        src=ThreadSource(7,200,0x99990001,0x15,'/HS1','text')
        generation=session.connection_generation
        event=lambda n:HookEvent('output','host-a',generation,n,100+n,7,src,'decoded')
        self.assertFalse(session.accepts(event(1)))
        self.assertEqual(session.candidate(src),'unique')
        self.assertFalse(session.accepts(event(1)))
        self.assertTrue(session.accepts(event(2)))
        self.assertFalse(session.authorized)
        session.invalidate('host_lost')
        self.assertFalse(session.accepts(event(3)))

    def test_ambiguity_and_cross_process_events_fail_closed(self):
        a=HookKey('/HS1','text',1,2)
        s=SessionIdentity('g','p','host')
        s.bind_process_roles(hook_pids={7},audio_pids={7},input_pid=7,input_hwnd=11)
        s.choose((a,))
        first=ThreadSource(7,10,1,2,'/HS1','text')
        second=ThreadSource(7,11,1,2,'/HS1','text')
        foreign=ThreadSource(8,10,1,2,'/HS1','text')
        self.assertEqual(s.candidate(foreign),'ignored')
        self.assertEqual(s.candidate(first),'unique')
        self.assertEqual(s.candidate(second),'ambiguous')
        self.assertFalse(s.accepts(HookEvent('output','host',s.connection_generation,1,1,7,first,'text')))
        self.assertFalse(s.accepts(HookEvent('output','host',s.connection_generation,2,2,8,foreign,'text')))

    def test_role_change_forces_reacquisition(self):
        s=SessionIdentity('game','profile','host')
        s.bind_process_roles(hook_pids={2},audio_pids={3},input_pid=4,input_hwnd=5)
        prev=s.connection_generation
        s.authorized=True
        s.bind_process_roles(hook_pids={2},audio_pids={6},input_pid=4,input_hwnd=5)
        self.assertGreater(s.connection_generation,prev)
        self.assertFalse(s.authorized)
        self.assertEqual(s.audio_pids,{6})
