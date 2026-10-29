"""Increment regressions: pure rules and existing API, no desktop access."""
from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from af_r13_bridge import window_policy as p
from af_r13_bridge.api import BUSINESS_OPERATIONS, Bridge
from af_r13_bridge.catalog import load_catalog, query_catalog
from af_r13_bridge.models import BridgeError
from af_r13_bridge.__main__ import main


class FixedWindowTests(unittest.TestCase):
    def test_client_contract_uses_client_not_outer_dimensions(self):
        self.assertEqual(p.expected_profile(1,9)['client_rect'],[30,50,1830,1010])

    def test_geometry_drift_identity_dpi_and_minimize_are_rejected(self):
        expected=p.expected_profile(1,9)
        for key,value in [('hwnd',2),('pid',10),('dpi',120),('maximized',True),
                          ('client_rect',[31,50,1831,1010]),('minimized',True)]:
            with self.subTest(key=key),self.assertRaises(BridgeError):
                p.assert_profile({**expected,key:value},expected)

    def test_disabled_main_is_valid_geometry_while_owned_modal_is_active(self):
        expected=p.expected_profile(1,9)
        self.assertEqual(p.assert_profile({**expected,'enabled':False},expected)['hwnd'],1)

    def test_invalid_configuration_and_empty_client_rejected(self):
        for cfg in [{}, {**p.DEFAULT_WINDOW,'dpi':True}, {**p.DEFAULT_WINDOW,'client_width':0}]:
            with self.subTest(cfg=cfg),self.assertRaises(BridgeError):
                p.expected_profile(1,9,cfg)
        with self.assertRaises(BridgeError):
            p.rectangle([1,2,1,3])


