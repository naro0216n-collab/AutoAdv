"""Game-specific *verified* voice-asset evidence, tied to an observed line.

The generic runtime never interprets filename patterns or a VAD-negative as
proof of no native voice. An optional game adapter may report a real asset
assignment lookup through this strictly fenced loopback contract. A missing,
uncertain or mismatched report does not authorize silent progression.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
import hmac
import hashlib
from .application import MetadataVoice
from .controller import Line


@dataclass(frozen=True)
class AssetReport:
    adapter_seq: int
    session_id: str
    epoch: int
    token: int
    source_line_id: str
    assigned: bool
    asset_id: str | None
    # An adapter must have *verified* the actual game's current-line asset
    # assignment; not a character's historic voiced/unvoiced preference.
    proof: str


def signed_asset_packet(payload:dict, secret:bytes) -> bytes:
    """For a user-supplied game adapter; bind the signed identity to one line."""
    if not isinstance(secret,bytes) or len(secret)<16:
        raise ValueError('game metadata signing key must be at least 16 bytes')
    raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
    signature=hmac.new(secret,raw,hashlib.sha256).hexdigest()
    return json.dumps({**payload,'signature':signature},ensure_ascii=False).encode('utf-8')


def decode_asset_report(data: bytes, secret:bytes) -> AssetReport:
    if len(data)>4096:
        raise ValueError('metadata report too large')
    if not isinstance(secret,bytes) or len(secret)<16:
        raise ValueError('game metadata signing key must be at least 16 bytes')
    packet=json.loads(data)
    expected={'schema_version','adapter_seq','session_id','epoch','token',
              'source_line_id','assigned','asset_id','proof','signature'}
    if not isinstance(packet,dict) or set(packet)!=expected or packet['schema_version']!=1:
        raise ValueError('invalid game asset schema')
    signed={k:v for k,v in packet.items() if k!='signature'}
    expected_hmac=hmac.new(secret,json.dumps(signed,ensure_ascii=False,
        sort_keys=True,separators=(',',':')).encode('utf-8'),hashlib.sha256).hexdigest()
    if not isinstance(packet['signature'],str) or not hmac.compare_digest(packet['signature'],expected_hmac):
        raise ValueError('unauthenticated game metadata')
    if (type(packet['adapter_seq']) is not int or packet['adapter_seq']<=0 or
        type(packet['epoch']) is not int or packet['epoch']<=0 or
        type(packet['token']) is not int or packet['token']<=0 or
        type(packet['assigned']) is not bool or
        not isinstance(packet['session_id'],str) or not packet['session_id'] or
        not isinstance(packet['source_line_id'],str) or not packet['source_line_id'] or
        packet['proof']!='game_current_line_asset_lookup'):
        raise ValueError('unverified game asset evidence')
    aid=packet['asset_id']
    if ((packet['assigned'] and (not isinstance(aid,str) or not aid)) or
        (not packet['assigned'] and aid is not None)):
        raise ValueError('inconsistent game asset assignment')
    return AssetReport(packet['adapter_seq'],packet['session_id'],packet['epoch'],
                       packet['token'],packet['source_line_id'],packet['assigned'],aid,packet['proof'])


class VoiceMetadataInbox:
    def __init__(self,shared_key:bytes):
        if not isinstance(shared_key,bytes) or len(shared_key)<16:
            raise ValueError('a trusted game-adapter shared key is required')
        self._key=shared_key
        self._last_seq=0
        self._accepted:dict[tuple[str,int,int,str],MetadataVoice]={}

    def accept(self, raw:bytes, session_id:str, current:Line | None) -> bool:
        packet=decode_asset_report(raw,self._key)
        if packet.adapter_seq<=self._last_seq:
            return False
        self._last_seq=packet.adapter_seq
        if (current is None or packet.session_id!=session_id or
            packet.epoch!=current.epoch or packet.token!=current.token or
            packet.source_line_id!=current.source_line_id):
            return False
        self._accepted.clear()
        key=(packet.session_id,packet.epoch,packet.token,packet.source_line_id)
        self._accepted[key]=MetadataVoice(packet.assigned,packet.asset_id,verified=True)
        return True

    def for_line(self, session_id:str, line:Line | None) -> MetadataVoice | None:
        if line is None:return None
        return self._accepted.get((session_id,line.epoch,line.token,line.source_line_id))
