"""Offline adversarial closure tests. Fictional cases, fake HTTP, no real model clients."""
from __future__ import annotations

import copy
import json
import re

import pytest
from mock_handlers import HANDLERS, established, hypothesized, verify
from test_second_audit import VCTX, item, ledger, v

from aitx.agents import verifier
from aitx.evidence import passages, select_passages
from aitx.factory import CONFIGS
from aitx.ledger import EvidenceLedger, EvidenceRecord
from aitx.llm.base import BudgetExceeded
from aitx.llm.mock import MockLLM
from aitx.pipeline import Pipeline
from aitx.render import render_item, render_report
from aitx.retrieval.clinicaltrials import _flatten, eligibility_blocks
from aitx.rules import apply_section_rules, apply_verdicts, compose_from_verified_claims
from aitx.schemas import Claim, ItemRevision, RevisionPlan, Section, VerificationBatch
from aitx.submission import SubmissionBlocked, assemble_submission
from aitx.verification_contract import item_digest, item_text


def payload(ctx):
    return json.loads(re.search(r"Claims to verify \(JSON\):\n(\[.*?\])\n\nSource texts:", ctx['system'], re.S).group(1))


def run_full(settings, fake_http, test_case, handlers=None):
    opts = CONFIGS['B_full']
    llm = MockLLM({**HANDLERS, **(handlers or {})})
    result = Pipeline(llm, settings, http=fake_http, parallel=False,
                      mode=opts['mode'], verify_claims=opts['verify_claims']).run(test_case)
    return result, llm


def adversarial_handlers():
    def aso(ctx):
        out = hypothesized(ctx)
        out.items[0].bespoke_verdict = 'AMENABLE'
        out.items[0].conditional_action = 'REJECTED_ASSERTION: treatment cures this patient.'
        return out

    def off_label(ctx):
        out = established(ctx)
        out.items[-1].section = Section.OFF_LABEL
        out.items[-1].human_precedent = 'REJECTED_ASSERTION: Drug Q eliminated all symptoms in this patient [PMID:11111111].'
        return out

    def reject(ctx):
        out = verify(ctx)
        cs = {(c['item_id'], c['claim_index']): c for c in payload(ctx)}
        for decision in out.verdicts:
            text = cs[(decision.item_id, decision.claim_index)]['claim']
            if 'REJECTED_ASSERTION' in text or '"bespoke_verdict": "AMENABLE"' in text:
                decision.verdict = 'unsupported'
                decision.rationale = 'Unsupported feasibility, human treatment precedent or patient benefit.'
        return out

    return {'C_hypothesized': aso, 'B_established': off_label, 'D_verify': reject,
            'F_revise': lambda ctx: RevisionPlan(revisions=[])}


def test_complete_b_full_rejected_fields_cannot_reach_final_artifact(settings, fake_http, test_case):
    result, llm = run_full(settings, fake_http, test_case, adversarial_handlers())
    md = result.output.report_markdown
    rounds = result.audit['stages']['D_verification_rounds']
    initial = rounds[0]['verdicts']
    assert {d['item_id'] for d in initial if d['claim_index'] == -1 and d['verdict'] == 'unsupported'} >= {'aso-allele', 'tgt-q'}
    assert 'REJECTED_ASSERTION' not in md and 'eliminated all symptoms' not in md
    assert '**Verdict:** AMENABLE' not in md
    final = {i['item_id']: i for i in result.audit['final_items']}
    assert {'sup-seizures', 'tgt-q', 'off-r', 'aso-allele'} <= final.keys()
    assert final['aso-allele']['bespoke_verdict'] == 'UNCERTAIN'
    assert '50%' in final['tgt-q']['human_precedent']  # supported human information is reconstructed
    assert 'Conditional experimental research hypothesis' in md
    assert 'wild-type/mutant' in md
    assert assemble_submission([result], ['TEST-0001'], allow_mock=True)
    with pytest.raises(SubmissionBlocked):
        assemble_submission([result], ['TEST-0001'])  # real submission still rejects mock
    assert all(c.served_model == 'mock' and c.cost_usd == 0 for c in llm.cost.calls)


