"""R13 lifecycle adapter calibrated from new, recorded UI observations.

P1 readback covers an empty design's visible entity counts. It does not claim
material, solver results or arbitrary model persistence have been inspected.
"""
from __future__ import annotations
import json
import re
import time
import uuid
from dataclasses import replace
from pathlib import Path
import psutil
from PIL import ImageGrab
from . import native
from .session import connect
from ..models import Design, BridgeError
from ..supervisor import Supervisor, WriterLease

NEW='main_window.m_topDockWidget.topWidget.DesignBarViewClass.m_newDesignBtn'
WELCOME='main_window.AFWelcomeDialogClass.m_mainFrame.m_bottomFrame.m_closePB'

def unique(records, **selector):
    found=[item for item in records if all(item.get(k)==v for k,v in selector.items())]
    if len(found)!=1:
        raise BridgeError('ambiguous_control','Expected one current control',selector=selector,count=len(found))
    return found[0]

def active_title_matches(title,name):
    return re.match(r'^AutoForm Forming R13 - '+re.escape(name)+r'(?:\.afd)? ',title) is not None

def empty_counts(controls, window_rect):
    """Require explicit displayed counts, not absence of geometric controls."""
    patterns={'parts':r'(\d+) 零件\(s\)', 'tools':r'(\d+) 模具\(s\)',
              'coordinate_systems':r'(\d+) 坐标系\(s\)', 'curves_points':r'(\d+) 曲线\(s\), (\d+) 点\(s\)'}
    values={}
    l,t,r,b=window_rect
    for key,pattern in patterns.items():
        hits=[]
        for item in controls:
            x1,y1,x2,y2=item['rect']
            if item['kind']=='TextControl' and l<=x1<x2<=r and t<=y1<y2<=b and item['enabled']:
                match=re.fullmatch(pattern,item['name'])
                if match: hits.append(tuple(map(int,match.groups())))
        if len(hits)!=1:
            raise BridgeError('readback_unavailable','Expected one explicit visible entity count',field=key,count=len(hits))
        values[key]=list(hits[0]) if key=='curves_points' else hits[0][0]
    if any(value!=0 for key,value in values.items() if key!='curves_points') or values['curves_points']!=[0,0]:
        raise BridgeError('scenario_not_verified','P1 currently verifies only empty designs',counts=values)
    return {'scenario':'empty_design', 'entity_counts':values}

