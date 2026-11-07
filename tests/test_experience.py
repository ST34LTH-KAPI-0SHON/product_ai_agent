import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from product_ai_agent.analytics import aggregate
from product_ai_agent.experience import start, session, save, choose, pause, impact
from product_ai_agent.ingestion import source_output
from product_ai_agent.presentation import publish_report, doctor, export_project
from product_ai_agent.storage import AgentError, Store, atomic, encode
from product_ai_agent.workflow import validation


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path/'product.md').write_text('TEST ONLY: a synthetic product with tasks and invitations.',encoding='utf-8')
    (tmp_path/'table.csv').write_text('period,n,d\np1,2,5\np2,3,10\n',encoding='utf-8')
    result=start('projects/local-test','Test product','Which small action should we choose?',
                 [tmp_path/'product.md',tmp_path/'table.csv'],synthetic=True)
    store=Store(tmp_path/result['project'])
    store.mutate('web-fixture','test',{},lambda:source_output(store,b'TEST ONLY webpage',
        {'format':'html','blocks':[{'block_id':'html-1','text':'TEST ONLY public page fixture'}],'tables':[]},'.html','TEST ONLY web',
        {'type':'url','value':'https://example.org/'},'synthetic',[],'https://example.org/'))
    table=next(s for s in store.registry('sources.jsonl') if s['kind']=='table')
    store.mutate('calc-test','test',{},lambda:aggregate(store,table['id'],{'metric_name':'Conversion','definition':'n/d',
        'operation':'ratio_of_sums','numerator_column':'n','denominator_column':'d','group_by':['period'],'units':'proportion'}))
    return store,table


def brief():
    return {'title':'Test decision <script>not executable</script>','recommendation':'Check the measurement before an expensive change.',
        'reasons':[{'text':'The observed numbers differ.','claim_ids':['clm-test']}],
        'confidence':'High confidence in arithmetic; the mechanism is untested.',
        'first_steps':[{'action':'Verify attribution','deliverable':'A checked definition','when':'Before the pilot','owner':None}],
        'do_not':['Do not promise growth.'],'success_rule':'Agree a useful effect and guardrails before launch.',
        'unknowns':['Budget'],'revisit_when':['New data contradicts the input.'],'expected_effect':'Not measured.','synthetic':True}


def quality_review():
    return {'strongest_objection':'TEST ONLY: synthetic observations cannot establish real demand.',
        'response_to_objection':'TEST ONLY: check real measurement before selecting an expensive action.',
        'checks':[{'id':name,'status':'pass','explanation':'TEST ONLY: checked the limited synthetic material for this criterion.',
                   'claim_ids':['clm-test'] if name=='evidence' else []} for name in ['evidence','alternatives','promises','next_step']],
        'alternatives':[{'id':ident,'label':label,'action':action,'objection':'TEST ONLY: this alternative still requires real-world evidence.', 'kind':kind}
            for ident,label,action,kind in [
                ('keep','Keep current product','Keep the current product without a new pilot.','keep'),
                ('improve','Small measurement pilot','Check the measurement before a limited product change.','investigate'),
                ('pivot','Change target audience','Test a different target audience with limited resources.','change'),
                ('close','Close the direction','Stop the current direction and document the tradeoff.','close')]],
        'promises':[{'text':'TEST ONLY: the observed numbers differ in the provided fixture.','claim_ids':['clm-test']}],
        'promises_applicability':'TEST ONLY: no promise of future uplift is made from synthetic observations.'}


def await_choice(store,table):
    claim={'id':'clm-test','text':'An observed difference in TEST ONLY data.','type':'observation',
        'evidence':[{'source_id':table['id'],'locator':'record:1'}],'calculation_ids':[],
        'basis_claim_ids':[],'limitations':['TEST ONLY'],'status':'active','supersedes':None,'created_at':'2026-11-07T12:00:00Z'}
    options=[{'id':v,'label':v} for v in ['keep','improve','pivot','close']]
    update={'stage':'awaiting_decision','documents':{'brief.md':'A decision question','research-plan.md':'A useful check',
        'reports/analysis.md':'TEST ONLY synthetic arithmetic and limitations.',
        'reports/options.md':'Four distinct TEST alternatives'},'claims':[claim],'decision_brief':brief(),'quality_review':quality_review(),
        'pending_decision':{'question':'TEST choice?','options':options},'next_action':'Wait for the real user choice.'}
    atomic(store.path('drafts/update.json'),encode(update))
    return save(None,'drafts/update.json')