@pytest.mark.parametrize('field', ['title', 'statement', 'conditional_action', 'human_precedent', 'missing_information',
                                  'criteria_met', 'criteria_unassessable', 'potential_exclusions', 'mechanism_qualification'])
def test_every_claim_bearing_field_is_in_exact_verifier_payload_and_digest(field):
    it = item('x', Section.REPURPOSING, [('Supported cell result.', ['PMID:2'], 'treatment_effect_preclinical')])
    original = item_digest(it)
    setattr(it, field, ['REJECTED_ASSERTION'] if isinstance(getattr(it, field), list) else 'REJECTED_ASSERTION')
    assert 'REJECTED_ASSERTION' in verifier._statement_text(it)
    assert item_digest(it) != original


def test_salvage_constructs_fresh_safe_auxiliary_fields():
    it = item('x', Section.BESPOKE, [('A supported molecular fact.', ['PMID:1'], 'molecular_fact')],
              bespoke_verdict='AMENABLE', human_precedent='REJECTED_ASSERTION', criteria_met=['REJECTED_ASSERTION'],
              conditional_action='REJECTED_ASSERTION')
    it.missing_information = ['REJECTED_ASSERTION']
    rebuilt = compose_from_verified_claims(it, [v('x', 0), v('x', -1, 'unsupported')])
    assert rebuilt and rebuilt.bespoke_verdict == 'UNCERTAIN'
    assert 'REJECTED_ASSERTION' not in item_text(rebuilt)


def test_renderer_refuses_stale_final_contract():
    from aitx.schemas import Synthesis
    it = item('x', Section.REPURPOSING, [('Supported result.', ['PMID:2'], 'treatment_effect_preclinical')])
    contracts = {'x': {'digest': item_digest(it), 'status': 'supported'}}
    it.human_precedent = 'REJECTED_ASSERTION'
    s = Synthesis(summary_lines=[], missing_information=[], outcome_measures=[], calibration_notes=[], duplicate_item_ids_to_drop=[])
    with pytest.raises(ValueError, match='Unverified rendered item'):
        render_report('TEST', [it], s, ledger(), verification_contracts=contracts)


def test_synthesis_cannot_restore_rejected_feasibility_or_patient_benefit(settings, fake_http, test_case):
    from mock_handlers import synthesize
    def poisoned(ctx):
        out = synthesize(ctx)
        out.summary_lines[0].text = 'REJECTED_ASSERTION: Seizures eliminated in this patient [GENEREVIEWS:NBK9999#Treatment_of_Manifestations].'
        out.outcome_measures[0].rank_rationale = 'REJECTED_ASSERTION: ASO AMENABLE and clinically effective.'
        out.missing_information = ['REJECTED_ASSERTION: no clinical uncertainty remains.']
        return out
    result, _ = run_full(settings, fake_http, test_case, {**adversarial_handlers(), 'F_synthesize': poisoned})
    assert 'REJECTED_ASSERTION' not in result.output.report_markdown
    assert 'clinically effective' not in result.output.report_markdown
    assert result.audit['stages']['F_synthesis_verification']['events']


def test_auxiliary_citations_reach_the_full_item_verifier():
    from aitx.rules import item_citations
    it = item('x', Section.REPURPOSING, [('One fact.', ['PMID:1'], 'molecular_fact')])
    it.criteria_unassessable = ['An additional assertion [PMID:2].']
    assert 'PMID:2' in item_citations(it)


