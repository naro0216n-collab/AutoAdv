#!/usr/bin/env python3
"""Adopt a coherent, pinned LunaTranslator HOOK source snapshot.

Run only on explicit adoption, never on application startup. Source files are
copied verbatim under vendor/luna, not silently modified for PySide6. A candidate
update must be reviewed, built and tested before publication.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

UPSTREAM='https://github.com/HIllya51/LunaTranslator.git'
PIN='51884b6c88cba8c42e98ce3e43c4f408017082a1'
# Native is a complete tree, not a hand-selected engine whitelist. Python
# related functionality is preserved as upstream sources (not yet loaded).
PATHS=(
    # Keep the entire native build subtree: version generator, common CMake,
    # engine and emulator libraries. Isolating LunaHook alone omits real
    # transitive inputs such as version_def.cpp and version templates.
    'src/NativeImpl/',
    'src/version.txt',
    'src/LunaTranslator/textio/',
    'src/LunaTranslator/gui/',
    'src/LunaTranslator/myutils/',
    'src/LunaTranslator/LunaSubProcess.py',
    'src/LunaTranslator/qtsymbols.py',
    'LICENSE',
)

def run(*command,cwd=None):
    return subprocess.check_output(command,cwd=cwd,stderr=subprocess.PIPE)

def archive_sources(repository:Path, revision:str, destination:Path):
    if not (len(revision)==40 and all(c in '0123456789abcdef' for c in revision)):
        raise ValueError('commit SHA must be a full 40-char hash')
    actual=run('git','rev-parse','--verify',f'{revision}^{{commit}}',cwd=repository).decode().strip()
    if actual != revision:raise RuntimeError('upstream revision mismatch')
    tar=run('git','archive','--format=tar',revision,*PATHS,cwd=repository)
    with tarfile.open(fileobj=io.BytesIO(tar)) as source:
        for entry in source.getmembers():
            location=Path(entry.name)
            if location.is_absolute() or '..' in location.parts or entry.issym() or entry.islnk():
                raise ValueError(f'unsafe source entry: {entry.name}')
            if entry.isdir():continue
            if not entry.isfile():raise ValueError(f'unsupported source entry: {entry.name}')
            target=destination / location
            target.parent.mkdir(parents=True,exist_ok=True)
            with source.extractfile(entry) as original,target.open('wb') as output:
                shutil.copyfileobj(original,output)
    return {p.relative_to(destination).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(destination.rglob('*')) if p.is_file()}

def fetch(repository:Path,revision:str):
    # No extra worktrees or development branches; throwaway upstream object db.
    run('git','init','--quiet',str(repository))
    run('git','remote','add','origin',UPSTREAM,cwd=repository)
    run('git','fetch','--quiet','--no-tags','--depth=1','origin',revision,cwd=repository)
    actual=run('git','rev-parse','FETCH_HEAD',cwd=repository).decode().strip()
    if actual!=revision:raise RuntimeError('unexpected upstream commit')

def adopt(destination:Path,*,revision:str=PIN,local_upstream:Path|None=None,write:bool=False)->dict:
    with tempfile.TemporaryDirectory(prefix='autoadv-luna-import-') as temp:
        root=Path(temp)
        upstream=local_upstream or root/'source'
        if local_upstream is None:fetch(upstream,revision)
        staged=root/'vendor'
        checksums=archive_sources(upstream,revision,staged)
        manifest={'repository':UPSTREAM,'revision':revision,'paths':list(PATHS),
                  'file_count':len(checksums),'sha256':checksums,
                  'notice':'LunaTranslator GPL-3.0; refer to LICENSE and upstream attribution'}
        existing={}
        if (destination/'manifest.json').exists():
            existing=json.loads((destination/'manifest.json').read_text(encoding='utf-8')).get('sha256',{})
        summary={'revision':revision,'files':len(checksums),
                 'added':sorted(set(checksums)-set(existing)),
                 'changed':sorted(k for k in set(checksums)&set(existing) if checksums[k]!=existing[k]),
                 'removed':sorted(set(existing)-set(checksums)),
                 'written':bool(write)}
        if write:
            # No implicit fallback or partial merge of two upstream commits.
            destination.parent.mkdir(parents=True,exist_ok=True)
            staged_manifest=staged/'manifest.json'
            staged_manifest.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            # Archive old adoption until the new coherent one is fully staged.
            previous=destination.with_name(destination.name+'.previous')
            if previous.exists():raise RuntimeError('unreviewed prior source backup exists')
            try:
                if destination.exists():destination.replace(previous)
                staged.replace(destination)
            except Exception:
                if previous.exists() and not destination.exists():previous.replace(destination)
                raise
            else:
                if previous.exists():shutil.rmtree(previous)
        return summary

def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('--commit',default=PIN)
    p.add_argument('--destination',type=Path,default=Path('vendor/luna'))
    p.add_argument('--local-upstream',type=Path,help='already fetched test repository (no network)')
    p.add_argument('--write',action='store_true')
    args=p.parse_args(argv)
    print(json.dumps(adopt(args.destination,revision=args.commit,
                           local_upstream=args.local_upstream,write=args.write),indent=2))

if __name__=='__main__':main()
