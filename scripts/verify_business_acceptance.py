"""Read-only independent acceptance reparse; only the audit report is written.

Usage: .venv/Scripts/python.exe -I scripts/verify_business_acceptance.py RUN_DIRECTORY
Optional --expected-runs 1 is a diagnostic only, never three-round acceptance.
No UI modules, workflow implementation or production result parsers are imported.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
BUSINESS = ['import_part','extract_part','set_direction','set_material','forming_check','create_plan',
            'assign_operation','visit_die_face','copy_blank','set_friction','configure_tools',
            'add_reference','set_springback','start_simulation','solve']
STAGES = ['attach','create_design','empty_readback'] + BUSINESS + [
    'read_before','save_before_close','close_original','reopen_same_file','read_after',
    'compare_persistence','save_after_reopen','close_reopened']
LIFECYCLE = ['attach','create_design','readback'] + BUSINESS[:-2] + [
    'business_save_registered','start_simulation','solve','read_business','save_design','close_design',
    'reopen_design','same_file_opened','read_business','save_design','close_design','cleanup','disconnect']
AXES = ('X', "Y'", "Z''")


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            h.update(chunk)
    return h.hexdigest()


def pathkey(value):
    return normalize(str(value)).strip().strip('"').replace('\\','/').casefold()


def normalize(text):
    def restore(match):
        try:
            return bytes(ord(c)&255 for c in match.group()).decode('utf-8')
        except UnicodeDecodeError:
            return match.group()
    return re.sub(r'[\uff80-\uffff]+',restore,text)


def decode(raw):
    for codec in (('utf-16',) if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else ('utf-8-sig','gb18030')):
        try:
            return normalize(raw.decode(codec))
        except UnicodeDecodeError:
            continue
    raise ValueError('Unrecognized raw log encoding')


def native_parse(text, full):
    """Independently require every END, and ordered actual Springback execution."""
    lines=normalize(text).splitlines()
    end_re=re.compile(r'\bProgram\s+END[^\[]*\[(\d+)\s+(-?\d+)\.?\]',re.I)
    ends=[]
    raw_count=0
    fatal=[]
    for line_no,line in enumerate(lines,1):
        raw_count += len(re.findall(r'\bProgram\s+END\b',line,re.I))
        ends.extend({'line':line_no,'pid':int(m[1]),'code':int(m[2])} for m in end_re.finditer(line))
        if re.match(r'^\s*(?:[a-z]+:\s*)?(?:ERROR:|FATAL(?:\s+ERROR)?\b)',line,re.I):
            fatal.append({'line':line_no,'text':line})
    zero=bool(ends) and len(ends)==raw_count and all(e['code']==0 for e in ends)
    sequences=[]
    for number,line in enumerate(lines,1):
        convergence=re.match(r'^\s*nl:\s*Springback solution converged in\s+([1-9]\d*)\s+iterations?\.\s*$',line,re.I)
        if not convergence:
            continue
        prior=max((e['line'] for e in ends if e['line']<number),default=0)
        following=next((e for e in ends if e['line']>number),None)
        if following is None:
            continue
        finished=next((i for i in range(number+1,following['line']) if re.match(r'^\s*ct:\s*Simulation successfully finished\.\s*$',lines[i-1],re.I)),None)
        if finished is None:
            continue
        starts=[]
        for i in range(prior+1,number):
            m=re.match(r'^\s*ct:\s*Start of process step\b.*?"([^"]+)"',lines[i-1],re.I)
            if m:
                starts.append((i,m[1]))
        active=starts[-1] if starts else None
        summary=next((i for i in range(number+1,finished) if re.match(r'^\s*Process step\s*\.{2,}\s*:\s*"[^"]*\bSpringback\b[^"]*"',lines[i-1],re.I)),None)
        if active and re.search(r'\bSpringback\b',active[1],re.I) or summary:
            sequences.append({'convergence_line':number,'iterations':int(convergence[1]),
                              'context_line':active[0] if active and re.search(r'\bSpringback\b',active[1],re.I) else summary,
                              'finished_line':finished,'end':following})
    counts=[int(n) for n in re.findall(r'(?:New number of elements:\s*|InitialElements\s+\S+\s+)(\d+)',text)]
    tool_names=set(re.findall(r'Tool center initial coordinates of "([^"]+)"',text))
    geometry={'operation_c20':'Initializing operation "C-20"' in text,
              'material':'Blank material data' in text,'positive_mesh':any(n>0 for n in counts),
              'both_tools':{'凸模','凹模'}.issubset(tool_names)}
    return {'ok':zero and not fatal and (bool(sequences) if full else all(geometry.values())),
            'end_codes':[e['code'] for e in ends],'end_markers':ends,'raw_end_count':raw_count,
            'fatal':fatal,'springback_sequences':sequences,'geometry':geometry}


def gui_binding(text, target):
    records=[]
    for number,line in enumerate(normalize(text).splitlines(),1):
        if 'Started job:' not in line:
            continue
        m=re.search(r'Started job:\s*AFFormingJob\s+-jobId\s+(SingleSim_[A-Za-z0-9]+)#([A-Za-z0-9-]+)\s+-jn\s+(.*?)\s+-ls\s+',line)
        if m and pathkey(m[3])==pathkey(target):
            records.append({'name':m[1],'id':m[2],'line':number,'source':normalize(m[3]).strip('"')})
    unique={(x['name'],x['id']) for x in records}
    saved=any('#FILE_LOG#Save:' in line and pathkey(line.split('#FILE_LOG#Save:',1)[1])==pathkey(target) for line in normalize(text).splitlines())
    return {'ok':len(unique)==1 and saved,'records':records,'saved_full_path':saved,
            'job':records[-1] if len(unique)==1 else None}


def gui_status(text, job, target):
    normalized=normalize(text)
    starts=list(re.finditer(r'<singleSimJobStatus\b',normalized))
    chunks=list(re.finditer(r'<singleSimJobStatus\b[^>]*>.*?</singleSimJobStatus>',normalized,re.S))
    malformed=len(starts)!=len(chunks)
    statuses=[]
    for chunk in chunks:
        try:
            node=ET.fromstring(chunk.group())
        except ET.ParseError:
            malformed=True
            continue
        if node.findtext('jobId_R11/name')==job['name'] and node.findtext('jobId_R11/id')==job['id'] and pathkey(node.findtext('fileName',''))==pathkey(target):
            statuses.append({k:node.findtext(k) for k in ('status','failing','canceling','check')})
    source=any(pathkey(x)==pathkey(target) for x in re.findall(r'Message: Input file:\s*([^\r\n]+)',normalized))
    signal=bool(re.search(r'sig(?:JobStatus)?Changed\s+Completed\s+'+re.escape(job['id'])+r'\b',normalized))
    last=statuses[-1] if statuses else None
    failed=bool(last and (last['failing']=='true' or last['canceling']=='true' or last['status'] in {'Failed','Canceled','Cancelled'}))
    completed=bool(source and signal and last and not malformed and last['status'] in {'Calculated','Completed'} and all(last[k]=='false' for k in ('failing','canceling','check')))
    return {'completed':completed,'failed':failed,'source_matches':source,'signal':signal,
            'malformed':malformed,'statuses':statuses}


class Audit:
    def __init__(self,directory,expected_runs=3):
        self.directory=directory
        self.expected_runs=expected_runs
        self.result={'ok':False,'audited_at_utc':datetime.now(timezone.utc).isoformat(),
                     'expected_runs':expected_runs,'three_consecutive_verified':False,
                     'scope':'Frozen new Bridge baogu, three empty starts, raw GUI/native completion, same-file business persistence and two closes. No full numeric field or product tolerance claim.',
                     'audit_script_sha256':digest(__file__),'checks':[],'failures':[],'runs':[], 'files':{}}

    def check(self,condition,name,**details):
        item={'name':name,'ok':bool(condition),**details}
        self.result['checks'].append(item)
        if not condition:
            self.result['failures'].append(item)
        return bool(condition)

    def track(self,path):
        path=Path(path).resolve()
        if not path.is_file():
            raise FileNotFoundError(str(path))
        self.result['files'][str(path)]={'sha256':digest(path),'size':path.stat().st_size}
        return path

    def json(self,path):
        return json.loads(self.track(path).read_text(encoding='utf-8-sig'))

    def jsonl(self,path):
        return [json.loads(line) for line in self.track(path).read_text(encoding='utf-8-sig').splitlines() if line.strip()]

    def section(self,name,callback):
        try:
            return callback()
        except Exception as exc:
            self.check(False,name,error_type=type(exc).__name__,error=str(exc))
            return None

    def manifest(self):
        manifest=self.json(self.directory/'code-manifest.json')
        installed=Path(importlib.util.find_spec('af_r13_bridge').origin).parent
        source=ROOT/'src/af_r13_bridge'
        actual=[]
        for file in sorted(source.rglob('*')):
            if file.is_file() and file.suffix in {'.py','.json','.png'}:
                relative=file.relative_to(source)
                copied=installed/relative
                actual.append({'file':relative.as_posix(),'source_sha256':digest(file),
                               'installed_sha256':digest(copied) if copied.is_file() else None})
        fingerprint=hashlib.sha256(json.dumps(actual,sort_keys=True).encode()).hexdigest()
        self.check(manifest.get('files')==actual,'source_installed_matches_frozen_manifest')
        self.check(all(x['source_sha256']==x['installed_sha256'] for x in actual),'source_matches_installed')
        self.check(manifest.get('fingerprint')==fingerprint,'manifest_fingerprint')
        self.result['version_fingerprint']=manifest.get('fingerprint')
        return manifest

    def run_gui(self,run,stages,facts):
        value=stages['start_simulation']['data']
        state=self.json(run/'gui-solver/simulation.json')
        self.check(all(state.get(k)==v for k,v in value.items() if k!='evidence'),f'{run.name}.gui_record_matches_raw_state')
        self.check(all(value.get(k) is True for k in ['ok','started','saved','job_bound','completed','gui_job_completion_verified','native_solver_completion_verified','springback']),f'{run.name}.gui_success_flags')
        target=Path(value['path']).resolve()
        self.check(target.parent==run and target.suffix.casefold()=='.afd',f'{run.name}.gui_target_owned')
        delta_path=self.track(run/'gui-solver/gui-current.delta.log')
        raw=delta_path.read_bytes()
        binding=gui_binding(decode(raw),target)
        self.check(binding['ok'],f'{run.name}.raw_gui_started_and_saved_full_path',binding=binding)
        job=binding['job']
        self.check(job is not None and all(value['job'].get(k)==job[k] for k in ('name','id','source')),f'{run.name}.gui_job_identity')
        external=Path(value['gui_log_path'])
        with external.open('rb') as stream:
            stream.seek(value['gui_log_baseline']['size'])
            corresponding=stream.read(len(raw))
        self.check(corresponding==raw,f'{run.name}.gui_delta_matches_original_offset')
        statuses=[]
        for item in value['job_logs']:
            copy=self.track(item['copy'])
            self.check(copy.parent==run/'gui-solver/diagnostic-logs' and item['sha256']==digest(copy) and item['size']==copy.stat().st_size,f'{run.name}.gui_job_log_hash',file=str(copy))
            self.check(copy.name.startswith('log_'+job['name']+'_AFForming'),f'{run.name}.gui_job_log_name')
            statuses.append(gui_status(decode(copy.read_bytes()),job,target))
        self.check(bool(statuses) and any(x['completed'] for x in statuses) and not any(x['failed'] for x in statuses),f'{run.name}.raw_gui_job_completed',statuses=statuses)
        native_path=self.track(run/'gui-solver/native-solver.log')
        native_text=native_path.read_text(encoding='utf-8')
        self.check(hashlib.sha256(native_text.encode('utf-8')).hexdigest()==value['native_log']['sha256'],f'{run.name}.gui_native_text_hash')
        named=bool(re.search(r'\b'+re.escape(job['name'])+r'\b',native_text))
        fullpath=pathkey(target) in pathkey(native_text)
        unique=bool(re.search(r'(?:^|[-_])[a-f0-9]{8,32}$',target.stem,re.I))
        post=bool(re.search(r'^\s*post:\s*Postfile\s+"'+re.escape(target.name)+r'"\s+opened\.\s*$',native_text,re.M|re.I))
        self.check(named or fullpath or unique and post,f'{run.name}.gui_native_current_job_binding')
        completion=native_parse(native_text,True)
        self.check(completion['ok'],f'{run.name}.raw_gui_native_springback_end',completion=completion)
        self.check(completion['end_codes']==value['program_end_codes'],f'{run.name}.gui_end_codes_match')
        transaction=value['save_transaction']
        owned=stages['create_design']['data']['design']['metadata']['runtime_id']
        self.check(transaction['ok'] and transaction['before_rid']==transaction['after_rid']==owned and pathkey(transaction['path'])==pathkey(target) and pathkey(transaction['path_readback'])==pathkey(target),f'{run.name}.gui_saved_owned_identity_and_path')
        readable=transaction['file_verification']
        samples=readable.get('samples',[])
        self.check(transaction.get('identity_verified') is True and transaction.get('completion_verified') is True and transaction.get('file_bytes_verified') is True and readable.get('ok') is True and readable.get('timed_out') is False and len(samples)==2 and samples[0]==samples[1] and samples[0]['sha256']==readable['sha256']==transaction['sha256'] and samples[0]['size']==readable['file_size']==transaction['file_size'] and samples[0]['size']>0,f'{run.name}.gui_final_bytes_two_stable_readable_samples')
        facts.update(gui_job=job,gui_native_completion=completion,gui_target=str(target),gui_save_hash=transaction['sha256'])

    def run_native(self,run,stages,facts):
        native=stages['solve']['data']
        self.check(native==self.json(run/'solver/solver-result.json'),f'{run.name}.solver_record_matches_raw_state')
        self.check(native['ok'] and native['source_unchanged'] and native['springback'],f'{run.name}.native_success_flags')
        gui=stages['start_simulation']['data']
        self.check(native['source_sha256']==gui['save_transaction']['sha256'],f'{run.name}.native_source_matches_gui_save_bytes')
        roles=native['file_roles']
        self.check(pathkey(roles['gui_original'])==pathkey(gui['path'])==pathkey(native['source_project']) and len({pathkey(v) for v in roles.values()})==3 and native.get('gui_results_updated_by_native_copy') is False,f'{run.name}.distinct_file_roles')
        results=[]
        for check,job in ((True,native['geometry_input_validation']['check']),(False,native['native_job'])):
            label='input_check' if check else 'full_solve'
            folder=Path(job['work_dir']).resolve()
            self.check(folder.is_relative_to(run/'solver'),f'{run.name}.{label}_owned_directory')
            self.check(pathkey(job['project'])==pathkey(roles['input_check_copy' if check else 'native_solve_copy']),f'{run.name}.{label}_file_role_binding')
            self.check(job==self.json(folder/'native-job.json'),f'{run.name}.{label}_manifest_matches')
            self.check(job.get('kind')=='af_r13_bridge.native_solver' and job['ok'] and job['status']=='completed' and job['exit_code']==0 and job['workers_stopped'] and not job['cancelled'] and not job['timed_out'] and job['application_terminated'] is False,f'{run.name}.{label}_terminal_state')
            command=job['command']
            self.check(Path(command[0]).name=='AFFormingJob.exe' and command[command.index('-jn')+1]==job['project'] and ('-check' in command)==check and job['check']==check,f'{run.name}.{label}_command_binding')
            self.check(digest(command[0])==job['executable_sha256'],f'{run.name}.{label}_native_executable_hash')
            self.check(job['source_sha256']==native['source_sha256'],f'{run.name}.{label}_input_copy_hash')
            self.check(digest(self.track(job['project']))==job['final_project_sha256'],f'{run.name}.{label}_final_copy_hash')
            for kind in ('stdout','stderr'):
                file=self.track(job[kind+'_path'])
                self.check(file.parent==folder and digest(file)==job[kind+'_sha256'],f'{run.name}.{label}_{kind}_hash')
            parsed=native_parse(Path(job['stdout_path']).read_text(encoding='utf-8',errors='replace'),not check)
            self.check(parsed['ok'],f'{run.name}.raw_{label}_accepted',parsed=parsed)
            self.check(parsed['end_codes']==job['program_end_codes'],f'{run.name}.{label}_codes_match')
            events=self.jsonl(folder/'events.jsonl')
            closed=[x for x in events if x['event']=='worker_job_closed']
            self.check(len(closed)==1 and closed[0]['active_processes_remaining']==0 and closed[0]['application_terminated'] is False,f'{run.name}.{label}_job_tree_closed')
            results.append({'check':check,'job_id':job['job_id'],'parsed':parsed,'stdout':job['stdout_path']})
        facts['native_jobs']=results

    def semantics(self,run,record,stages,facts):
        before=stages['read_before']['data']
        after=stages['read_after']['data']
        persistence=record['persistence']
        self.check(before['ok'] and after['ok'] and persistence['ok'] and before['values']==after['values']==persistence['before']==persistence['after'],f'{run.name}.business_semantics_persisted')
        values=after['values']; p=values['part']; q=values['process']
        self.check(p['part_count']==1 and bool(p['part_names']) and p['thickness_mm']==4.0 and p['material_stem']=='S355MC_4936_4.00',f'{run.name}.part_material_semantics')
        direction=stages['set_direction']['data']
        start=direction['before']['angles_deg']; target=direction['after']['angles_deg']; saved=p['angles_deg']
        valid=all(set(v)==set(AXES) and all(isinstance(x,(float,int)) and not isinstance(x,bool) and math.isfinite(x) for x in v.values()) for v in (start,target,saved))
        delta={axis:(target[axis]-start[axis]+180)%360-180 for axis in AXES} if valid else {}
        self.check(valid and abs(abs(delta['X'])-180)<=.1 and abs(delta["Y'"])<=.1 and abs(delta["Z''"])<=.1,f'{run.name}.actual_direction_reversal',delta=delta)
        self.check(valid and all(abs((saved[a]-target[a]+180)%360-180)<=.1 for a in AXES),f'{run.name}.saved_angles_match_this_run_target')
        self.check(q['operation']=='C-20' and q['draw_region_assignment'] is True and q['blank_contour_committed'] is True,f'{run.name}.plan_blank_semantics')
        tools=q['automatic_tools']
        self.check(len(tools)==2 and {x['tool'] for x in tools}=={'凸模','凹模'} and all(x['model_mode']=='自动' and x['automatic_contact'] is True for x in tools),f'{run.name}.automatic_tool_semantics')
        self.check(q['friction']['kind']=='库仑摩擦' and q['reference_input_present'] is True and len(q['measurement_plans'])==1 and bool(q['measurement_plans'][0]) and q['free_springback'] is True,f'{run.name}.process_semantics')
        facts['semantic_values']=values

    def business_actions(self,run,stages):
        """Check requested route separately from later persistence comparisons."""
        forming=stages['forming_check']['data']
        measured=forming['after']['result_evidence']
        numbers=measured.get('numeric_results',{})
        stability=forming['result_stability']
        self.check(forming['after'].get('busy') is False and measured.get('ok') is True
                   and set(numbers)=={'weight_kg','force_kn'}
                   and all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and v>0 for v in numbers.values())
                   and stability['observed_samples']>=2 and stability['observed_interval_s']>=stability['required_interval_s'],
                   f'{run.name}.forming_results_positive_and_stable',numeric_results=numbers)
        plan=stages['create_plan']['data']; assignment=stages['assign_operation']['data']
        self.check(plan['operation']==assignment['operation']=='C-20' and plan['after']['ok'] is True
                   and assignment['after']['ok'] is True and assignment['feature']=='拉延区域 1',
                   f'{run.name}.c20_draw_assignment_route')
        editor=stages['visit_die_face']['data']
        self.check(editor['editor_left'] is True and editor['geometry_generated'] is False,
                   f'{run.name}.die_face_editor_visited_and_left')
        blank=stages['copy_blank']['data']
        self.check(blank['source']=='料片轮廓线 - 成型检查部分' and blank['submission_verified'] is True,
                   f'{run.name}.forming_blank_source_committed')
        tools=stages['configure_tools']['data']; visibility=tools['recorded_visibility']
        visible=visibility['after']; icons=visible['icons']
        self.check(tools['visibility_requested'] is True and visibility['visibility_verified'] is True
                   and visibility['visibility_only'] is True and visibility['enabled_state_verified'] is False
                   and visible['visibility']=='visible' and visibility['visible_samples']>=2
                   and set(icons)=={'header','slide','convex','bed','concave'}
                   and all(icon['ok'] is True and icon['state']=='visible' for icon in icons.values()),
                   f'{run.name}.eyes_visible_separate_from_enabled')
        capture=self.track(visible['capture']['path'])
        self.check(capture.is_relative_to(run),f'{run.name}.tool_visibility_capture_owned')
        friction=stages['set_friction']['data']['readback']
        self.check(friction['ok'] is True and friction['selected']=='库仑摩擦',f'{run.name}.coulomb_friction_readback')
        reference=stages['add_reference']['data']
        self.check(reference['reverse_requested'] is False and reference['readback']['ok'] is True
                   and all('反转' not in step for step in reference['steps']),
                   f'{run.name}.reference_committed_without_reverse')
        springback=stages['set_springback']['data']
        self.check(len(springback['plans'])==1 and bool(springback['plans'][0])
                   and springback['readback']['ok'] is True and springback['readback']['kind']=='自由回弹',
                   f'{run.name}.one_free_springback_plan')

    def lifecycle(self,run,record,stages,facts):
        rows=self.jsonl(run/'lifecycle-events.jsonl')
        self.check([r['sequence'] for r in rows]==list(range(1,len(rows)+1)) and len({r['run_id'] for r in rows})==1 and [r['monotonic_ms'] for r in rows]==sorted(r['monotonic_ms'] for r in rows),f'{run.name}.lifecycle_sequence_identity_time')
        self.check([r['operation'] for r in rows if r['operation']!='resource_sample']==LIFECYCLE,f'{run.name}.lifecycle_exact_order')
        baseline=rows[0]['baseline']
        self.check(baseline==record['baseline']==stages['attach']['data']['baseline'],f'{run.name}.baseline_record_consistent')
        created=next(r for r in rows if r['operation']=='create_design')
        reopened=next(r for r in rows if r['operation']=='reopen_design')
        saved=[r for r in rows if r['operation']=='save_design']
        closed=[r for r in rows if r['operation']=='close_design']
        opened=next(r for r in rows if r['operation']=='same_file_opened')
        ids=[created['document_id'],reopened['document_id']]
        self.check(ids[0]!=ids[1] and len(closed)==2 and [r['document_id'] for r in closed]==ids and all(r['close_verified'] is True for r in closed),f'{run.name}.both_distinct_owned_closes')
        baseline_tokens=sorted(x['token'] for x in baseline)
        self.check(all(sorted(r['remaining_tokens'])==baseline_tokens for r in closed),f'{run.name}.baseline_only_after_both_closes')
        self.check(len(saved)==2 and [r['document_id'] for r in saved]==ids and all(r['save_verified'] is True for r in saved),f'{run.name}.both_owned_saves')
        file=record['final_file']
        self.check(pathkey(file['path'])==pathkey(record['persistence']['same_file'])==pathkey(opened['path'])==pathkey(saved[0]['file']['path'])==pathkey(saved[1]['file']['path'])==pathkey(stages['start_simulation']['data']['path']),f'{run.name}.same_exact_file_lifecycle')
        self.check(opened['sha256']==saved[0]['file']['sha256'],f'{run.name}.same_saved_bytes_reopened')
        self.check(digest(self.track(file['path']))==file['sha256']==saved[-1]['file']['sha256'],f'{run.name}.final_file_hash')
        self.check(created['sequence']<saved[0]['sequence']<closed[0]['sequence']<reopened['sequence']<opened['sequence']<saved[1]['sequence']<closed[1]['sequence'],f'{run.name}.save_close_reopen_order')
        for label in ('cleanup','disconnect'):
            self.check(record[label]['ok'] is True,f'{run.name}.{label}_public_success')
        clean=record['cleanup']['data']
        self.check(clean['cleanup_ok'] and clean['owned_documents_remaining']==0 and clean['unknown_documents_remaining']==0 and not clean['errors'],f'{run.name}.final_no_owned_or_unknown')
        self.check(record['disconnect']['postconditions']['workers_stopped'] is True and record['disconnect']['data']['application_exit_requested'] is False,f'{run.name}.disconnect_workers_only')
        samples=[r['sample'] for r in rows if r['operation']=='resource_sample']
        self.check(samples==record['resources'] and samples[0]['design_count']==samples[-1]['design_count']==len(baseline) and samples[-1]['owned_count']==0,f'{run.name}.resource_inventory_endpoints')
        facts.update(baseline_count=len(baseline),owned_remaining=0,unknown_remaining=0,final_file=file,resources=samples)
        return baseline,created,reopened

    def workers(self,run,identity,stages):
        if identity is None:
            raise ValueError('Cannot check raw inventories without successful lifecycle identities')
        baseline,created,reopened=identity
        baseline_rids={tuple(x['metadata']['runtime_id']) for x in baseline}
        baseline_names={tuple(x['metadata']['runtime_id']):x['name'] for x in baseline}
        owned_rids={tuple(created['design']['metadata']['runtime_id']),tuple(reopened['design']['metadata']['runtime_id'])}
        close_rids={tuple(created['design']['metadata']['close_rid']),tuple(reopened['design']['metadata']['close_rid'])}
        events=self.jsonl(run/'events.jsonl')
        starts=[x for x in events if x['event']=='worker_start']; ends={x['call_id']:x for x in events if x['event']=='worker_end'}
        closed={x['call_id']:x for x in events if x['event']=='worker_job_closed'}
        self.check(bool(starts) and len(starts)==len(ends)==len(closed),f'{run.name}.all_worker_calls_accounted')
        self.check(not any(x['event'] in {'worker_failed','worker_reap_failed'} for x in events),f'{run.name}.no_failed_worker_events')
        inventories=[]; closes=[]; business=[]
        for start in starts:
            call=start['call_id']; command=start['command']
            self.check('-I' in command and 'af_r13_bridge.adapters.uia_worker' in command and Path(command[0]).resolve()==ROOT/'.venv/Scripts/python.exe',f'{run.name}.isolated_new_worker_command',call_id=call)
            self.check(call in ends and ends[call]['returncode']==0 and call in closed and closed[call]['active_processes_remaining']==0,f'{run.name}.worker_exit_confirmed',call_id=call)
            request=self.json(start['files']['request.json']); response=self.json(start['files']['response.json'])
            self.check(response.get('ok') is True,f'{run.name}.raw_worker_response_ok',call_id=call)
            if request['action']=='tabs':
                observed={tuple(tab['rid']) for tab in response['tabs']}
                inventories.append(observed)
                self.check(baseline_rids.issubset(observed) and observed.issubset(baseline_rids|owned_rids) and len(observed-baseline_rids)<=1,f'{run.name}.raw_inventory_baseline_and_one_owned',call_id=call)
                original_tabs=[tab for tab in response['tabs'] if tuple(tab['rid']) in baseline_rids]
                self.check(all([child['name'] for child in tab['children'] if child['class']=='AFDesignBarToolButton']==[baseline_names[tuple(tab['rid'])]] for tab in original_tabs),f'{run.name}.raw_baseline_names_unchanged',call_id=call)
            if request['action']=='click' and tuple(request.get('rid',[])) in close_rids:
                closes.append(tuple(request['rid']))
            if request['action']=='business':
                business.append(request['operation'])
                self.check(tuple(request['document_rid']) in owned_rids,f'{run.name}.business_worker_owned_rid',call_id=call)
                stage_name=request['operation']
                if stage_name=='read_business':
                    stage_name='read_before' if business.count('read_business')==1 else 'read_after'
                self.check(response==stages[stage_name]['data'],f'{run.name}.raw_business_result_matches_workflow',stage=stage_name)
                for artifact in response.get('evidence',[]):
                    path=self.track(artifact)
                    self.check(path.is_relative_to(run),f'{run.name}.business_evidence_owned',file=str(path))
            for suffix in ('.isolation.json','.isolation.before.json'):
                audit=Path(start['files']['response.json']).with_suffix(suffix)
                self.check(self.json(audit).get('ok') is True,f'{run.name}.worker_isolation_clean',file=str(audit))
        self.check(inventories and inventories[0]==inventories[-1]==baseline_rids,f'{run.name}.raw_initial_final_baseline_only')
        self.check(len(closes)==2 and set(closes)==close_rids,f'{run.name}.raw_two_owned_close_clicks')
        self.check(business==BUSINESS[:-1]+['read_business','read_business'],f'{run.name}.raw_business_worker_order')

    def run(self,index,entry):
        run=self.directory/f'run-{index}'
        record=self.json(run/'workflow.json')
        facts={'run':index,'seconds':record.get('elapsed_seconds'),'required_stages':26}
        self.result['runs'].append(facts)
        self.check(entry==record,f'{run.name}.chain_workflow_identical')
        self.check(record.get('ok') is True and record.get('e2e_ok') is True and record.get('from_empty') is True and record.get('elapsed_seconds',math.inf)<1200,f'{run.name}.complete_under_20_minutes')
        self.check(record.get('evidence_errors')==[] and not record.get('error'),f'{run.name}.no_evidence_write_failure')
        stages_list=record.get('stages',[])
        self.check([s['name'] for s in stages_list]==STAGES,f'{run.name}.all_26_stages_exact_order')
        self.check(all(s.get('status')=='passed' and s.get('result',{}).get('ok') is True for s in stages_list),f'{run.name}.all_required_stage_postconditions')
        stages={s['name']:s.get('result',{}) for s in stages_list}
        self.check([x['role'] for x in record.get('inputs',[])]==['part','material'],f'{run.name}.both_inputs_recorded')
        for item in record.get('inputs',[]):
            self.check(Path(item['path']).resolve().parent==run and digest(self.track(item['path']))==item['sha256']==digest(self.track(item['source'])),f'{run.name}.input_source_copy_hash',role=item['role'])
        self.section(f'{run.name}.business_actions_section',lambda:self.business_actions(run,stages))
        self.section(f'{run.name}.gui_raw_section',lambda:self.run_gui(run,stages,facts))
        self.section(f'{run.name}.native_raw_section',lambda:self.run_native(run,stages,facts))
        self.section(f'{run.name}.business_semantics_section',lambda:self.semantics(run,record,stages,facts))
        identity=self.section(f'{run.name}.lifecycle_section',lambda:self.lifecycle(run,record,stages,facts))
        self.section(f'{run.name}.worker_raw_section',lambda:self.workers(run,identity,stages))

    def execute(self):
        chain=self.json(self.directory/'chain.json')
        cli=self.json(self.directory/'result.json')
        self.check(cli.get('ok') is True and cli.get('operation')=='chain' and cli.get('data')==chain,'cli_complete_result')
        self.check(chain.get('ok') is True and chain.get('e2e_ok') is True and chain.get('three_consecutive_verified') is (self.expected_runs==3) and chain.get('runs_requested')==self.expected_runs and len(chain.get('runs',[]))==self.expected_runs,'expected_complete_run_count')
        self.check(chain.get('source_unchanged') is True,'chain_source_unchanged')
        self.section('frozen_code_manifest',self.manifest)
        self.check(chain.get('version_fingerprint')==self.result.get('version_fingerprint'),'chain_version_matches_manifest')
        for name in ('isolation-before.json','isolation-after.json'):
            self.check(self.json(self.directory/name).get('ok') is True,'top_level_'+name)
        for index,entry in enumerate(chain.get('runs',[]),1):
            self.section(f'run-{index}.record_section',lambda i=index,e=entry:self.run(i,e))
        run_ids=[x.get('gui_job',{}).get('id') for x in self.result['runs']]
        self.check(len(run_ids)==self.expected_runs and all(run_ids) and len(set(run_ids))==self.expected_runs,'distinct_gui_jobs')
        final_paths=[x.get('final_file',{}).get('path') for x in self.result['runs']]
        self.check(len(final_paths)==self.expected_runs and all(final_paths) and len(set(final_paths))==self.expected_runs,'distinct_output_files')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('directory',type=Path)
    parser.add_argument('--expected-runs',type=int,choices=(1,3),default=3,
                        help='Default 3 is acceptance; 1 is a complete single-run diagnostic only')
    args=parser.parse_args()
    directory=args.directory.resolve()
    audit=Audit(directory,args.expected_runs)
    audit.section('acceptance_structure',audit.execute)
    audit.result['ok']=not audit.result['failures']
    audit.result['three_consecutive_verified']=audit.result['ok'] and args.expected_runs==3
    audit.result['diagnostic_only']=args.expected_runs==1
    if args.expected_runs==1:
        audit.result['scope']='Single complete run diagnostic only; not consecutive-three acceptance. Raw GUI/native completion, persistence and closes are still required.'
    audit.result['check_count']=len(audit.result['checks'])
    output=directory/'acceptance-audit.json'
    if output.exists():
        output=directory/('acceptance-audit-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    output.write_text(json.dumps(audit.result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'ok':audit.result['ok'],'three_consecutive_verified':audit.result['three_consecutive_verified'],
                      'diagnostic_only':audit.result['diagnostic_only'],'checks':audit.result['check_count'],
                      'failures':[x['name'] for x in audit.result['failures']],'report':str(output)},ensure_ascii=True))
    return 0 if audit.result['ok'] else 1


if __name__=='__main__':
    raise SystemExit(main())