@pytest.mark.parametrize('mutation', ['markdown', 'auxiliary', 'verdict', 'missing_contract'])
def test_submission_rejects_post_verification_tampering(settings, fake_http, test_case, mutation):
    result, _ = run_full(settings, fake_http, test_case)
    assert assemble_submission([result], ['TEST-0001'], allow_mock=True)
    if mutation == 'markdown':
        result.output.report_markdown += '\nDrug Q eliminated all symptoms.\n'
    elif mutation == 'auxiliary':
        result.audit['final_items'][0]['human_precedent'] = 'Unsupported human efficacy.'
    elif mutation == 'verdict':
        result.audit['stages']['final_item_verification']['verdicts'][0]['verdict'] = 'unsupported'
    else:
        result.audit['item_contracts'] = {}
    with pytest.raises(SubmissionBlocked):
        assemble_submission([result], ['TEST-0001'], allow_mock=True)


@pytest.mark.parametrize('failure', ['missing', 'exception', 'budget'])
def test_verifier_failure_closes_all_rendering_paths(settings, fake_http, test_case, failure):
    def broken(ctx):
        if failure == 'exception':
            raise RuntimeError('Synthetic verifier transport failure')
        if failure == 'budget':
            raise BudgetExceeded('Synthetic budget exhausted')
        return VerificationBatch(verdicts=[])
    result, llm = run_full(settings, fake_http, test_case, {'D_verify': broken})
    assert result.audit['final_items'] == []
    assert result.blockers
    assert len([s for s in llm.seen if s['stage'].startswith('F_revise')]) <= settings.max_revisions
    with pytest.raises(SubmissionBlocked):
        assemble_submission([result], ['TEST-0001'], allow_mock=True)


@pytest.mark.parametrize('revision', ['empty', 'unchanged'])
def test_exhausted_revision_budget_never_promotes_partial_claims(settings, fake_http, test_case, revision):
    def partial(ctx):
        out = verify(ctx)
        for d in out.verdicts:
            if d.item_id == 'off-r':
                d.verdict = 'partially_supported'
        return out
    plan = RevisionPlan(revisions=[] if revision == 'empty' else [ItemRevision(item_id='off-r', action='revise',
                        revised_statement='Compound R rescued Genex knockout mice [PMID:22222222].', reason='unchanged')])
    result, llm = run_full(settings, fake_http, test_case, {'D_verify': partial, 'F_revise': lambda ctx: plan})
    assert 'off-r' not in {i['item_id'] for i in result.audit['final_items']}
    assert len([s for s in llm.seen if s['stage'].startswith('F_revise')]) == settings.max_revisions


def test_partial_candidate_can_be_narrowed_and_freshly_verified(settings, fake_http, test_case):
    narrowed = 'Compound R restored release in mice.'
    def research(ctx):
        from mock_handlers import emerging
        out = emerging(ctx)
        out.items[-1].statement = 'Compound R completely cured the disease [PMID:22222222].'
        out.items[-1].claims[0].text = 'Compound R completely cured the disease.'
        return out
    def check(ctx):
        out = verify(ctx)
        cs = {(c['item_id'], c['claim_index']): c for c in payload(ctx)}
        for d in out.verdicts:
            if d.item_id == 'off-r' and 'completely cured' in cs[(d.item_id, d.claim_index)]['claim']:
                d.verdict = 'partially_supported'
                d.suggested_revision = narrowed
        return out
    def revise(ctx):
        return RevisionPlan(revisions=[ItemRevision(item_id='off-r', action='revise', reason='narrowed to measured animal result',
             revised_statement=narrowed+' [PMID:22222222]', revised_claims=[Claim(text=narrowed, citations=['PMID:22222222'], kind='treatment_effect_preclinical')])])
    result, _ = run_full(settings, fake_http, test_case, {'B_emerging': research, 'D_verify': check, 'F_revise': revise})
    final = {i['item_id']: i for i in result.audit['final_items']}
    assert final['off-r']['claims'][0]['text'] == narrowed
    assert 'completely cured' not in result.output.report_markdown
    rounds = result.audit['stages']['D_verification_rounds']
    assert any(r['tag'].startswith('revision') and any(v['item_id']=='off-r' and v['claim_index']==0 and v['verdict']=='supported'
               for v in r['verdicts']) for r in rounds)


