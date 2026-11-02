"""Real SDK-shaped offline responses; no model API calls or credentials."""
import json
from datetime import date
from types import SimpleNamespace as NS
from dataclasses import replace

import pytest
from google.genai import types
from pydantic import BaseModel

from aitx.llm.gemini_client import GeminiLLM, argon_prices
from aitx.llm.base import CostTracker, StructuredOutputError, BudgetExceeded


class Answer(BaseModel):
    result: str


def response(text='{"result":"ok"}', parts=None, **kw):
    return types.GenerateContentResponse(model_version=kw.pop('model', 'gemini-4-argon'),
        candidates=[types.Candidate(content=types.Content(role='model', parts=parts or [types.Part(text=text)]),
                                    finish_reason=kw.pop('finish', 'STOP'))],
        usage_metadata=kw.pop('usage', types.GenerateContentResponseUsageMetadata(
            prompt_token_count=1000, candidates_token_count=100, thoughts_token_count=200, cached_content_token_count=400)), **kw)


class Client:
    def __init__(self, script):
        self.script=list(script);self.calls=[];self.models=self
    def generate_content(self, **kw):
        self.calls.append(kw)
        result=self.script.pop(0)
        if isinstance(result, Exception): raise result
        return result


def invoke(settings, script):
    client=Client(script);cost=CostTracker(12)
    llm=GeminiLLM(settings,cost,client=client)
    return llm,client,cost


def test_structured_billing_and_no_hidden_calls(settings):
    llm,client,cost=invoke(settings,[response()])
    assert llm.structured(stage='x',system='s',user='u',output_model=Answer).result=='ok'
    cfg=client.calls[0]['config']
    assert cfg.store is False and cfg.automatic_continuation is False
    assert cfg.service_tier==types.ServiceTier.STANDARD
    assert cfg.automatic_function_calling.disable
    assert cfg.temperature is None and cfg.cached_content is None
    pin,pout,cache=argon_prices()
    assert cost.calls[0].output_tokens==300
    assert cost.total_usd==round((600*pin+400*cache+300*pout)/1e6,4)
    assert cost.reserved_usd==0


@pytest.mark.parametrize('bad', [response(finish='MAX_TOKENS'),response(finish='CONTINUATION'),
    response(finish='SAFETY'),response(model='gemini-3.8-flash'),response(text='PATIENT_SECRET'),
    response(parts=[types.Part(text='{"result":"ok"}'),types.Part(function_call=types.FunctionCall(name='unrequested',args={}))])])
def test_incomplete_or_wrong_provider_output_fails_closed(settings,bad):
    llm,client,cost=invoke(settings,[bad])
    with pytest.raises(StructuredOutputError) as e:
        llm.structured(stage='x',system='s',user='u',output_model=Answer)
    assert 'PATIENT_SECRET' not in str(e.value)
    assert len(client.calls)==1 and cost.reserved_usd==0


@pytest.mark.parametrize('bad', [response(usage=None), None])
def test_absent_usage_metadata_retries_then_fails_closed(settings, monkeypatch, bad):
    # Third real paid run: one tool-heavy turn returned a response whose usage metadata could not be
    # verified and the stage died without a retry. Missing metadata must retry (like a provider error)
    # and only fail closed after the attempt budget, never accepting unverifiable content.
    monkeypatch.setattr('aitx.llm.gemini_client.time.sleep', lambda _: None)
    llm,client,cost=invoke(settings,[bad]*3)
    with pytest.raises(StructuredOutputError) as e:
        llm.structured(stage='x',system='s',user='u',output_model=Answer)
    assert len(client.calls)==3
    assert 'after retries' in str(e.value)
    assert [c.stop_reason for c in cost.calls]==['invalid_usage']*3
    assert all(c.uncertain_billing for c in cost.calls)
    assert cost.reserved_usd==0


def test_invalid_usage_recovers_when_next_attempt_has_valid_metadata(settings, monkeypatch):
    # The retry path must reuse the ordinary accounting: one uncertain invalid-usage record followed by
    # a normally billed successful call, with no leftover reservations.
    monkeypatch.setattr('aitx.llm.gemini_client.time.sleep', lambda _: None)
    um = types.GenerateContentResponseUsageMetadata(prompt_token_count=10, thoughts_token_count=5)
    llm,client,cost=invoke(settings,[response(usage=um),response()])
    assert llm.structured(stage='x',system='s',user='u',output_model=Answer).result=='ok'
    assert len(client.calls)==2
    assert cost.calls[0].stop_reason=='invalid_usage' and cost.calls[0].uncertain_billing
    assert cost.calls[1].stop_reason=='' and not cost.calls[1].uncertain_billing
    assert cost.reserved_usd==0 and cost.total_usd>0


