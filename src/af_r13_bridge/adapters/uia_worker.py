"""Bounded disposable UIA process. Every mutation re-finds its target.

This worker never spawns child processes. Its supervisor owns cancellation.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import traceback
import time

def run(req):
    from . import native
    native.configure_input(req.get('window_profile'))
    if req.get('material_preview'):
        # Explicitly scoped file-picker transaction/recovery only. Observation
        # and every subsequent native guard remain in this bounded worker.
        if req.get('action') not in {'click','text'}:
            raise ValueError('Material preview context requires a file-picker input')
        from ..operations.ui import material_preview_proof
        with native.material_preview_context(req['hwnd'],material_preview_proof(req['hwnd'])):
            return _run(req)
    return _run(req)


def _run(req):
    import uiautomation as a
    from . import native
    from ..isolation import isolation_report
    audit=isolation_report()
    if not audit['ok']: raise RuntimeError('Worker isolation audit failed: '+str(audit['findings']))
    Path(req['_audit_path']).with_suffix('.before.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    a.SetGlobalSearchTimeout(2)
    if req.get('action') == 'business':
        from ..operations.dispatch import run as run_business
        return run_business(req)
    hwnd, pid = int(req['hwnd']), int(req['pid'])
    if native.window_pid(hwnd)!=pid:
        raise RuntimeError('Stale window')
    root=a.ControlFromHandle(hwnd)

    def record(control,depth=0):
        rect=control.BoundingRectangle
        data={'name':control.Name, 'class':control.ClassName, 'aid':control.AutomationId,
              'kind':control.ControlTypeName, 'rid':list(control.GetRuntimeId()),
              'rect':[rect.left,rect.top,rect.right,rect.bottom],
              'enabled':control.IsEnabled,'offscreen':control.IsOffscreen,'depth':depth,
              'help':control.HelpText}
        try:
            p=control.GetValuePattern()
            if p: data['value']=p.Value
        except Exception:
            pass
        return data

    def walk(control,depth=0):
        yield control,depth
        if depth < int(req.get('depth',16)):
            for child in control.GetChildren():
                yield from walk(child,depth+1)

    action=req['action']
    if action=='file_fields':
        if root.ClassName!='#32770': raise RuntimeError('Expected native file dialog')
        selected=[item for item in native.dialog_children(hwnd)
                  if (item['class']=='Edit' and item['aid'] in ('1001','1148')) or
                     (item['class']=='Button' and item['aid'] in ('1','2'))]
        if any(item['pid']!=pid for item in selected): raise RuntimeError('File control PID mismatch')
        return {'controls':[record(a.ControlFromHandle(item['hwnd'])) for item in selected]}
    if action=='dump':
        records=[]
        for control,depth in walk(root):
            records.append(record(control,depth))
            if len(records)>4000: raise RuntimeError('Tree too large')
        return {'controls':records,'windows':native.windows(pid)}
    if action=='tabs':
        tabs=[]
        # Stop at the design bar; do not enumerate hidden document workspaces.
        def bars(control,depth=0):
            if control.ClassName=='AutoForm::DesignBarView':
                yield control
                return
            if depth<7:
                for child in control.GetChildren():
                    yield from bars(child,depth+1)
        for bar in bars(root):
            for item,_ in walk(bar):
                if item.ClassName=='AutoForm::DesignBarItem':
                    item_data=record(item)
                    item_data['children']=[record(child) for child in item.GetChildren()]
                    tabs.append(item_data)
        return {'tabs':tabs,'title':root.Name,'windows':native.windows(pid)}
    if action=='keys':
        if not native.u.IsWindowEnabled(hwnd): raise RuntimeError('Window is disabled by a modal')
        native.guard_input(hwnd,pid,activate=True)
        if not native.u.IsWindowEnabled(hwnd): raise RuntimeError('Window became disabled')
        a.SendKeys(req['keys'],waitTime=.15)
        return {'input_sent':True}
    target=None
    rid=req.get('rid')
    matches=[]
    selector=req.get('selector',{})
    native_file_target=(root.ClassName=='#32770' and set(selector)=={'aid','class'} and
                        selector['class'] in ('Edit','Button') and selector['aid'] in ('1','2','1001','1148'))
    if native_file_target:
        search=[(a.ControlFromHandle(item['hwnd']),0) for item in native.dialog_children(hwnd)
                if all(item.get(key)==value for key,value in selector.items()) and item['pid']==pid]
    else:
        search=walk(root)
    for control,_ in search:
        if rid is not None:
            match=list(control.GetRuntimeId())==rid
        else:
            match=all(getattr(control,{'name':'Name','class':'ClassName','aid':'AutomationId','kind':'ControlTypeName'}[k])==v
                      for k,v in req.get('selector',{}).items())
        if match: matches.append(control)
    if len(matches)!=1: raise RuntimeError(f'Expected unique target, found {len(matches)}')
    target=matches[0]
    popup_proof=None
    main_hwnd=req.get('window_profile',{}).get('hwnd',hwnd)
    if hwnd!=main_hwnd:
        observed=next(item for item in native.windows(pid) if item['hwnd']==hwnd)
        if observed['owner']==0:
            popup_proof={**observed,'root_class':root.ClassName}
    native.guard_input(hwnd,pid,activate=True,popup_proof=popup_proof)
    r=record(target)
    if not r['enabled']: raise RuntimeError('Target is disabled')
    x,y=native.guard_point(hwnd,pid,r['rect'],popup_proof=popup_proof)
    if action=='click':
        native.click_point(x,y)
    elif action=='invoke':
        pattern=target.GetInvokePattern()
        if pattern is None: raise RuntimeError('Target does not support InvokePattern')
        pattern.Invoke()
    elif action=='value':
        p=target.GetValuePattern()
        if not p: raise RuntimeError('No ValuePattern')
        target.SetFocus()
        if not target.HasKeyboardFocus: raise RuntimeError('Edit focus guard failed')
        native.guard_input(hwnd,pid)
        p.SetValue(req['value'])
        time.sleep(.2)
        if p.Value != req['value']: raise RuntimeError('Value readback differs')
    elif action=='text':
        a.Click(x,y,waitTime=.1)
        target.SetFocus()
        if not target.HasKeyboardFocus: raise RuntimeError('Edit focus guard failed')
        native.guard_input(hwnd,pid)
        a.SendKeys('{Ctrl}a',waitTime=.05)
        for char in req['value']:
            if not target.HasKeyboardFocus: raise RuntimeError('Focus changed during text input')
            native.guard_input(hwnd,pid)
            a.SendUnicodeChar(char)
        # SendInput enqueues input; the dialog can finish handling it after the
        # sending call returns. Observe completion without retyping the path.
        deadline=time.monotonic()+3
        actual=None
        while time.monotonic()<deadline:
            p=target.GetValuePattern()
            actual=p.Value if p else None
            if actual==req['value']: break
            time.sleep(.05)
        else:
            raise RuntimeError(f'Typed value readback differs: {actual!r}')
    else:
        raise ValueError('Unsupported worker action')
    return {'input_sent':True,'target':r}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--request',required=True)
    parser.add_argument('--response',required=True)
    args=parser.parse_args()
    try:
        request=json.loads(Path(args.request).read_text(encoding='utf-8-sig'))
        request['_audit_path']=str(Path(args.response).with_suffix('.isolation.json'))
        result=run(request)
        payload={'ok':True,**result}
        code=0
    except BaseException as exc:
        payload={'ok':False,'error':str(exc),'traceback':traceback.format_exc()}
        code=1
    try:
        from ..isolation import isolation_report
        final_audit=isolation_report()
        Path(args.response).with_suffix('.isolation.json').write_text(json.dumps(final_audit,ensure_ascii=False,indent=2),encoding='utf-8')
        if not final_audit['ok']:
            payload={'ok':False,'error':'Worker final isolation audit failed','findings':final_audit['findings']}
            code=1
    except BaseException as exc:
        payload={'ok':False,'error':'Cannot record final worker isolation: '+str(exc)}
        code=1
    Path(args.response).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    return code

if __name__=='__main__':
    sys.exit(main())
