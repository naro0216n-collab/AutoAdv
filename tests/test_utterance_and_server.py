import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock
from autoadv.text import TextInterpreter,GameTextConfig,StructureRule,TextReplacement
from autoadv.contracts import ContentKind,TTSPolicy
from autoadv.irodori_server import local_voice_ids, IrodoriServer

class UtteranceContract(TestCase):
    def test_shared_data_and_readings(self):
        cfg=GameTextConfig(name_readings={'京':'みやこ'},speaker_routes={'大和':(TTSPolicy.SKIP,None)},
                           replacements=(TextReplacement('おはよう','おはよー'),))
        parse=TextInterpreter(cfg).parse
        line=parse('大和\n「京(みやこ)、おはよう」')
        self.assertEqual((line.speaker,line.body,line.tts_text,line.asr_target,line.tts_route),
                         ('大和','京(みやこ)、おはよう','みやこ、おはよー','京(みやこ)、おはよう',TTSPolicy.SKIP))
        self.assertEqual(parse('東京へ京が行く').tts_text,'東京へみやこが行く')
        self.assertEqual(parse('今日は(ため息)です').tts_text,'今日は(ため息)です')
        self.assertEqual(parse('「……」').content_kind,ContentKind.SYMBOLIC)
        self.assertEqual(parse('「……」').tts_text,'')
    def test_rules_priority_and_validation(self):
        cfg=GameTextConfig(structure_rules=(StructureRule('one',r'(?P<body>.+)'),StructureRule('two',r'(?P<body>.+)')))
        self.assertEqual(TextInterpreter(cfg).parse('x').conflicts,('two',))
        with self.assertRaises(ValueError):StructureRule('wrong',r'(.*)')
        with self.assertRaises(ValueError):TextReplacement('[','',regex=True)

class IrodoriContract(TestCase):
    def test_local_candidates_do_not_descend_old(self):
        with tempfile.TemporaryDirectory() as path:
            root=Path(path);v=root/'voices';v.mkdir();(v/'old').mkdir();(v/'old'/'obsolete.wav').write_bytes(b'')
            for file in ('a.wav','b.speaker.safetensors','c.pth','invalid.txt'):(v/file).write_bytes(b'')
            (v/'voices.json').write_text(json.dumps({'alias':{'ref_wavs':['a.wav']}}))
            self.assertEqual(local_voice_ids(root),('a','alias','b','c'))
    def test_external_server_not_owned(self):
        def opener(req,timeout):
            body=(b'{"status":"ok","model":{},"runtime":{},"voices":{}}'
                  if req.full_url.endswith('/health') else b'{"data":[{"id":"voice"}]}')
            return type('Response',(),{'__enter__':lambda s:s,'__exit__':lambda *a:None,
                                        'read':lambda s:body})()
        mgr=IrodoriServer(opener=opener,process_factory=Mock(side_effect=AssertionError('spawn')))
        self.assertEqual(mgr.connect_or_start().origin,'external')
        mgr.close()

class ServerStartupOwnership(TestCase):
    def test_selected_folder_bootstrap_and_owned_termination(self):
        from unittest.mock import Mock,patch
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'pyproject.toml').write_text('[project]\nname="irodori"')
            exe=root/'.venv'/'Scripts'/'python.exe'
            exe.parent.mkdir(parents=True);exe.write_bytes(b'')
            connected=False
            class Reply:
                def __init__(self,payload):self.payload=payload
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def read(self):return json.dumps(self.payload).encode()
            def opener(req,timeout):
                if not connected:raise __import__('urllib').error.URLError('not started')
                if req.full_url.endswith('/health'):
                    return Reply({'status':'ok','model':{},'runtime':{},'voices':{}})
                return Reply({'data':[{'id':'voice_A'}]})
            spawned=Mock()
            spawned.poll.return_value=None
            def launch(argv,**kwargs):
                nonlocal connected
                connected=True
                self.assertTrue(Path(argv[0]).samefile(exe))
                self.assertEqual(argv[1:3],['-m','irodori_openai_tts'])
                self.assertTrue(Path(kwargs['cwd']).samefile(root))
                return spawned
            from unittest.mock import patch
            manager=IrodoriServer(root,opener=opener,process_factory=launch)
            with patch('autoadv.irodori_server.socket.socket') as sock:
                sock.return_value.__enter__.return_value.connect_ex.return_value=111
                self.assertEqual(manager.connect_or_start(timeout_sec=.5).voices,('voice_A',))
            self.assertEqual(manager.state.origin,'owned')
            manager.connect_or_start()
            manager.close()
            spawned.terminate.assert_called_once()
            spawned.wait.assert_called_once()

    def test_port_with_other_service_cannot_launch_or_take_ownership(self):
        from unittest.mock import Mock
        def opener(req,timeout):
            return type('Reply',(),{'__enter__':lambda s:s,'__exit__':lambda *a:None,
                  'read':lambda s:b'{"status":"ok"}'})()
        proc=Mock(side_effect=AssertionError('must not start an unverified service'))
        manager=IrodoriServer(opener=opener,process_factory=proc)
        self.assertEqual(manager.connect_or_start().status,'other_service')
        manager.close()
        proc.assert_not_called()

    def test_read_alias_location_from_official_setting(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'voices').mkdir()
            (root/'.env').write_text('IRODORI_VOICE_ALIASES_FILE=alias.json\n',encoding='utf-8')
            (root/'alias.json').write_text('{"voice_alias":"v.wav"}',encoding='utf-8')
            self.assertIn('voice_alias',local_voice_ids(root))

class VoiceAssignmentValidation(TestCase):
    def test_default_routing_requires_a_real_configured_default(self):
        with self.assertRaises(ValueError):
            GameTextConfig(speaker_routes={'大和':(TTSPolicy.DEFAULT,None)})
        with self.assertRaises(ValueError):
            GameTextConfig(unknown_route=TTSPolicy.SELECTED)
        config=GameTextConfig(speaker_routes={'大和':(TTSPolicy.DEFAULT,None)},default_voice='existing_id')
        self.assertEqual(TextInterpreter(config).parse('大和「本文」').voice_id,'existing_id')
