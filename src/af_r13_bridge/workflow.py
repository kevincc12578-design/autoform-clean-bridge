"""Baogu workflow composed exclusively from the public Bridge API."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
import time
import uuid
from .api import Bridge
from .models import BridgeError
from .isolation import isolation_report

DEFAULT_PART=Path('C:/Users/Administrator/Desktop/八角管抱箍/抱箍.STEP')
DEFAULT_MATERIAL=Path('C:/ProgramData/AutoForm/AFplus/R13F/materials/BrandName_MDB/Erdemir/Steels_Conventional_HSS/S355MC_4936_4.00.mtb')
BUSINESS_STAGES=('import_part','extract_part','set_direction','set_material','forming_check','create_plan',
    'assign_operation','visit_die_face','copy_blank','set_friction','configure_tools','add_reference','set_springback',
    'start_simulation','solve')
FINAL_STAGES=('read_before','save_before_close','close_original','reopen_same_file','read_after',
              'compare_persistence','save_after_reopen','close_reopened')
ALL_STAGES=('attach','create_design','empty_readback')+BUSINESS_STAGES+FINAL_STAGES


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path,data):
    path=Path(path)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    temporary.replace(path)


def code_manifest(root):
    root=Path(root)
    source=root/'src'/'af_r13_bridge'
    installed=Path(__file__).parent
    records=[]
    for path in sorted(source.rglob('*')):
        if not path.is_file() or path.suffix not in {'.py','.json','.png'}:
            continue
        relative=path.relative_to(source)
        counterpart=installed/relative
        digest=sha(path)
        records.append({'file':relative.as_posix(),'source_sha256':digest,
                        'installed_sha256':sha(counterpart) if counterpart.is_file() else None})
    if any(row['source_sha256']!=row['installed_sha256'] for row in records):
        raise BridgeError('installation_stale','Source and installed package differ; reinstall before live verification')
    return {'files':records,'fingerprint':hashlib.sha256(json.dumps(records,sort_keys=True).encode()).hexdigest()}


def run_once(directory,part,material,*,launch=False,license_server=None,allow_selected_license=False,until=None,adapter_factory=None):
    directory=Path(directory).resolve()
    directory.mkdir()
    started=time.monotonic()
    result={'ok':False,'e2e_ok':False,'from_empty':True,'stages':[], 'run_directory':str(directory),
            'reference_reversed':False,'numeric_springback_field_read':False,'product_tolerance_verified':False}
    bridge=None
    document_id=None
    halted=False
    filename=directory/f'抱箍-{uuid.uuid4().hex[:10]}.afd'
    try:
        copies=[]
        for role,source in [('part',part),('material',material)]:
            source=Path(source).resolve()
            copied=directory/source.name
            shutil.copy2(source,copied)
            if sha(copied)!=sha(source):
                raise BridgeError('input_copy_mismatch','Input copy differs from source')
            copies.append({'role':role,'source':str(source),'path':str(copied),'sha256':sha(copied)})
        result['inputs']=copies
        if adapter_factory is None:
            from .adapters.windows import WindowsAdapter
            adapter_factory=WindowsAdapter
        adapter=adapter_factory(directory,launch=launch,license_server=license_server,allow_selected_license=allow_selected_license)
        bridge=Bridge(adapter,directory)

        def call(stage,operation=None,arguments=None):
            nonlocal document_id
            if time.monotonic()-started>=1200:
                raise BridgeError('run_timeout','Workflow exceeded 20 minutes')
            arguments=dict(arguments or {})
            op=operation or stage
            if op not in {'attach','create_design','reopen_design'}:
                arguments['document_id']=document_id
            value=bridge.call(op,arguments)
            if value.get('document_id'):
                document_id=value['document_id']
            item={'name':stage,'status':'passed' if value['ok'] else 'failed','result':value}
            result['stages'].append(item)
            write(directory/'workflow.json',result)
            print(json.dumps({'run':directory.name,'stage':stage,'ok':value['ok'],'error':value.get('error')},ensure_ascii=False),flush=True)
            if not value['ok']:
                raise BridgeError('workflow_stage_failed','Required business stage failed',stage=stage,details=value.get('error'))
            return value['data']

        call('attach')
        call('create_design')
        call('empty_readback','readback')
        expected={}
        for stage in BUSINESS_STAGES:
            args={}
            if stage=='import_part': args={'path':copies[0]['path']}
            elif stage=='set_material': args={'path':copies[1]['path'],'thickness_mm':4.0}
            elif stage=='start_simulation': args={'path':str(filename),'timeout':300}
            elif stage=='solve': args={'timeout':min(900,max(1,1190-(time.monotonic()-started))),'license_server':license_server or '2375@127.0.0.1'}
            data=call(stage,arguments=args)
            if stage=='set_direction': expected=data.get('after',{}).get('angles_deg')
            if stage==until:
                halted=True
                result['development_stop_after']=until
                break
        if not halted:
            read_args={'material_stem':Path(material).stem,'thickness_mm':4.0,'expected_angles':expected}
            before=call('read_before','read_business',read_args)
            call('save_before_close','save_design',{'path':str(filename)})
            call('close_original','close_design')
            call('reopen_same_file','reopen_design',{'path':str(filename)})
            after=call('read_after','read_business',read_args)
            matched=before['values']==after['values']
            result['persistence']={'ok':matched,'same_file':str(filename),'before':before['values'],'after':after['values']}
            result['stages'].append({'name':'compare_persistence','status':'passed' if matched else 'failed','result':result['persistence']})
            if not matched:
                raise BridgeError('persistence_mismatch','Business values changed after same-file reopening',before=before['values'],after=after['values'])
            call('save_after_reopen','save_design',{'path':str(filename)})
            call('close_reopened','close_design')
            result['final_file']={'path':str(filename),'sha256':sha(filename),'role':'GUI original; saved, closed, same-file reopened and closed again'}
    except (Exception,KeyboardInterrupt) as error:
        result['error']={'code':getattr(error,'code','workflow_failed'),'message':str(error),'details':getattr(error,'details',{})}
    finally:
        if bridge is not None:
            try:
                result['cleanup']=bridge.call('cleanup')
                result['resources']=bridge.lifecycle.resource_samples
                result['baseline']=[item.to_dict() for item in (bridge.lifecycle.baseline or {}).values()]
            finally:
                result['disconnect']=bridge.call('disconnect')
            result['evidence_errors']=list(bridge.lifecycle.evidence_errors)
            if result['evidence_errors']:
                result.setdefault('error',{'code':'evidence_write_failed','details':result['evidence_errors']})
        existing={item['name'] for item in result['stages']}
        for stage in ALL_STAGES:
            if stage not in existing:
                result['stages'].append({'name':stage,'status':'not_requested' if halted and not result.get('error') else 'skipped_dependency'})
        result['elapsed_seconds']=round(time.monotonic()-started,3)
        result['e2e_ok']=bool(not halted and not result.get('error') and
            all(item['status']=='passed' for item in result['stages']) and
            result.get('cleanup',{}).get('ok') and result.get('disconnect',{}).get('ok') and result['elapsed_seconds']<1200)
        result['ok']=result['e2e_ok']
        result['development_ok']=bool(halted and not result.get('error') and result.get('cleanup',{}).get('ok') and result.get('disconnect',{}).get('ok'))
        write(directory/'workflow.json',result)
    return result


def run_chain(directory,*,runs=3,part=DEFAULT_PART,material=DEFAULT_MATERIAL,launch=False,license_server=None,allow_selected_license=False,until=None):
    directory=Path(directory).resolve()
    if runs<1 or runs>3 or (until and runs!=1):
        raise BridgeError('invalid_request','Use 1–3 runs; development --until requires exactly 1 run')
    if until and until not in BUSINESS_STAGES:
        raise BridgeError('invalid_request','Unknown development stop stage')
    root=Path(__file__).resolve().parents[4] if '.venv' in Path(__file__).parts else Path(__file__).resolve().parents[2]
    import sys
    root=Path(sys.prefix).parent
    audit=isolation_report(root)
    write(directory/'isolation-before.json',audit)
    if not audit['ok']:
        raise BridgeError('isolation_failed','Refusing business workflow with an invalid runtime',findings=audit['findings'])
    manifest=code_manifest(root)
    write(directory/'code-manifest.json',manifest)
    result={'ok':False,'e2e_ok':False,'workflow':'baogu','runs_requested':runs,'runs':[],'version_fingerprint':manifest['fingerprint']}
    for number in range(1,runs+1):
        if code_manifest(root)['fingerprint']!=manifest['fingerprint']:
            raise BridgeError('code_changed','Frozen source changed between acceptance rounds')
        current=run_once(directory/f'run-{number}',part,material,launch=launch,
            license_server=license_server,allow_selected_license=allow_selected_license,until=until)
        result['runs'].append(current)
        write(directory/'chain.json',result)
        if not current['ok']:
            break
    audit_after=isolation_report(root)
    write(directory/'isolation-after.json',audit_after)
    result['source_unchanged']=code_manifest(root)['fingerprint']==manifest['fingerprint']
    result['ok']=result['e2e_ok']=bool(len(result['runs'])==runs and all(item['ok'] for item in result['runs']) and audit_after['ok'] and result['source_unchanged'])
    result['three_consecutive_verified']=bool(result['ok'] and runs==3)
    result['development_ok']=bool(until and len(result['runs'])==1 and result['runs'][0].get('development_ok'))
    write(directory/'chain.json',result)
    return result
