"""Independent tests: PASS requires the safer behavior. No paid or live calls."""
import sys,json
from pathlib import Path
import os
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
import pytest
from test_second_audit import item,v,ledger,VCTX
from aitx.rules import apply_verdicts,apply_section_rules,compose_from_verified_claims
from aitx.evidence import select_passages
from aitx.ledger import EvidenceRecord,EvidenceLedger
from aitx.schemas import Section,RevisionPlan
from aitx.render import render_item
from aitx.agents.verifier import ground_verdict
from aitx.config import get_settings
from aitx.http import HttpClient
from aitx.llm.mock import MockLLM
from aitx.pipeline import Pipeline
from aitx.submission import assemble_submission,SubmissionBlocked
from fake_http import transport
from mock_handlers import HANDLERS,hypothesized,established,verify

@pytest.fixture(scope='module')
def rejected_report():
    def hypo(ctx):
        o=hypothesized(ctx);o.items[0].bespoke_verdict='AMENABLE';return o
    def est(ctx):
        o=established(ctx);o.items[-1].section=Section.OFF_LABEL
        o.items[-1].human_precedent='Drug Q eliminated all symptoms in this patient [PMID:11111111].'
        return o
    def ver(ctx):
        o=verify(ctx)
        for d in o.verdicts:
            if d.item_id in ('aso-allele','tgt-q') and d.claim_index==-1:
                d.verdict='unsupported';d.rationale='The clinical assessment/precedent is not supported.'
        return o
    h={**HANDLERS,'C_hypothesized':hypo,'B_established':est,'D_verify':ver,'F_revise':lambda ctx:RevisionPlan(revisions=[])}
    s=get_settings(deployment_mode=True,genereviews_dir=ROOT/'tests/fixtures/genereviews',max_revisions=2)
    http=HttpClient(s,transport=transport())
    try:
        r=Pipeline(MockLLM(h),s,http=http,parallel=False).run(json.loads((ROOT/'tests/fixtures/test_case.json').read_text()))
    finally:
        http.close()
    return r

def test_rejected_bespoke_assessment_cannot_survive_salvage(rejected_report):
    r=rejected_report
    assert not any(i['bespoke_verdict']=='AMENABLE' for i in r.audit['final_items']), 'Rejected ASO verdict still rendered after salvage'

def test_rejected_human_precedent_cannot_survive_salvage(rejected_report):
    assert 'eliminated all symptoms' not in rejected_report.output.report_markdown

def test_submission_gate_blocks_rejected_clinical_fields(rejected_report):
    # Only the mock-specific prohibition is waived; all substantive gates remain active.
    decisions=[v for r in rejected_report.audit['stages']['D_verification_rounds'] for v in r['verdicts']]
    assert any(v['item_id']=='aso-allele' and v['claim_index']==-1 and v['verdict']=='unsupported' for v in decisions)
    # Safe reconstruction/removal may now produce a clean test report. Inject a
    # rejected auxiliary assertion into the final snapshot: substantive gates
    # must reject it even when only the mock prohibition is waived.
    from copy import deepcopy
    tampered = deepcopy(rejected_report)
    tampered.audit['final_items'][0]['human_precedent'] = 'Drug Q eliminated all symptoms in this patient.'
    with pytest.raises(SubmissionBlocked):
        assemble_submission([tampered],['TEST-0001'],allow_mock=True)

def test_mechanism_gate_requires_function_not_merely_variant_identity():
    L=ledger()
    it=item('x',Section.REPURPOSING,[('The patient carries p.Asp34Asn.',['PMID:1'],'molecular_fact')],
            statement='Consider a GENEX potentiator [PMID:1].',title='Channel potentiator')
    rr=apply_verdicts([it],[v('x',0,app='same_variant'),v('x',-1)],ledger=L)
    assert any(e.rule=='mechanism_direction_unestablished' for e in rr.events)

def test_partial_only_candidate_survives_to_revision():
    it=item('x',Section.REPURPOSING,[('Compound R completely rescued cells.',['PMID:2'],'treatment_effect_preclinical')])
    rr=apply_verdicts([it],[v('x',0,'partially_supported'),v('x',-1,'partially_supported')],ledger=ledger())
    assert rr.items, 'Partially true candidate dropped before rewrite'

def test_long_negative_passage_not_silently_omitted():
    harm='However, survival was lower in the treated animals. '+('Detailed safety observations and measurements. '*90)
    rec=EvidenceRecord('PMID:9','pubmed','Synthetic','u',content='Q improved cells.',metadata={'passages':[('Abstract','Q improved cells.'),('Results',harm)]})
    text,info=select_passages(rec,'Q improved cells',max_chars=1200)
    assert 'survival was lower' in text,info

def test_gene_named_only_in_exclusions_is_not_required_gene():
    L=EvidenceLedger();L.add(EvidenceRecord('NCT00000999','clinicaltrials','Ataxia trial','u',
        content='Inclusion: all genetically confirmed ataxias. Exclusion: OTHER1-related disease.',
        metadata={'overall_status':'RECRUITING','min_age_years':1,'max_age_years':90,'sex':'ALL'}))
    it=item('x',Section.TRIALS,[('Trial recruiting.',['NCT00000999'],'trial_status_or_eligibility')],nct_id='NCT00000999')
    assert apply_section_rules([it],L,7,'male',['GENEX']).items

def test_factual_not_checkable_rejected():
    it=item('x',Section.REPURPOSING,[('ASO cured humans.',['PMID:1'],'treatment_effect_human')])
    assert not apply_verdicts([it],[v('x',0,'not_checkable'),v('x',-1,'not_checkable')],ledger=ledger()).items

def test_valid_preclinical_retained():
    it=item('x',Section.REPURPOSING,[('Compound R increased channel activity in cells.',['PMID:2'],'treatment_effect_preclinical')])
    assert apply_verdicts([it],[v('x',0,ev='preclinical'),v('x',-1)],ledger=ledger()).items

def test_fabricated_quote_rejected():
    cov={'quote_failures':0,'applicability_downgrades':0,'uncited_supported_to_not_checkable':0}
    d=ground_verdict(v('x',0,quote='ASO cured every patient.',key='PMID:1'),{'citations':['PMID:1']},ledger(),VCTX,cov)
    assert d.verdict=='insufficient_source_text'