class WindowsAdapter:
    def __init__(self,evidence_dir:Path,*,launch=False,license_server=None,allow_selected_license=False):
        self.directory=Path(evidence_dir).resolve()
        self.directory.mkdir(parents=True,exist_ok=True)
        self.session=connect(self.directory,launch,license_server,allow_selected_license=allow_selected_license)
        self.pid,self.hwnd=self.session['pid'],self.session['hwnd']
        self.lease=WriterLease(self.pid).acquire()
        try:
            self.supervisor=Supervisor(self.directory)
        except BaseException:
            self.lease.release()
            raise
        self.session_id=uuid.uuid4().hex
        self.entries={}
        self.live_rids={}
        self._owned_tokens=set()
        self._saved={}
        self._closed=False
        self._last_title=''

    def _windows(self):
        if psutil.Process(self.pid).create_time()!=self.session['create_time']:
            raise BridgeError('stale_process','R13 process identity changed')
        return native.windows(self.pid)

    def _call(self,action,*,hwnd=None,**request):
        if self._closed: raise BridgeError('disconnected','Adapter has disconnected')
        self._windows()
        return self.supervisor.run_worker({'action':action,'pid':self.pid,'hwnd':hwnd or self.hwnd,**request})

    def _dump(self,hwnd=None):
        return self._call('dump',hwnd=hwnd,depth=32)['controls']

    def _unexpected(self,windows):
        self.capture('unexpected-modal')
        for window in windows:
            self._call('dump',hwnd=window['hwnd'])
        raise BridgeError('needs_user','Unexpected modal; no automatic save/discard',windows=windows)

    def _idle(self,*,dismiss_welcome=False):
        windows=self._windows()
        main=unique(windows,hwnd=self.hwnd)
        extra=[w for w in windows if w['hwnd']!=self.hwnd]
        if extra and all(self._save_render_window(w) for w in extra):
            deadline=time.monotonic()+5
            while extra and time.monotonic()<deadline:
                time.sleep(.2)
                extra=[w for w in self._windows() if w['hwnd']!=self.hwnd]
            if not extra: return self._idle(dismiss_welcome=dismiss_welcome)
        if extra:
            if len(extra)==1 and extra[0]['owner']==self.hwnd:
                controls=self._dump(extra[0]['hwnd'])
                if controls[0]['class']=='AutoForm::AFWelcomeDialog':
                    if dismiss_welcome:
                        unique(controls,aid=WELCOME)
                        self._call('click',hwnd=extra[0]['hwnd'],selector={'aid':WELCOME})
                        return self._idle()
                    return main
            self._unexpected(extra)
        if not main['enabled']: raise BridgeError('busy','Main window is disabled')
        return main

    @staticmethod
    def _save_render_window(window):
        # Observed R13 save thumbnail rendering: detached viewport outside the
        # desktop. Never treat its presence as completion; wait for disappearance.
        return (window['owner']==0 and window['title']=='AutoForm Forming' and
                window['class_name']=='Qt5QWindowIcon' and native.outside_desktop(window['rect']))

    def capture(self,label):
        windows=self._windows()
        main=unique(windows,hwnd=self.hwnd)
        path=self.directory/f'{time.monotonic_ns()}-{label}.png'
        ImageGrab.grab(bbox=tuple(main['rect'])).save(path)
        self.supervisor.evidence.snapshot(label,path,metadata={'windows':windows})
        return str(path)

    def inventory(self):
        self._idle()
        response=self._call('tabs')
        self._last_title=response['title']
        current={}
        for tab in response['tabs']:
            rid=tuple(tab['rid'])
            token=self.live_rids.get(rid) or f'{self.session_id}:{uuid.uuid4().hex}'
            button=unique(tab['children'],**{'class':'AFDesignBarToolButton'})
            close=unique(tab['children'],**{'class':'QToolButton'})
            cached=self.entries.get(token)
            # Paths are associated only by the verified file-dialog transaction.
            path=cached.path if cached and cached.name==button['name'] else None
            meta={'runtime_id':tab['rid'],'button_rid':button['rid'],'close_rid':close['rid'],
                  'dirty_observation':'not_exposed','path_source':'file_dialog_transaction' if path else 'unknown'}
            if token in self._saved and path:
                meta.update(self._saved[token])
            current[token]=Design(token,button['name'],path,None,meta)
            self.live_rids[rid]=token
        live_tokens=set(current)
        self.live_rids={rid:token for rid,token in self.live_rids.items() if token in live_tokens}
        self.entries=current
        return list(current.values())

    def _design(self,design):
        current={d.token:d for d in self.inventory()}
        if design.token not in current: raise BridgeError('stale_document','Document token no longer exists')
        return current[design.token]

    def _select(self,design):
        self._idle(dismiss_welcome=True)
        current=self._design(design)
        if sum(item.name==current.name for item in self.entries.values())!=1:
            raise BridgeError('ambiguous_document_name','Duplicate tab names prevent independent active-document verification')
        self._call('click',rid=current.metadata['button_rid'])
        self.inventory()
        if not active_title_matches(self._last_title,current.name):
            raise BridgeError('active_document_mismatch','Active title did not match the uniquely identified tab')
        return current

    def create(self):
        self._idle(dismiss_welcome=True)
        before={d.token for d in self.inventory()}
        if self._owned_tokens & before: raise BridgeError('owned_limit','Close owned design before creating another')
        self._call('click',selector={'aid':NEW})
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            now=self.inventory()
            new=[d for d in now if d.token not in before]
            if len(new)==1 and len(now)==len(before)+1:
                self._owned_tokens.add(new[0].token)
                self.capture('created')
                return new[0]
            if len(new)>1: raise BridgeError('ambiguous_creation','More than one new document appeared')
            time.sleep(.2)
        raise BridgeError('create_timeout','Creation outcome unknown; do not retry')

    def _file_dialog(self,title):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            windows=self._windows()
            extra=[w for w in windows if w['hwnd']!=self.hwnd]
            if len(extra)==1 and extra[0]['class_name']=='#32770' and extra[0]['owner']==self.hwnd and extra[0]['title']==title:
                dialog=extra[0]
                controls=self._call('file_fields',hwnd=dialog['hwnd'])['controls']
                edit_aid='1148' if title=='打开设计文件' else '1001'
                unique(controls,aid=edit_aid,**{'class':'Edit'})
                unique(controls,aid='1',**{'class':'Button'})
                dialog['edit_aid']=edit_aid
                return dialog
            if extra: self._unexpected(extra)
            time.sleep(.15)
        raise BridgeError('dialog_timeout','Expected file dialog did not appear')

    def _submit_file(self,dialog,path):
        self._call('text',hwnd=dialog['hwnd'],selector={'aid':dialog['edit_aid'],'class':'Edit'},value=str(path))
        check=unique(self._call('file_fields',hwnd=dialog['hwnd'])['controls'],aid=dialog['edit_aid'],**{'class':'Edit'})
        if check.get('value')!=str(path): raise BridgeError('path_mismatch','File dialog path changed')
        self._call('click',hwnd=dialog['hwnd'],selector={'aid':'1','class':'Button'})
        deadline=time.monotonic()+30
        stable=0
        while time.monotonic()<deadline:
            windows=self._windows()
            extra=[w for w in windows if w['hwnd']!=self.hwnd]
            if not extra and unique(windows,hwnd=self.hwnd)['enabled']:
                stable+=1
                if stable>=3: return
            else:
                stable=0
                if any(w['hwnd']!=dialog['hwnd'] and not self._save_render_window(w) for w in extra):
                    self._unexpected(extra)
            time.sleep(.2)
        raise BridgeError('file_dialog_timeout','File dialog/save did not settle')

    def save(self,design,path):
        if design.token not in self._owned_tokens: raise BridgeError('not_owned','Adapter cannot save baseline design')
        current=self._select(design)
        path=Path(path).resolve()
        if not path.is_relative_to(self.directory): raise BridgeError('path_outside_run','Save path must be in this run')
        if path.exists() and current.path!=str(path): raise BridgeError('overwrite_refused','Cannot overwrite a different existing file')
        same=current.path==str(path)
        self._saved.pop(design.token,None)
        if not same:
            self._call('keys',keys='{Ctrl}{Shift}s')
            dialog=self._file_dialog('另存为')
            self._submit_file(dialog,path)
        else:
            self._call('keys',keys='{Ctrl}s')
            time.sleep(.5)
            self._idle()
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            after=self._design(design)
            if after.name==path.stem and self._last_title.startswith(f'AutoForm Forming R13 - {path.name} ') and path.is_file() and path.stat().st_size>0:
                self._idle()
                verified={'save_verified':True,'verification_scope':'file-dialog path + same live tab + document title + idle + file; dirty flag unknown',
                          'save_file':self.supervisor.evidence.hash_file(path)}
                self._saved[design.token]=verified
                updated=replace(after,path=str(path),metadata={**after.metadata,**verified})
                self.entries[design.token]=updated
                self.capture('saved')
                return updated
            time.sleep(.2)
        raise BridgeError('save_unverified','Saved document/path could not be verified')

    def close(self,design):
        if design.token not in self._owned_tokens: raise BridgeError('not_owned','Adapter cannot close baseline design')
        current=self._select(design)
        if not current.metadata.get('save_verified'): raise BridgeError('save_required','Verified save required before close')
        self._call('click',rid=current.metadata['close_rid'])
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            # A known warning may indicate an intervening modification. Cancel,
            # preserve content, and let lifecycle diagnostics save a fresh file.
            windows=self._windows()
            extra=[w for w in windows if w['hwnd']!=self.hwnd]
            if len(extra)==1:
                controls=self._dump(extra[0]['hwnd'])
                if controls[0]['class']=='AutoForm::AFMessageBox':
                    messages=[c['name'] for c in controls if c['aid'].endswith('.m_text')]
                    if len(messages)==1 and Path(current.path).name in messages[0] and '保存' in messages[0]:
                        cancel=unique(controls,aid='main_window.AFMessageBoxClass.m_buttonBoxFrame.m_buttonBox.CancelButton')
                        self._call('click',hwnd=extra[0]['hwnd'],rid=cancel['rid'])
                        self._saved.pop(design.token,None)
                        raise BridgeError('save_required','Unexpected dirty prompt was cancelled without discarding')
            now={d.token for d in self.inventory()}
            if design.token not in now:
                self._owned_tokens.discard(design.token)
                self._saved.pop(design.token,None)
                self._idle(dismiss_welcome=True)
                self.capture('closed')
                return
            time.sleep(.2)
        raise BridgeError('close_timeout','Owned tab still exists')

    def open(self,path):
        self._idle(dismiss_welcome=True)
        path=Path(path).resolve()
        if not path.is_relative_to(self.directory) or not path.is_file():
            raise BridgeError('path_outside_run','Open only this run\'s existing file')
        current=self.inventory()
        if any(d.name==path.stem for d in current):
            raise BridgeError('ambiguous_document_name','Same-named baseline design is already open')
        before={d.token for d in current}
        if self._owned_tokens & before: raise BridgeError('owned_limit','Close owned design before reopening')
        self._call('keys',keys='{Ctrl}o')
        dialog=self._file_dialog('打开设计文件')
        self._submit_file(dialog,path)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            now=self.inventory()
            new=[d for d in now if d.token not in before]
            if len(new)==1 and len(now)==len(before)+1 and new[0].name==path.stem and self._last_title.startswith(f'AutoForm Forming R13 - {path.name} '):
                opened=replace(new[0],path=str(path),metadata={**new[0].metadata,'path_source':'open_file_dialog'})
                self.entries[opened.token]=opened
                self._owned_tokens.add(opened.token)
                self.capture('reopened')
                return opened
            if len(new)>1: raise BridgeError('ambiguous_open','More than one document appeared')
            time.sleep(.2)
        raise BridgeError('open_unverified','Same-file reopen could not be verified')

    def readback(self,design):
        self._select(design)
        controls=self._dump()
        result=empty_counts(controls,self._idle()['rect'])
        self.capture('readback')
        return result

    def business(self,design,operation,arguments):
        from ..operations.dispatch import OPERATIONS
        if operation not in OPERATIONS:
            raise BridgeError('not_implemented','Business operation not registered')
        if design.token not in self._owned_tokens:
            raise BridgeError('not_owned','Business operations require an owned design')
        if operation=='import_part':
            # R13 can restore below the desktop work area, covering Apply with
            # the taskbar. Start business work in the maximized application view.
            self._idle()
            native.u.ShowWindow(self.hwnd,3)
            time.sleep(.3)
            self.supervisor.evidence.append('business_window_maximized',hwnd=self.hwnd,windows=self._windows())
        current=self._select(design)
        if operation!='read_business':
            self._saved.pop(design.token,None)
        timeout={'forming_check':210,'start_simulation':360,'read_business':180}.get(operation,120)
        response=self.supervisor.run_worker({'action':'business','operation':operation,
            'pid':self.pid,'hwnd':self.hwnd,'document_rid':current.metadata['runtime_id'],
            'directory':str(self.directory),'project':current.path,'arguments':arguments},timeout=timeout)
        if operation=='start_simulation' and response.get('save_transaction',{}).get('ok'):
            proof=response['save_transaction']
            path=Path(arguments['path']).resolve()
            rid=current.metadata['runtime_id']
            from ..lifecycle import digest, canonical_path
            if (canonical_path(proof.get('path',''))!=canonical_path(path)
                    or proof.get('path_readback')!=str(path)
                    or proof.get('before_rid')!=rid or proof.get('after_rid')!=rid
                    or not path.is_relative_to(self.directory) or not path.is_file()
                    or proof.get('sha256')!=digest(path)):
                raise BridgeError('simulation_save_unverified','Start save transaction failed independent validation',proof=proof)
            after=self._design(design)
            if after.name!=path.stem or not active_title_matches(self._last_title,path.stem):
                raise BridgeError('simulation_save_identity','Start save title/identity mismatch')
            verified={'save_verified':True,'save_file':digest(path),'save_transaction':proof,
                      'verification_scope':'Start file-dialog path + same owned tab + title + file hash'}
            self._saved[design.token]=verified
            self.entries[design.token]=replace(after,path=str(path),metadata={**after.metadata,**verified})
        return response

    def metrics(self):
        self._windows()
        memory=psutil.Process(self.pid).memory_info()
        return {'pid':self.pid,'working_set_bytes':memory.rss,'private_bytes':getattr(memory,'private',None)}

    def shutdown(self):
        if self._closed: return
        # A failed reap must retain the writer lease. Another bridge must not
        # start sending input while the first bridge's worker may still run.
        self.supervisor.shutdown()
        self.lease.release()
        self._closed=True
