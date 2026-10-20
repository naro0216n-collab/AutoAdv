import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from autoadv.profiles import GameCatalog,GameProfile,GameRecord,import_luna
from autoadv.hook_protocol import HookKey

class CatalogContracts(TestCase):
    def test_names_are_not_keys_and_multi_profile_order_roundtrip(self):
        c=GameCatalog();one=c.create_game('同名',executable=r'C:\games\a.exe')
        two=c.create_game('同名',executable=r'C:\games\b.exe')
        self.assertNotEqual(one.game_id,two.game_id)
        selected=c.duplicate(one.game_id,one.default_profile_id,'別HOOK')
        selected.selected_hooks=[{'code':'/HS1','name':'text','context':255,'context2':4},
                                 {'code':'/HS2','name':'speaker','context':1,'context2':2}]
        selected.validate()
        c.choose(one.game_id,selected.profile_id)
        serialized=c.to_dict();other=GameCatalog.from_dict(json.loads(json.dumps(serialized)))
        self.assertEqual(other.active()[1].keys(),(HookKey('/HS1','text',255,4),HookKey('/HS2','speaker',1,2)))
        other.delete_profile(one.game_id,selected.profile_id)
        self.assertNotEqual(other.active_profile_id,selected.profile_id)
        self.assertEqual(other.games[one.game_id].display_name,'同名')

    def test_ambiguous_path_is_not_auto_selected(self):
        c=GameCatalog();a=c.create_game('emua',executable='emu.exe',emulator_game_id='game-a')
        b=c.create_game('emub',executable='emu.exe',emulator_game_id='game-b')
        self.assertEqual([x.game_id for x in c.candidates(process_path='emu.exe')],[])
        self.assertEqual([x.game_id for x in c.candidates(process_path='emu.exe',emulator_id='game-a')],[a.game_id])
        c.create_game('same1',executable='same.exe')
        c.create_game('same2',executable='same.exe')
        self.assertEqual(len(c.candidates(process_path='same.exe')),2)

    def test_runtime_pid_and_input_authority_are_never_persisted(self):
        c=GameCatalog();p=c.create_game('g').profiles[c.active_profile_id]
        p.monitor={'pid':5}
        with self.assertRaises(ValueError):c.to_dict()
        p.monitor={};p.overrides={'authorized':True}
        with self.assertRaises(ValueError):c.to_dict()

    def test_import_luna_preserves_all_selected_hook_order_and_original(self):
        with TemporaryDirectory() as td:
            file=Path(td)/'savegamedata_5.3.1.json'
            data=[['uid-a','uid-b'],{
                'uid-a':{'gamepath':'C:/game/a.exe','hook':[[111,222,333,444,'voice','/HS1'],[1,2,3,4,'body','/HS2']],
                         'needinserthookcode':['/HS3'],'save_text_process_info':{'rank':['trim'] ,'postprocessconfig':{}}},
                'uid-b':{'gamepath':'C:/game/b.exe','hook':[]}},[None],None,{}]
            file.write_text(json.dumps(data),encoding='utf-8')
            c=GameCatalog();mapping=import_luna(c,file)
            self.assertEqual(set(mapping),{'uid-a','uid-b'})
            game=c.games[mapping['uid-a']]
            p=game.profiles[game.default_profile_id]
            self.assertEqual([x.code for x in p.keys()],['/HS1','/HS2'])
            self.assertEqual(p.inserted_hooks,['/HS3'])
            self.assertEqual(p.quality['rank'],['trim'])
            self.assertEqual(p.selected_hooks,[
                {'code':'/HS1','name':'voice','context':333,'context2':444},
                {'code':'/HS2','name':'body','context':3,'context2':4},
            ])
            # PID and absolute hook addresses are not part of the HookKey
            # storage contract. Random stable game/profile IDs are unrelated.
            for hook in p.selected_hooks:
                self.assertEqual(set(hook),{'code','name','context','context2'})
            before=c.to_dict()
            file.write_text('[]',encoding='utf-8')
            with self.assertRaises(ValueError):import_luna(c,file)
            self.assertEqual(c.to_dict(),before)
