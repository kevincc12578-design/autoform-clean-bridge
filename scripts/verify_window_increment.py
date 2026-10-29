"""Explicit live fixed-window rejection check; no design or solver mutations."""
import argparse
import json
from pathlib import Path
import time


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--out',required=True)
    args=parser.parse_args()
    if not args.live:
        parser.error('Window mutations require --live')
    from af_r13_bridge.adapters.windows import WindowsAdapter
    from af_r13_bridge.adapters import native
    from af_r13_bridge.models import BridgeError
    out=Path(args.out).resolve()
    out.mkdir(parents=True,exist_ok=False)
    adapter=None
    before=None
    result={'ok':False,'checks':{},'design_mutation_requested':False}
    try:
        adapter=WindowsAdapter(out)
        before=[d.to_dict() for d in adapter.inventory()]
        original=dict(adapter.window_profile)
        native.configure_input(original)
        altered=native.normalize_window(adapter.hwnd,adapter.pid,
            dict(client_width=1760,client_height=940,client_x=35,client_y=55,dpi=96))
        result['altered']=altered
        try:
            native.guard_input(adapter.hwnd,adapter.pid)
        except BridgeError as error:
            result['checks']['resize_input_rejected']=error.to_dict()
        else:
            raise AssertionError('Resized window was accepted')
        result['restored_after_resize']=native.normalize_window(adapter.hwnd,adapter.pid)
        native.u.ShowWindow(adapter.hwnd,6)
        deadline=time.monotonic()+3
        while not native.u.IsIconic(adapter.hwnd) and time.monotonic()<deadline:
            time.sleep(.05)
        if not native.u.IsIconic(adapter.hwnd):
            raise AssertionError('Minimize was not observed')
        try:
            native.guard_input(adapter.hwnd,adapter.pid)
        except BridgeError as error:
            result['checks']['minimized_input_rejected']=error.to_dict()
        else:
            raise AssertionError('Minimized window was accepted')
    except Exception as error:
        result['error']={'type':type(error).__name__,'message':str(error)}
    finally:
        if adapter is not None:
            try:
                result['final_profile']=native.normalize_window(adapter.hwnd,adapter.pid)
                after=[d.to_dict() for d in adapter.inventory()]
                result['documents_unchanged']=before is not None and before==after
                result['before']=before
                result['after']=after
                adapter.capture('fixed-window-restored')
                result['ok']=bool(not result.get('error') and len(result['checks'])==2 and before is not None and before==after)
            finally:
                adapter.shutdown()
                result['workers_stopped']=True
        (out/'window-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__=='__main__':
    raise SystemExit(main())