def test_budget_blocks_before_request(settings):
    llm,client,cost=invoke(settings,[]);cost.budget_usd=0.01
    with pytest.raises(BudgetExceeded): llm.structured(stage='x',system='s',user='u',output_model=Answer)
    assert not client.calls


class ProviderError(Exception):
    def __init__(self,code): self.code=code;super().__init__('SECRET provider response')


@pytest.mark.parametrize('code,n_calls',[(429,3),(503,3),(401,1),(403,1)])
def test_bounded_retries_and_sanitized_errors(settings,monkeypatch,code,n_calls):
    monkeypatch.setattr('aitx.llm.gemini_client.time.sleep',lambda _:None)
    llm,client,cost=invoke(settings,[ProviderError(code)]*3)
    with pytest.raises(StructuredOutputError) as e:
        llm.structured(stage='x',system='s',user='u',output_model=Answer)
    assert len(client.calls)==n_calls and 'SECRET' not in str(e.value)
    assert cost.reserved_usd==0
    assert bool(cost.calls)==(code==503)


def test_parallel_tool_calls_keep_signatures_ids_and_results(settings):
    parts=[types.Part(function_call=types.FunctionCall(id='a',name='lookup',args={'q':'A'}),thought_signature=b'opaque'),
           types.Part(function_call=types.FunctionCall(id='b',name='lookup',args={'q':'B'}))]
    llm,client,cost=invoke(settings,[response(parts=parts),response()])
    seen=[]
    tb=NS(specs=lambda names,strict: [{'name':'lookup','description':'Lookup','input_schema':{'type':'object','properties':{'q':{'type':'string'}}}}],
          execute=lambda name,args:(seen.append(args) or json.dumps(args),False))
    out=llm.agent(stage='x',system='s',user='u',output_model=Answer,toolbox=tb,tool_names=None,max_rounds=1)
    assert out.result=='ok' and len(seen)==2
    history=client.calls[-1]['contents']
    assert history[1].parts[0].thought_signature==b'opaque'
    assert [p.function_response.id for p in history[2].parts]==['a','b']
    assert client.calls[-1]['config'].tools is None


def test_price_schedule_expiry():
    assert argon_prices(date(2026,11,30))==(2,10,0.1)
    assert argon_prices(date(2026,12,1))==(4,20,0.2)


class FillToolbox:
    """ToolBox-compatible stub whose results grow the conversation history without any network use.
    Each result carries its call number so tests can tell which rounds survived compaction."""

    def __init__(self, size=20000):
        self.size = size
        self.calls = 0

    def specs(self, names, strict=False):
        return [{"name": "fill", "description": "Fills history", "input_schema": {"type": "object", "properties": {},
                                                                                  "required": [], "additionalProperties": False}}]

    def execute(self, name, args):
        self.calls += 1
        return f"R{self.calls} " + "x" * self.size, False


def _tool_results(contents) -> list[str]:
    out = []
    for c in contents:
        for p in c.parts or []:
            fr = p.function_response
            if fr is not None and isinstance(fr.response, dict):
                out.append(fr.response.get("result", ""))
    return out


def _request_bound(kw) -> int:
    payload = json.dumps({"contents": [c.model_dump(mode="json", exclude_none=True) for c in kw["contents"]],
                          "config": kw["config"].model_dump(mode="json", exclude_none=True)}, ensure_ascii=False)
    return len(payload.encode("utf-8")) + 4096