@pytest.mark.parametrize('evidence,direction,app,expected', [
    ('identity_or_pathogenicity', 'unknown', 'same_variant', False),
    ('not_assessed', 'unknown', 'same_variant', False),
    ('variant_function', 'gain_of_function', 'same_variant', True),
    ('variant_function', 'loss_of_function', 'same_variant', True),
    ('variant_function', 'mixed_or_conflicting', 'same_variant', False),
    ('variant_function', 'gain_of_function', 'same_gene_other_variant_or_mechanism', False),
    ('variant_function', 'loss_of_function', 'same_gene_same_mechanism', False),
])
def test_identity_pathogenicity_and_function_are_distinct(evidence, direction, app, expected):
    it = item('x', Section.REPURPOSING, [('Experimental result for the variant.', ['PMID:1'], 'molecular_fact')],
              title='Channel potentiator', statement='Experimental testing of a channel potentiator [PMID:1].')
    decision = v('x', 0, app=app).model_copy(update={'functional_evidence': evidence, 'functional_direction': direction,
                    'functional_context': 'heterozygous_wild_type_mutant'})
    res = apply_verdicts([it], [decision, v('x', -1)], ledger=ledger())
    flagged = any(e.rule=='mechanism_direction_unestablished' for e in res.events)
    assert flagged != expected
    if not expected:
        assert 'Conditional experimental research hypothesis only' in render_item(res.items[0], ledger(), {})


def test_contradictory_exact_variant_assays_remain_conditional():
    it = item('x', Section.REPURPOSING, [('Assay A.', ['PMID:1'], 'molecular_fact'), ('Assay B.', ['PMID:2'], 'molecular_fact')], title='Channel inhibitor')
    ds = [v('x', n, app='same_variant').model_copy(update={'functional_evidence':'variant_function',
          'functional_direction': direction, 'functional_context':'patient_derived'}) for n, direction in enumerate(['gain_of_function','loss_of_function'])]
    res = apply_verdicts([it], ds+[v('x', -1)], ledger=ledger())
    assert 'unresolved or conflicting' in res.items[0].mechanism_qualification


@pytest.mark.parametrize('context,qualifies', [('mutant_only',True),('unknown',True),('heterozygous_wild_type_mutant',False),('patient_derived',False)])
def test_heterozygous_context_is_not_inferred_from_mutant_only(context, qualifies):
    it = item('x', Section.REPURPOSING, [('A functional result.', ['PMID:1'], 'molecular_fact')], title='Potentiator')
    d = v('x',0,app='same_variant').model_copy(update={'functional_evidence':'variant_function',
                'functional_direction':'loss_of_function','functional_context':context})
    res=apply_verdicts([it],[d,v('x',-1)],ledger=ledger())
    assert ('allelic context' in res.items[0].mechanism_qualification) == qualifies


def test_functional_quote_must_identify_the_exact_variant():
    cov={'quote_failures':0,'applicability_downgrades':0,'uncited_supported_to_not_checkable':0}
    d=v('x',0,app='same_variant',key='PMID:2',quote='Compound R potentiated GENEX channel activity in HEK293 cells.').model_copy(
        update={'functional_evidence':'variant_function','functional_direction':'gain_of_function'})
    got=verifier.ground_verdict(d,{'kind':'molecular_fact','citations':['PMID:1','PMID:2']},ledger(),VCTX,cov)
    assert got.functional_evidence=='not_assessed'


def test_exact_variant_experimental_quote_can_establish_function():
    L=ledger()
    quote='The p.Asp34Asn variant reduced calcium release in a heterozygous wild-type/mutant co-expression assay.'
    L.add(EvidenceRecord('PMID:4','pubmed','Functional experiment','u4',content=quote))
    cov={'quote_failures':0,'applicability_downgrades':0,'uncited_supported_to_not_checkable':0}
    decision=v('x',0,app='same_variant',key='PMID:4',quote=quote).model_copy(update={
        'functional_evidence':'variant_function','functional_direction':'loss_of_function',
        'functional_context':'heterozygous_wild_type_mutant'})
    got=verifier.ground_verdict(decision,{'kind':'molecular_fact','citations':['PMID:4']},L,VCTX,cov)
    assert got.functional_evidence=='variant_function' and got.functional_direction=='loss_of_function'


