"""Worker-side, allowlisted business dispatcher. No session discovery here."""
from pathlib import Path
import time
from .ui import BusinessSession


OPERATIONS = {
    'import_part','extract_part','set_direction','set_material','forming_check',
    'create_plan','assign_operation','visit_die_face','copy_blank','set_friction',
    'configure_tools','add_reference','set_springback','start_simulation','read_business',
}


def run(request):
    from . import part, plan, process
    from . import simulation
    session = BusinessSession(request)
    operation = request['operation']
    args = request.get('arguments',{})
    if operation not in OPERATIONS:
        raise ValueError('Unregistered business operation')
    actions = {
        'import_part':lambda:part.import_part(session,Path(args['path'])),
        'extract_part':lambda:part.extract_part(session),
        'set_direction':lambda:part.apply_auto_tip_and_reverse(session),
        'set_material':lambda:part.apply_material(session,Path(args['path']),thickness_mm=args.get('thickness_mm',4.0)),
        'forming_check':lambda:part.apply_bending_check(session),
        'create_plan':lambda:plan.create_recorded_plan(session),
        'assign_operation':lambda:plan.activate_recorded_operation(session),
        'visit_die_face':lambda:plan.open_recorded_die_face(session),
        'copy_blank':lambda:plan.copy_recorded_blank(session),
        'set_friction':lambda:process.set_recorded_friction(session),
        'configure_tools':lambda:plan.verify_automatic_tools(session,show_tools=True),
        'add_reference':lambda:process.add_recorded_reference(session,reverse=False),
        'set_springback':lambda:process.ensure_recorded_springback(session),
        'start_simulation':lambda:simulation.start_recorded_simulation(session,Path(args['path']),Path(args['out']),timeout=args.get('timeout',300)),
    }
    if operation=='read_business':
        p = part.read_part_state(session,args['material_stem'],args.get('thickness_mm',4.0),args.get('expected_angles'))
        q = process.read_process_state(session)
        result = {'ok':bool(p['ok'] and q['ok']), 'values':{'part':p.get('values'),'process':q.get('values')},
                  'checks':{'part':p,'process':q}}
    else:
        result = actions[operation]()
    # A business action never gains permission to switch or create a document.
    session.verify_document()
    stamp=str(time.monotonic_ns())
    screenshot=session.screenshot(session.directory/f'{stamp}-{operation}.png')
    dump=session.directory/f'{stamp}-{operation}.json'
    session.dump(dump)
    result.setdefault('evidence',[]).extend([str(screenshot),str(dump)])
    return result