def test_agent_compacts_growing_history_instead_of_aborting(settings):
    # A real B_full run aborted at round 17 with "conservative context limit exceeded": the tool loop
    # appends every round forever. The client must trim the oldest rounds (keeping the original user
    # message and the latest rounds) so a long research session can still finalize.
    class RecordingClient(Client):
        def generate_content(self, **kw):
            kw = dict(kw)
            kw["contents"] = list(kw["contents"])  # snapshot: the caller mutates the list between calls
            self.calls.append(kw)
            return self.script.pop(0)

    n_tool_rounds = 58
    script = [response(parts=[types.Part(function_call=types.FunctionCall(id=str(i), name="fill", args={}))])
              for i in range(n_tool_rounds)]
    script.append(response(text='{"result":"ok"}'))  # ends the tool loop
    script.append(response(text='{"result":"ok"}'))  # answers the schema-only finalization
    llm = GeminiLLM(settings, CostTracker(12), client=RecordingClient(script))
    tb = FillToolbox()
    out = llm.agent(stage="x", system="s", user="u", output_model=Answer, toolbox=tb, tool_names=None, max_rounds=60)
    assert out.result == "ok" and tb.calls == n_tool_rounds
    assert llm.compacted_rounds > 0  # the overflow scenario actually exercised the new path
    for kw in llm.client.calls:
        assert _request_bound(kw) + settings.max_tokens <= 1048576 + 8  # no request ever exceeded the bound
    last = llm.client.calls[-1]["contents"]
    assert last[0].parts[0].text == "u"  # original user message preserved
    assert last[-1].parts[0].text.startswith("Return the final JSON")  # finalization prompt preserved
    kept = " ".join(_tool_results(last))
    assert "R1 " not in kept  # the oldest rounds were compacted away
    assert f"R{n_tool_rounds} " in kept  # the latest tool results are intact
    assert 0 < kept.count("R") < n_tool_rounds  # most of the middle history was dropped
    assert len(last) < 1 + 2 * n_tool_rounds + 1  # history is strictly smaller than an uncompacted run


def test_agent_fails_closed_when_history_cannot_fit(settings):
    # If even the last conversation pair exceeds the conservative bound (pathological single tool result),
    # the client must still refuse to send rather than submit an over-limit request.
    script = [response(parts=[types.Part(function_call=types.FunctionCall(id="1", name="fill", args={}))]),
              response(text='{"result":"ok"}')]
    llm, client, cost = invoke(settings, script)
    tb = FillToolbox(size=1_100_000)
    with pytest.raises(StructuredOutputError) as e:
        llm.agent(stage="x", system="s", user="u", output_model=Answer, toolbox=tb, tool_names=None, max_rounds=2)
    assert "context limit" in str(e.value)
    assert llm.compacted_rounds == 0  # nothing could be trimmed
    assert cost.reserved_usd == 0  # the over-limit request was never reserved/sent


def test_usage_with_cached_above_prompt_is_accepted_and_billed_consistently(settings):
    # Second real paid run: on a tool-heavy turn the live API reported cached_content_token_count ABOVE
    # prompt_token_count (prompt excluding the cached prefix) and the client failed closed with
    # "missing or invalid billing metadata". Both shapes must be billed without double counting:
    # fresh prompt + tool results at input rate, cached prefix at the cache rate, flagged uncertain.
    um = types.GenerateContentResponseUsageMetadata(prompt_token_count=5000, candidates_token_count=100,
                                                    thoughts_token_count=200, cached_content_token_count=20000,
                                                    tool_use_prompt_token_count=3000)
    llm, client, cost = invoke(settings, [response(usage=um)])
    assert llm.structured(stage="x", system="s", user="u", output_model=Answer).result == "ok"
    rec = cost.calls[0]
    pin, pout, cache = argon_prices()
    assert rec.input_tokens == 5000 + 3000  # fresh prompt + tool results (prompt excludes cached in this shape)
    assert rec.cache_read_input_tokens == 20000
    assert rec.output_tokens == 300
    # uncertain billing settles at the conservative reserved bound, so the ledger charge must be at
    # least the computed bill and never below it
    bill = (8000 * pin + 20000 * cache + 300 * pout) / 1e6
    assert rec.cost_usd >= bill
    assert rec.uncertain_billing is True  # anomalous shape is flagged, never silently trusted