def test_simple_user_journey(project,tmp_path):
    store,table=project
    assert session()['stage']=='brief'
    assert len(store.registry('sources.jsonl'))==3
    result=await_choice(store,table)
    assert result['status']=='awaiting_user'
    assert store.state()['revision']==4
    # Replaying the save does not add checkpoints or claims.
    save(None,'drafts/update.json')
    assert store.state()['revision']==4 and len(store.registry('claims.jsonl'))==1
    choose(None,'improve','SIMULATED TEST ONLY: choose improve')
    assert session()['stage']=='strategy'
    assert not session()['quality']['ready']  # The pre-choice self-review cannot approve a post-choice answer.
    with pytest.raises(AgentError):publish_report()
    update={'stage':'complete','documents':{'reports/strategy.md':'TEST product and communication strategy',
        'reports/validation-plan.md':'TEST comparison, window and guardrails','reports/run-summary.md':'TEST run; costs unknown'},
        'decision_brief':brief(),'quality_review':quality_review(),'next_action':'Review the completed decision.'}
    atomic(store.path('drafts/final.json'),encode(update));save(None,'drafts/final.json')
    report=publish_report()
    text=Path(report['report']).read_text(encoding='utf-8')
    assert '&lt;script&gt;not executable&lt;/script&gt;' in text
    assert '<script>not executable</script>' not in text
    assert 'Verify attribution' in text and 'SIMULATED TEST ONLY' in text
    assert doctor(str(store.root))['ready']
    assert session()['stage']=='complete'
    result=export_project(None,True)
    restored=tmp_path/'restored';restored.mkdir()
    with zipfile.ZipFile(result['path']) as z:z.extractall(restored)
    assert validation(Store(restored/'local-test'))['valid']
    summary=export_project(None,False)
    with zipfile.ZipFile(summary['path']) as z:
        assert not any('/inputs/' in n or '/.operations/' in n or '/sources/' in n for n in z.namelist())
    revised=brief();revised['recommendation']='TEST ONLY: a revised reviewed recommendation.'
    update={'stage':'complete','decision_brief':revised,'quality_review':quality_review(),
            'next_action':'Read the revised result.'}
    atomic(store.path('drafts/revised.json'),encode(update));save(None,'drafts/revised.json')
    # No explicit report call: export must render the current card itself.
    fresh=export_project(None,False)
    with zipfile.ZipFile(fresh['path']) as z:
        assert b'a revised reviewed recommendation' in z.read('local-test/reports/decision.html')


def test_no_choice_and_no_fake_completion(project):
    store,table=project
    with pytest.raises(AgentError):choose(None,'improve','')
    await_choice(store,table)
    update={'stage':'complete','decision_brief':brief(),'next_action':'Not allowed without a choice'}
    atomic(store.path('drafts/invalid.json'),encode(update))
    with pytest.raises(AgentError):save(None,'drafts/invalid.json')
    assert session()['stage']=='awaiting_decision'


def test_start_rejects_bad_material_before_creating_project(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path/'bad.csv').write_text('a,a\n1,2\n')
    with pytest.raises(AgentError):start('projects/bad','Bad','Question',[tmp_path/'bad.csv'])
    assert not (tmp_path/'projects/bad').exists()


def test_saved_010_calculations_remain_valid(project):
    store,table=project
    spec={'metric_name':'Old calculation','definition':'n/d','operation':'ratio_of_sums',
          'numerator_column':'n','denominator_column':'d','units':'proportion'}
    store.mutate('old-version','test',{},lambda:aggregate(store,table['id'],spec,tool_version='0.1.0'))
    assert validation(store)['valid']


def test_windows_project_inherits_workspace_permissions(project):
    if os.name!='nt':pytest.skip('Windows ACL regression')
    store,_=project
    result=subprocess.run(['powershell','-NoProfile','-Command','[System.IO.Directory]::GetAccessControl($env:AGENT_TEST_PROJECT).AreAccessRulesProtected'],
        env={**os.environ,'AGENT_TEST_PROJECT':str(store.root)},capture_output=True,text=True)
    assert result.returncode==0
    assert result.stdout.strip()=='False'


def test_no_existing_session_is_a_plain_start_message(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert session()['needs_project']
    assert session()['projects']==[]


def test_pause_and_new_evidence_review(project):
    store,table=project
    assert pause()['status']=='paused'
    assert pause()['status']=='paused'
    await_choice(store,table)
    choose(None,'improve','SIMULATED TEST ONLY: improve')
    atomic(store.path('inputs/new.csv'),b'period,n,d\np1,1,50\np2,1,100\n')
    from product_ai_agent.ingestion import ingest_file
    new=store.mutate('new-table','ingest',{},lambda:ingest_file(store,'inputs/new.csv',','))['data']['source_id']
    result=impact(None,new,table['id'])
    assert result['requires_review']
    assert result['affected_claims'][0]['id']=='clm-test'
    assert len(result['affected_decisions'])==1
    assert session()['needs_review']
    assert store.json('review.json')['status']=='pending'


def test_history_damage_is_detected(project):
    store,table=project
    await_choice(store,table)
    atomic(store.path('checkpoints/rev-000001/files/brief.md'),b'corrupted archived brief')
    assert not validation(store)['valid']


def test_retired_basis_remains_in_decision_history(project):
    from product_ai_agent.workflow import check_decision, record_claim
    store,table=project
    await_choice(store,table)
    choose(None,'improve','SIMULATED TEST ONLY: improve')
    original=store.registry('decisions.jsonl')[0]
    replacement=dict(store.registry('claims.jsonl')[0])
    replacement.update(id='clm-corrected',supersedes='clm-test',text='TEST ONLY: a corrected observation from the same fixture.')
    store.mutate('retire-basis','test',{},lambda:record_claim(store,replacement))
    result=validation(store)
    assert result['valid']
    assert any('Historical decision' in str(w) for w in result['warnings'])
    assert store.registry('decisions.jsonl')[0]==original
    # A newly recorded decision cannot reuse the retired grounds.
    with pytest.raises(AgentError):check_decision(store,original,store.registry('claims.jsonl'))
