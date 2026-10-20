import unittest
from autoadv.controller import Controller,Settings
from autoadv.contracts import Envelope,EventKind
from autoadv.text import GameTextConfig,TextInterpreter
from autoadv.live import utterance_view

class LiveStatusContracts(unittest.TestCase):
    def test_active_utterance_is_controller_ownership_not_gui_reparse(self):
        interpreter=TextInterpreter(GameTextConfig(name_readings={'京':'みやこ'}))
        ctrl=Controller(Settings(),session_id='session',interpreter=interpreter)
        ctrl.accept(Envelope(EventKind.LINE_CANDIDATE,'session',10,source_seq=1,text='大和「京(みやこ)だよ」'))
        result=utterance_view(ctrl.line)
        self.assertEqual(result['observed_original'],'大和「京(みやこ)だよ」')
        self.assertEqual(result['speaker'],'大和')
        self.assertEqual(result['tts_text'],'みやこだよ')
        self.assertEqual(result['asr_target'],ctrl.line.text.asr_target)
        self.assertEqual(result['config_revision'],interpreter.config.revision)
        self.assertIsNone(utterance_view(None))