def test_duplicate_functional_decisions_cannot_select_the_stronger_direction():
    a=v('x',0,app='same_variant').model_copy(update={'functional_evidence':'variant_function','functional_direction':'gain_of_function'})
    b=a.model_copy(update={'functional_direction':'loss_of_function'})
    merged=verifier._conservative_fields(a,b)
    assert merged.functional_direction=='mixed_or_conflicting'


def test_registry_flatten_preserves_raw_criteria_and_scope():
    raw='Inclusion: pathogenic variant in GENEX. Exclusion: OTHER1-related disease.'
    flat=_flatten({'protocolSection':{'eligibilityModule':{'eligibilityCriteria':raw}}})
    assert flat['eligibility_criteria']==raw
    assert [(b['scope'],b['arm']) for b in flat['eligibility_blocks']]==[('inclusion',''),('exclusion','')]


def test_general_requirements_are_separate_from_arm_specific_requirements():
    blocks=eligibility_blocks('Arm A:\nInclusion: OTHER1-related disease.\nGeneral study requirements: informed consent.')
    assert blocks[0]['arm']=='Arm A' and blocks[0]['scope']=='inclusion'
    assert blocks[1]['arm']=='' and blocks[1]['scope']=='general'


@pytest.mark.parametrize('criteria,keep,met', [
    ('Inclusion: pathogenic variant in OTHER1.',False,False),
    ('Inclusion: pathogenic variant in GENEX.',True,True),
    ('Inclusion: all genetic ataxias. Exclusion: OTHER1-related disease.',True,False),
    ('Inclusion: pathogenic variant in GENEX. Exclusion: OTHER1-related disease.',True,True),
    ('Inclusion Criteria:\n* pathogenic variant in GENEX\nExclusion Criteria:\n* OTHER1-related disease',True,True),
    ('Arm A:\nInclusion: pathogenic variant in OTHER1.\nArm B:\nInclusion: pathogenic variant in GENEX.',True,False),
    ('Inclusion:\nArm A: pathogenic variant in OTHER1.\nArm B: pathogenic variant in GENEX.',True,False),
    ('Inclusion: Arm A: pathogenic variant in GENEX. Arm B: pathogenic variant in OTHER1.',True,False),
    ('Inclusion: Arm A: pathogenic variant in OTHER1. Arm B: all genetically confirmed ataxias.',True,False),
    ('OTHER1-related disease mentioned without criteria scope.',True,False),
    ('',True,False),
    ('Inclusion: GENEX gene testing may be available.',True,False),
    ('Inclusion: no pathogenic variant in OTHER1.',True,False),
    ('Inclusion: OTHER1-related disease or any genetic ataxia.',True,False),
])
def test_trial_scope_never_transfers_exclusion_or_arm_genes(criteria,keep,met):
    L=EvidenceLedger()
    L.add(EvidenceRecord('NCT00000999','clinicaltrials','OTHER1-related disease study','u',content=criteria,
          metadata={'overall_status':'RECRUITING','min_age_years':2,'max_age_years':18,'sex':'ALL'}))
    it=item('x',Section.TRIALS,[('Recruiting.',['NCT00000999'],'trial_status_or_eligibility')],nct_id='NCT00000999')
    res=apply_section_rules([it],L,7,'female',['GENEX'])
    assert bool(res.items)==keep
    if keep:
        assert any('Genetic diagnosis' in c for c in res.items[0].criteria_met)==met
        if not criteria or 'Arm ' in criteria or 'scope' in criteria:
            assert any('uncertain' in c for c in res.items[0].criteria_unassessable)