class InputContextTests(unittest.TestCase):
    def setUp(self):
        self.main=dict(hwnd=1,pid=9,owner=0,class_name='Main',enabled=True,rect=[0,0,900,900])
        self.dialog=dict(hwnd=2,pid=9,owner=1,class_name='#32770',enabled=True,rect=[100,100,800,700])
        self.edit=dict(hwnd=3,pid=9,rect=[150,300,600,330])
        self.popup=dict(hwnd=4,pid=9,owner=0,class_name='Auto-Suggest Dropdown',rect=[150,330,600,450])
        self.gui=dict(thread_id=7,focus=3,capture=0)

    def test_owned_dialog_can_be_targeted(self):
        self.assertEqual(p.input_context(2,1,[self.main,self.dialog],self.gui,0),[2,1])

    def test_main_cannot_receive_input_behind_dialog(self):
        with self.assertRaises(BridgeError):
            p.input_context(1,1,[self.main,self.dialog],self.gui,0)

    def test_ownerless_extra_window_is_not_ignored(self):
        with self.assertRaises(BridgeError):
            p.input_context(2,1,[self.main,self.dialog,self.popup],self.gui,0)

    def test_hidden_capture_redirect_rejected_even_without_visible_popup(self):
        with self.assertRaises(BridgeError) as caught:
            p.input_context(1,1,[self.main],{**self.gui,'capture':88},88)
        self.assertEqual(caught.exception.code,'MOUSE_CAPTURE_REDIRECTED')

    def test_capture_by_target_child_is_allowed(self):
        self.assertEqual(p.input_context(1,1,[self.main],{**self.gui,'capture':88},1),[1])

    def test_known_qt_tooltip_is_not_a_modal_but_capture_is_still_checked(self):
        for class_name in ('Qt5QWindowToolTipDropShadowSaveBits','Qt5QWindowToolTipSaveBits','tooltips_class32'):
            tooltip=dict(hwnd=9,pid=9,owner=0,class_name=class_name)
            with self.subTest(class_name=class_name):
                self.assertEqual(p.input_context(1,1,[self.main,tooltip],self.gui,0),[1])
                with self.assertRaises(BridgeError):
                    p.input_context(1,1,[self.main,tooltip],{**self.gui,'capture':9},9)
        with self.assertRaises(BridgeError):
            p.input_context(1,1,[self.main,{**tooltip,'class_name':'Qt5QWindow'}],self.gui,0)

    def test_owner_cycle_and_foreign_process_rejected(self):
        for changed in [{**self.dialog,'owner':2},{**self.dialog,'pid':100}]:
            with self.assertRaises(BridgeError):
                p.owner_chain(2,1,[self.main,changed])

    def suggestions(self,**changes):
        values=dict(dialog=self.dialog,edit=self.edit,popups=[self.popup],gui=self.gui,
                    edit_gui=self.gui,popup_gui=self.gui,edit_root=2)
        values.update(changes)
        return p.filename_suggestions(**values)

    def test_filename_popup_has_narrow_allowance(self):
        allowed=self.suggestions()
        self.assertEqual(allowed,{4})
        self.assertEqual(p.input_context(2,1,[self.main,self.dialog,self.popup],self.gui,0,allowed),[2,1])

    def test_filename_allowance_cannot_override_capture_guard(self):
        with self.assertRaises(BridgeError):
            p.input_context(2,1,[self.main,self.dialog,self.popup],{**self.gui,'capture':4},4,self.suggestions())

    def test_filename_list_rejects_changed_focus_thread_process_and_parent(self):
        changes=[dict(gui={**self.gui,'focus':99}),dict(edit_gui={'thread_id':8}),
                 dict(popup_gui={'thread_id':8}),dict(edit={**self.edit,'pid':10}),
                 dict(edit_root=1),dict(dialog={**self.dialog,'enabled':False}),
                 dict(popups=[self.popup,self.popup])]
        for change in changes:
            with self.subTest(change=change),self.assertRaises(BridgeError):
                self.suggestions(**change)

    def test_filename_list_rejects_detached_or_overflowing_geometry(self):
        for rect in [[151,330,600,450],[150,331,600,450],[150,330,601,450],[150,330,600,701]]:
            with self.subTest(rect=rect),self.assertRaises(BridgeError):
                self.suggestions(popups=[{**self.popup,'rect':rect}])

    def test_popup_does_not_become_a_click_target(self):
        with self.assertRaises(BridgeError):
            p.input_context(4,1,[self.main,self.dialog,self.popup],self.gui,0,self.suggestions())

    def test_real_file_list_can_extend_below_small_dialog_within_work_area(self):
        # Observed during merge lifecycle-02; right/left/top still attach exactly.
        self.dialog['rect']=[30,50,685,466]
        self.edit['rect']=[234,394,440,411]
        self.popup['rect']=[234,411,440,503]
        self.assertEqual(self.suggestions(work_rect=[0,0,1920,1040]),{4})
        with self.assertRaises(BridgeError):
            self.suggestions(work_rect=[0,0,1920,480])

    def test_ownerless_afmenu_requires_exact_fresh_proof(self):
        popup=dict(hwnd=5,pid=9,owner=0,class_name='Qt5QWindowPopupDropShadowSaveBits',rect=[20,30,60,90])
        proof={**popup,'root_class':'AFMenu'}
        self.assertEqual(p.input_context(5,1,[self.main,popup],self.gui,0,popup_proof=proof),[5,1])
        for bad in [None,{**proof,'rect':[20,30,61,90]},{**proof,'root_class':'UnknownMenu'},
                    {**proof,'pid':10}]:
            with self.subTest(proof=bad),self.assertRaises(BridgeError):
                p.input_context(5,1,[self.main,popup],self.gui,0,popup_proof=bad)

    def test_verified_menu_does_not_allow_another_popup_or_hidden_capture(self):
        popup=dict(hwnd=5,pid=9,owner=0,class_name='Qt5QWindowPopupDropShadowSaveBits',rect=[20,30,60,90])
        proof={**popup,'root_class':'AFMenu'}
        with self.assertRaises(BridgeError):
            p.input_context(5,1,[self.main,popup,self.dialog],self.gui,0,popup_proof=proof)
        with self.assertRaises(BridgeError):
            p.input_context(5,1,[self.main,popup],{**self.gui,'capture':88},88,popup_proof=proof)

    def test_material_preview_requires_same_parent_and_fresh_classified_snapshot(self):
        preview=dict(hwnd=6,pid=9,owner=1,class_name='Qt5QWindow',rect=[200,30,600,700],title='Material preview')
        proof={**preview,'root_class':'AutoForm::MaterialPreviewerView'}
        windows=[self.main,self.dialog,preview]
        allowed=p.material_preview_allowance(2,1,windows,proof)
        self.assertEqual(p.input_context(2,1,windows,self.gui,0,allowed),[2,1])
        for bad in [{**proof,'root_class':'Other'},{**proof,'owner':0},{**proof,'title':'Changed'}]:
            with self.assertRaises(BridgeError):
                p.material_preview_allowance(2,1,windows,bad)
        with self.assertRaises(BridgeError):
            p.material_preview_allowance(1,1,windows,proof)

    def test_material_disclaimer_has_explicit_ownerless_rule_but_unknown_dialog_does_not(self):
        dialog=dict(hwnd=8,pid=9,owner=0,class_name='Qt5QWindow',rect=[200,300,500,650])
        proof={**dialog,'root_class':'AutoForm::MaterialDisclaimerView'}
        self.assertEqual(p.input_context(8,1,[self.main,dialog],self.gui,0,popup_proof=proof),[8,1])
        with self.assertRaises(BridgeError):
            p.input_context(8,1,[self.main,dialog],self.gui,0,popup_proof={**proof,'root_class':'UnknownDialog'})


class CatalogIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.source=self.directory/'helpLinks.cfg'
        self.source.write_text('First /user-interface/part-stage/import\nSecond /user-interface/part-stage/import\n'
                               'Third /user-interface/process-stage/friction\n',encoding='utf-8')

    def test_index_deduplicates_and_retains_source_lines(self):
        catalog=load_catalog(self.source)
        self.assertEqual(len(catalog.entries),2)
        entry=catalog.search('import')[0]
        self.assertEqual(entry['source_lines'],[1,2])
        self.assertFalse(entry['callable_verified'])
        self.assertEqual(entry['local_availability'],'unknown')
        entry['presenters'].clear()
        self.assertEqual(len(catalog.search('import')[0]['presenters']),2)

    def test_python_catalog_does_not_attach_or_claim_business(self):
        adapter=Mock()
        bridge=Bridge(adapter,self.directory/'run')
        result=bridge.call('catalog',dict(source=str(self.source),query='friction'))
        self.assertTrue(result['ok'])
        self.assertEqual(result['data']['match_count'],1)
        self.assertEqual(result['evidence'],[])
        self.assertEqual(adapter.mock_calls,[])
        self.assertIsNone(bridge.lifecycle.baseline)

    def test_cli_catalog_never_constructs_adapter(self):
        output=StringIO()
        with patch('af_r13_bridge.__main__._adapter_factory',side_effect=AssertionError('UI forbidden')),redirect_stdout(output):
            code=main(['catalog','--source',str(self.source),'--query','import'])
        self.assertEqual(code,0)
        self.assertEqual(json.loads(output.getvalue())['data']['match_count'],1)

    def test_empty_query_and_no_match_have_honest_counts(self):
        self.assertEqual(query_catalog('',self.source)['match_count'],2)
        result=query_catalog('not-present',self.source)
        self.assertEqual((result['entry_count'],result['match_count']),(2,0))

    def test_bad_query_is_structured_api_failure(self):
        result=Bridge(Mock(),self.directory/'run').call('catalog',dict(source=str(self.source),query=[]))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'],'invalid_request')

    def test_existing_business_allowlist_is_unchanged(self):
        self.assertEqual(BUSINESS_OPERATIONS,{
            'import_part','extract_part','set_direction','set_material','forming_check','create_plan',
            'assign_operation','visit_die_face','copy_blank','set_friction','configure_tools',
            'add_reference','set_springback','start_simulation','read_business','solve'})


if __name__=='__main__':
    unittest.main()
