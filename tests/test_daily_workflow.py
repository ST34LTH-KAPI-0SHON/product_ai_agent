import copy
import hashlib
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from product_ai_agent.experience import start, save, session, track, now_view, expand
from product_ai_agent.followup import Commitment, next_actions
from product_ai_agent.presentation import publish_report, export_project
from product_ai_agent.storage import AgentError, Store, atomic, encode
from product_ai_agent.versions import compare, list_versions
from product_ai_agent.workflow import validation

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def study(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = tmp_path / 'product.md'
    p.write_text('TEST ONLY: the synthetic service exports HTML after owner approval.', encoding='utf-8')
    result = start('projects/daily', 'TEST daily', 'Which existing export barrier should we check?', [p],
                   synthetic=True, mode='diagnosis', depth='quick', routing_reason='TEST: bounded qualitative question, no rollout requested.')
    store = Store(tmp_path / result['project'])
    source = store.registry('sources.jsonl')[0]
    block = result['materials_preview']['sources'][0]['blocks'][0]
    claim = {'id':'fact-export', 'text':'TEST ONLY: HTML export needs owner approval.', 'type':'fact',
             'evidence':[{'source_id':source['id'], 'locator':'block:'+block['block_id']}],
             'calculation_ids':[], 'basis_claim_ids':[], 'limitations':['Synthetic fixture, not a demand measurement.'],
             'status':'active', 'supersedes':None, 'created_at':'2026-11-08T00:00:00Z'}
    return store, claim, result


def card():
    return {'title':'TEST export diagnosis', 'recommendation':'Check how the existing HTML export is found before changing formats.',
            'reasons':[{'text':'HTML export exists, demand is unmeasured.', 'claim_ids':['fact-export']}],
            'confidence':'Synthetic capability only; real demand is unknown.',
            'first_steps':[{'action':'Observe export discovery', 'deliverable':'Recorded barriers or their absence',
                            'when':'After agreeing an owner and consent', 'owner':None}],
            'do_not':['Do not promise PDF or higher revenue.'], 'success_rule':'Separate discovery from format suitability.',
            'unknowns':['Real demand is unmeasured.'], 'revisit_when':['New evidence changes the task or capability.'],
            'expected_effect':'Learning rather than proven uplift.', 'synthetic':True}


def review():
    return {'strongest_objection':'Discoverability may be irrelevant if the real job needs a different format.',
            'response_to_objection':'Check the job before observing discovery, and record unresolved format requirements.',
            'checks':[{'id':key,'status':'pass','explanation':explanation,
                       'claim_ids':['fact-export'] if key=='evidence' else []}
                      for key,explanation in [
                          ('evidence','The narrow HTML capability is in the fixture; demand remains unmeasured.'),
                          ('alternatives','Keeping the current approach and diagnosing the job have different costs and risks.'),
                          ('promises','No new format, conversion effect or revenue benefit is promised.'),
                          ('next_step','The check has a deliverable and explicit conditions; an owner still must be agreed.')]],
            'alternatives':[{'id':'keep','label':'Keep current approach','kind':'keep',
                             'action':'Keep the current export without initiating new research.',
                             'objection':'A real barrier could remain undiscovered and unresolved.'},
                            {'id':'observe','label':'Check the export job','kind':'investigate',
                             'action':'Check the actual job before observing discovery of the existing export.',
                             'objection':'A small qualitative check cannot estimate prevalence or economic value.'}],
            'promises':[], 'promises_applicability':'This diagnosis proposes learning, not a new customer-facing capability.'}


def write(store, payload, filename='update.json'):
    atomic(store.path('drafts/'+filename), encode(payload))
    return save(str(store.root), 'drafts/'+filename)


def finish(study):
    store, claim, _ = study
    return write(store, {'stage':'complete','claims':[claim],'decision_brief':card(),'quality_review':review(),
                         'next_action':'Agree the next diagnostic check.'})


def task(**changes):
    value = {'id':'step-1','action':'Observe export discovery','deliverable':'Observed barriers or their absence',
             'owner':'TEST product lead','due_at':'2026-11-10T18:00:00+03:00','status':'open',
             'confirmation_kind':'synthetic',
             'confirmation_quote':'SIMULATED TEST ONLY: assign the lead and use this deadline.'}
    return {**value, **changes}


def track_payload(store, payload):
    atomic(store.path('drafts/tasks.json'), encode(payload))
    return track(str(store.root), 'drafts/tasks.json')


def test_quick_uses_a_small_checked_result(study):
    store, _, initial = study
    assert initial['materials_preview']['sources'][0]['blocks']
    result = finish(study)
    assert result['route']['stages']==['brief','analysis','complete']
    assert store.state()['revision']==2
    assert result['quality']['ready'] and 'assessment' not in result['quality']
    assert not store.path('research-plan.md').exists()
    assert not store.path('reports/analysis.md').exists()
    assert not store.path('reports/validation-plan.md').exists()
    assert not store.registry('decisions.jsonl')
    assert validation(store)['valid']
    assert len(list_versions(store))==1
    assert not compare(store)['has_previous']
    publish_report(str(store.root))
    html=store.read('reports/decision.html').decode()
    assert 'Следующая проверка' in html and 'Документ ещё готовится' not in html
    assert 'Что делать сейчас' in html and 'Что изменилось' in html


def test_no_review_cannot_be_hidden_in_quick_mode(study):
    store, claim, _ = study
    with pytest.raises(AgentError):
        write(store, {'stage':'complete','claims':[claim],'decision_brief':card(),'next_action':'Not approved.'})
    assert store.state()['revision']==0 and not store.path('versions.jsonl').exists()


def test_quick_quantitative_still_needs_a_real_calculation(study):
    store, _, _ = study
    request=store.json('request.json');request['quantitative']=True
    atomic(store.path('request.json'),encode(request))
    with pytest.raises(AgentError):finish(study)
    assert store.state()['revision']==0


def test_quick_cannot_disguise_a_strategic_choice(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError):
        start('projects/bad','Bad','Choose a direction.',mode='strategy',depth='quick')
    assert not (tmp_path/'projects/bad').exists()


def test_expand_preserves_the_old_answer_and_reopens_research(study):
    store, _, _ = study
    finish(study)
    first=store.read('versions/v0001.json')
    result=expand(str(store.root),'strategy','TEST: need an economic strategic choice beyond this narrow diagnosis.')
    assert result['stage']=='research' and result['route']['depth']=='full'
    assert result['pending_decision'] is None and not result['quality']['ready']
    assert store.read('versions/v0001.json')==first
    assert store.json('request-history/rev-000002.json')['depth']=='quick'
    with pytest.raises(AgentError):publish_report(str(store.root))


@pytest.mark.parametrize('changes',[
    {'confirmation_quote':None}, {'due_at':'2026-11-10'}, {'due_at':'2026-11-10T18:00:00'},
    {'status':'done','resolution':None}, {'status':'cancelled','resolution':None},
    {'status':'done','resolution':'TEST completed','blocked_reason':'Still blocked'}])
def test_commitments_need_real_confirmation_and_unambiguous_dates(changes):
    with pytest.raises(ValidationError):Commitment.model_validate(task(**changes))


def test_proposals_do_not_become_human_commitments(study):
    store, _, _ = study
    finish(study)
    view=now_view(str(store.root))
    assert view['primary']['kind']=='proposal'
    assert all(t['status']=='proposed' and t['owner'] is None and t['due_at'] is None for t in view['tasks'])
    assert not store.path('tasks.json').exists()  # Reading does not silently adopt a plan.


def test_deadlines_and_closed_work_are_handled_without_repeating_tasks(study):
    store, _, _ = study
    finish(study)
    payload={'expected_revision':0,'tasks':[task()]}
    track_payload(store,payload)
    before=next_actions(store,card(),{'required':True,'ready':True},'2026-11-10T14:59:59Z')
    after=next_actions(store,card(),{'required':True,'ready':True},'2026-11-10T15:00:01Z')
    assert not before['tasks'][0]['overdue'] and after['tasks'][0]['overdue']
    track_payload(store,{'expected_revision':1,'tasks':[task(status='done',resolution='TEST: observations recorded.')]})
    final=next_actions(store,card(),{'required':True,'ready':True},'2027-01-01T00:00:00Z')
    assert final['primary']['kind']=='monitor' and not final['tasks'][0]['overdue']
    track_payload(store,payload)  # Retry of old command cannot reopen the task.
    assert now_view(str(store.root))['tasks'][0]['status']=='done'


def test_tracking_conflict_and_report_freshness(study):
    store, _, _ = study
    finish(study);publish_report(str(store.root))
    original_quality=store.read('reports/quality-review.json')
    track_payload(store,{'expected_revision':0,'tasks':[task(blocked_reason='TEST: participant access not agreed.')]})
    assert now_view(str(store.root))['primary']['action'].startswith('Устранить')
    with pytest.raises(AgentError):track_payload(store,{'expected_revision':0,'tasks':[task(owner='Someone else')]})
    assert store.read('reports/quality-review.json')==original_quality
    result=export_project(str(store.root),False)
    with zipfile.ZipFile(result['path']) as z:
        assert b'participant access not agreed' in z.read('daily/reports/decision.html')


def test_new_answer_requires_an_explanation_and_preserves_the_old_version(study):
    store, _, _ = study
    finish(study)
    track_payload(store,{'expected_revision':0,'tasks':[task()]})
    old=store.read('versions/v0001.json')
    updated=card();updated['recommendation']='Verify the job and format before deciding whether to change discovery.'
    payload={'stage':'complete','decision_brief':updated,'quality_review':review(),'next_action':'Read the changed conclusion.'}
    with pytest.raises(AgentError):write(store,payload,'bad-change.json')
    assert len(list_versions(store))==1
    payload['change_reason']='TEST: the new question prioritises format fit over discovery of the existing action.'
    write(store,payload,'changed.json')
    assert store.read('versions/v0001.json')==old
    delta=compare(store)
    assert delta['recommendation']['changed'] and delta['explanation']==payload['change_reason']
    assert delta['recommendation']['before']==card()['recommendation']
    assert now_view(str(store.root))['primary']['kind']=='review_tasks'
    assert now_view(str(store.root))['tasks'][0]['needs_recheck']


@pytest.mark.parametrize('path',['versions/v0001.json','follow-up/rev-000001.json'])
def test_corrupted_history_is_not_silently_accepted(study,path):
    store, _, _ = study
    finish(study);track_payload(store,{'expected_revision':0,'tasks':[task()]})
    atomic(store.path(path),b'{}')
    assert not validation(store)['valid']
    with pytest.raises(AgentError):session(str(store.root))


def test_portable_copy_preserves_versions_and_followup(study,tmp_path,monkeypatch):
    store, _, _ = study
    finish(study);track_payload(store,{'expected_revision':0,'tasks':[task()]})
    result=export_project(str(store.root),True)
    dest=tmp_path/'other-workspace';dest.mkdir()
    with zipfile.ZipFile(result['path']) as z:z.extractall(dest)
    monkeypatch.chdir(dest)
    restored=Store(dest/'daily')
    assert validation(restored)['valid']
    assert session('daily')['now']['tasks'][0]['owner']=='TEST product lead'
    assert list_versions(restored)==list_versions(store)


def test_existing_demo_history_is_read_only(tmp_path,monkeypatch):
    with zipfile.ZipFile(REPO/'demo/confirmed-run.zip') as z:z.extractall(tmp_path)
    root=next(tmp_path.rglob('state.json')).parent
    # Locate the project root rather than an archived checkpoint.
    while not (root/'sources.jsonl').exists():root=root.parent
    monkeypatch.chdir(tmp_path)
    store=Store(root)
    before={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
    result=session(str(root))
    assert result['now']['primary']['kind']=='proposal'
    assert list_versions(store) and not store.path('versions.jsonl').exists()
    after={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
    assert before==after


def test_changed_grounds_are_shown_and_report_text_is_escaped(study):
    store, claim, _ = study
    finish(study)
    replacement={**claim,'id':'fact-corrected','supersedes':'fact-export','text':'TEST ONLY: a revised capability with a new limitation.'}
    revised=card();revised['reasons'][0]['claim_ids']=['fact-corrected']
    assessment=review();assessment['checks'][0]['claim_ids']=['fact-corrected']
    write(store,{'stage':'complete','claims':[replacement],'decision_brief':revised,'quality_review':assessment,
                 'change_reason':'TEST: the documented capability was corrected, so earlier grounds no longer apply.',
                 'next_action':'Read the corrected grounds.'},'correction.json')
    delta=compare(store)
    assert [c['id'] for c in delta['grounds']['removed']]==['fact-export']
    assert [c['id'] for c in delta['grounds']['added']]==['fact-corrected']
    assert not delta['recommendation']['changed']
    track_payload(store,{'expected_revision':0,'tasks':[task(action='TEST </script><script>alert(1)</script>')]})
    publish_report(str(store.root));html=store.read('reports/decision.html').decode()
    assert '<script>alert(1)</script>' not in html and '&lt;script&gt;alert(1)&lt;/script&gt;' in html


def test_synthetic_commitments_cannot_be_inserted_into_real_projects(study):
    store, _, _ = study
    request=store.json('request.json');request['synthetic']=False
    atomic(store.path('request.json'),encode(request))
    with pytest.raises(AgentError):track_payload(store,{'expected_revision':0,'tasks':[task()]})