@pytest.mark.parametrize('label', ['Arm', 'Cohort', 'Group', 'Part'])
def test_inline_eligibility_blocks_keep_each_cohort_scope(label):
    raw = f'Inclusion: {label} A: OTHER1-related disease. {label} B: all genetic ataxias. Exclusion: active infection.'
    blocks = eligibility_blocks(raw)
    assert [(b['scope'], b['arm']) for b in blocks] == [
        ('inclusion', f'{label} A'), ('inclusion', f'{label} B'), ('exclusion', f'{label} B')]
    assert blocks[0]['text'] == 'OTHER1-related disease.'
    assert blocks[1]['text'] == 'all genetic ataxias.'


@pytest.mark.parametrize('metadata,age,sex,keep', [
    ({'overall_status':'COMPLETED'},7,'female',False),
    ({'min_age_years':18},7,'female',False),
    ({'max_age_years':5},7,'female',False),
    ({'sex':'MALE'},7,'female',False),
    ({'overall_status':'NOT_YET_RECRUITING'},7,'female',True),
    ({},None,'unknown',True),
])
def test_trial_demographic_and_recruitment_safeguards(metadata,age,sex,keep):
    L=EvidenceLedger(); L.add(EvidenceRecord('NCT00000999','clinicaltrials','Trial','u',content='Inclusion: all genetic ataxias.',
      metadata={'overall_status':'RECRUITING','min_age_years':2,'max_age_years':18,'sex':'ALL',**metadata}))
    it=item('x',Section.TRIALS,[('Recruiting.',['NCT00000999'],'trial_status_or_eligibility')],nct_id='NCT00000999')
    assert bool(apply_section_rules([it],L,age,sex,['GENEX']).items)==keep


@pytest.mark.parametrize('budget',[1200,6000])
def test_long_negative_results_preserve_group_context_and_original_text(budget):
    negative='Wild-type controls received the same construct. However, survival was lower in these treated controls. '+('Detailed safety observations and measurements. '*90)
    rec=EvidenceRecord('PMID:9','pubmed','Synthetic experiment','u',content='Q improved mutant cells.',
        metadata={'passages':[('Results, experiment 2',negative)]})
    text,info=select_passages(rec,'Q improved mutant cells',max_chars=budget)
    assert len(text)<=budget
    assert 'survival was lower' in text and 'Wild-type controls received the same construct.' in text
    assert 'Results, experiment 2' in text
    for line in text.splitlines():
        fragment=line.split('] ',1)[1]
        assert fragment in negative or fragment in rec.content
    assert info['contrast_shown']>=1


def test_unsplittable_harm_records_context_limit_instead_of_claiming_complete_verification():
    rec=EvidenceRecord('PMID:9','pubmed','Synthetic','u',content='Positive cell result.',
        metadata={'passages':[('Results','However, '+('long measurement '*1000)+' mortality increased.')]})
    text,info=select_passages(rec,'positive result',max_chars=80)
    assert len(text)<=80 and info['important_evidence_omitted']>0


def test_omitted_limiting_results_cannot_receive_supported_verification():
    L=EvidenceLedger(); L.add(EvidenceRecord('PMID:9','pubmed','Synthetic','u',content='Q improved mutant cells.',
        metadata={'passages':[('Results',('However, survival was lower in controls. '+('detailed context '*60)+'. ')*15)]}))
    claims=[{'item_id':'x','claim_index':0,'section':'drug_repurposing','intervention':'Q',
             'claim':'Q improved mutant cells.','kind':'treatment_effect_preclinical','citations':['PMID:9']}]
    llm=MockLLM({'D_verify':verify})
    out=verifier.verify_payload(claims,L,llm,'medium','D_verify',VCTX)
    assert 'IMPORTANT_EVIDENCE_OMITTED' in llm.seen[0]['system']
    assert out.verdicts[0].verdict=='insufficient_source_text'