def test_invalid_usage_error_includes_token_counts_for_diagnosis(settings, monkeypatch):
    # When the shape is genuinely unusable (e.g. a required field missing), the client must still fail
    # closed after the retry budget — but the recorded call detail and the error carry the raw token
    # counts (accounting data, not provider text) so a future service change is diagnosable from the
    # audit without a paid probe.
    monkeypatch.setattr('aitx.llm.gemini_client.time.sleep', lambda _: None)
    um = types.GenerateContentResponseUsageMetadata(prompt_token_count=10, thoughts_token_count=5)
    llm, client, cost = invoke(settings, [response(usage=um)] * 3)
    with pytest.raises(StructuredOutputError) as e:
        llm.structured(stage="x", system="s", user="u", output_model=Answer)
    msg = str(e.value)
    assert "billing metadata" in msg and "candidates=None" in msg
    assert cost.calls[0].stop_reason == "invalid_usage" and cost.calls[0].uncertain_billing is True
    assert "candidates=None" in cost.calls[0].detail  # survives in the audit even if the error is truncated
    assert cost.reserved_usd == 0


def test_real_sdk_request_serialization_without_network(settings):
    import httpx
    from google import genai
    seen=[]
    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200,json=response().model_dump(mode='json',by_alias=True,exclude_none=True))
    client=genai.Client(api_key='offline-placeholder',http_options=types.HttpOptions(
        client_args={'transport':httpx.MockTransport(handler)},retry_options=types.HttpRetryOptions(attempts=1)))
    try:
        llm=GeminiLLM(settings,CostTracker(12),client=client)
        assert llm.structured(stage='x',system='s',user='u',output_model=Answer).result=='ok'
        assert seen[0]['store'] is False
        assert seen[0]['generationConfig']['responseJsonSchema']['type']=='object'
    finally: client.close()


def test_full_pipeline_through_argon_responses(settings,fake_http,test_case):
    from mock_handlers import HANDLERS, hypothesized, established, verify
    from aitx.pipeline import Pipeline
    from aitx.schemas import RevisionPlan, Section
    from aitx.submission import assemble_submission, SubmissionBlocked
    from aitx.qc import check_report_structure
    class ScriptedGemini(GeminiLLM):
        is_mock=True
        def agent(self, **kw):
            self.tb=kw['toolbox']
            return super().agent(**kw)
        def _send(self, stage, contents, config):
            # The transport is replaced; the production request handling,
            # accounting, JSON parsing, and finalization are still exercised.
            if config.tools:
                base=stage.rsplit(':r',1)[0]
                seen=getattr(self,'tool_stages',set())
                names={f.name for t in config.tools for f in t.function_declarations}
                if base not in seen and 'pubmed_search' in names:
                    seen.add(base);self.tool_stages=seen
                    self.client.script.append(response(parts=[types.Part(function_call=types.FunctionCall(
                        id='retrieval',name='pubmed_search',args={'query':'GENEX','max_results':5,'page':1}),
                        thought_signature=b'offline-signature')]))
                else:
                    self.client.script.append(response(text='Research complete; finalize.'))
            else:
                prefix=next(p for p in sorted(HANDLERS,key=len,reverse=True) if stage.startswith(p))
                ctx={'system':config.system_instruction,'toolbox':getattr(self,'tb',None)}
                out=HANDLERS[prefix](ctx)
                if prefix=='C_hypothesized': out.items[0].bespoke_verdict='AMENABLE'
                if prefix=='B_established':
                    out.items[-1].section=Section.OFF_LABEL
                    out.items[-1].human_precedent='Drug Q cured this patient.'
                if prefix=='D_verify':
                    for d in out.verdicts:
                        if d.item_id in ('aso-allele','tgt-q') and d.claim_index==-1: d.verdict='unsupported'
                if prefix=='F_revise': out=RevisionPlan(revisions=[])
                self.client.script.append(response(text=out.model_dump_json()))
            return super()._send(stage,contents,config)
    llm=ScriptedGemini(settings,CostTracker(12),client=Client([]))
    r=Pipeline(llm,settings,http=fake_http,parallel=False,cost=llm.cost).run(test_case)
    assert r.audit['stages_completed']
    assert llm.tool_stages
    assert check_report_structure(r.output.report_markdown,test_case['id']).ok
    assert 'Drug Q cured this patient' not in r.output.report_markdown
    assert not any(i['bespoke_verdict']=='AMENABLE' for i in r.audit['final_items'])
    assert 'Compound R' in r.output.report_markdown
    assert assemble_submission([r],[test_case['id']],allow_mock=True)
    with pytest.raises(SubmissionBlocked): assemble_submission([r],[test_case['id']])
