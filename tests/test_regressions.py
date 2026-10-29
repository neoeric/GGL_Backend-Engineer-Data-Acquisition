import asyncio
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from threads_agent.agent import execute
from threads_agent.browser import Browser
from threads_agent.core import Failure, Run, field, validate_result
from threads_agent.extraction import public_snapshot, build_data
from threads_agent.model import Model
from test_browser import BASE,scenario,html,no_dns
from test_contract import URL,load

RAW=Path(__file__).parent/'fixtures/raw-public-thread.json'


def test_raw_public_adapter_to_final_result_preserves_chain_order():
    raw=json.loads(RAW.read_text(encoding='utf-8'))
    async def test():
        async def transport(url):
            if url!=URL:raise AssertionError(url)
            body='<link rel="canonical" href="'+URL+'"><script type="application/json">'+json.dumps(raw['public_data'])+'</script><body>public fixture</body>'
            return html(body)
        result,trace=await execute(URL,mode='none',fields=['post_text','continuations'],
            browser_factory=lambda run:Browser(run,transport=transport,dns=no_dns,settle_ms=100))
        return result,trace
    result,trace=asyncio.run(test())
    assert result['status']=='complete'  # observed fields; no global termination-proof gate
    assert result['data']['post_text']['value']=='原文\n保留換行'
    items=result['data']['continuations']['value']
    assert [x['post_id'] for x in items]==['A1','A2']
    assert [x['order'] for x in items]==[1,2]
    assert items[1]['order_evidence']['post_ids']==['ROOT','A1','A2']
    assert 'ARS' not in [x['post_id'] for x in items]
    assert not any(e['type']=='llm_call' for e in trace['events'])


def test_raw_branch_fork_has_no_invented_order():
    raw=json.loads(RAW.read_text(encoding='utf-8'))
    raw['public_data']['thread_items'][0]['post']['text_post_app_info']['reply_to_post']['code']='ROOT'
    snap=public_snapshot({'canonical':URL,'scripts':[json.dumps(raw['public_data'])]},URL,URL,[], 'fixture-time')
    data,_=build_data(snap,['continuations'])
    assert data['continuations']['value'] is None


def test_raw_fixture_cli_agent_path_remains_explicitly_synthetic():
    result,trace=asyncio.run(execute(URL,mode='none',fields=['post_text','continuations'],fixture=RAW))
    assert result['evidence_mode']=='synthetic_fixture' and result['status']=='complete'
    assert [x['post_id'] for x in result['data']['continuations']['value']]==['A1','A2']


@pytest.mark.parametrize('body',['<script>location.reload()</script>', '<meta http-equiv="refresh" content="0">'])
def test_same_url_reload_and_meta_are_loops(body):
    error,fetched,chain,_=asyncio.run(scenario({BASE+'/share/START/':html(body)}))
    assert error=='redirect_loop' and len(fetched)==1


def test_public_captcha_discussion_is_not_a_challenge():
    error,_,_,doc=asyncio.run(scenario({BASE+'/share/START/':html('<body>This post discusses CAPTCHA and verify you are human UI.</body>')}))
    assert error is None and doc['blocked'] is False


def test_structural_challenge_stops():
    error,_,_,_=asyncio.run(scenario({BASE+'/share/START/':html('<body><form action="/challenge"><button>Verify</button></form></body>')}))
    assert error=='login_or_challenge'


def test_diagnostics_explain_program_policy_without_claiming_login():
    async def test():
        async def transport(url):
            if url.endswith('/share/START/'):
                return html('<script>fetch("/graphql/query",{method:"POST",body:"query"}).catch(()=>{});</script><script src="https://outside.invalid/x.js"></script>')
            raise AssertionError(url)
        r=Run();r.source=BASE+'/share/START/'
        b=Browser(r,transport=transport,dns=no_dns,settle_ms=150)
        try:s=await b.read_post()
        finally:await b.close()
        return r,s
    r,s=asyncio.run(test())
    assert s['acquisition_diagnostics']['counts']['policy_non_get_blocked']>=1
    assert any(x['reason_code']=='acquisition_policy_limited' for x in r.issues)
    assert not any(x['reason_code']=='login_or_challenge' for x in r.issues)
    assert 'query' not in json.dumps(s['acquisition_diagnostics']) or all('body' not in x for x in s['acquisition_diagnostics']['requests'])


def model_factory(monkeypatch,content,finish='stop'):
    monkeypatch.setenv('LLM_MODEL','fake-test-model')
    monkeypatch.setenv('LLM_API_KEY','sk-FAKE_TEST_TRANSPORT_KEY_987654321')
    def handler(request):
        return httpx.Response(200,json={'id':'fake-response-id','choices':[{'message':{'content':content,'reasoning_content':'NEVER_LOG_PRIVATE_REASONING'},'finish_reason':finish}],
            'usage':{'prompt_tokens':7,'completion_tokens':5}})
    return lambda r,mode:Model(r,mode,transport=httpx.MockTransport(handler))


@pytest.mark.parametrize('content,finish,code',[
    ('{"observed":"RESPONSE_EVIDENCE"}','length','model_incomplete_response'),
    ('{"observed":"RESPONSE_EVIDENCE"','stop','model_response_error'),
    ('["RESPONSE_EVIDENCE"]','stop','model_schema_error')])
def test_error_model_response_evidence_is_retained(monkeypatch,content,finish,code):
    r=Run();m=model_factory(monkeypatch,content,finish)(r,'openai-compatible')
    with pytest.raises(Failure,match=code):asyncio.run(m.call('parse',{'message':'test'}))
    dumped=json.dumps(r.events)
    assert 'RESPONSE_EVIDENCE' in dumped and 'NEVER_LOG_PRIVATE_REASONING' not in dumped
    assert r.usage()['total_tokens']==12


def test_long_model_response_has_explicit_evidence_bound(monkeypatch):
    r=Run();m=model_factory(monkeypatch,json.dumps({'observed':'X'*10000}),'length')(r,'openai-compatible')
    with pytest.raises(Failure):asyncio.run(m.call('parse',{}))
    evidence=r.events[-1]['output']['received_response']
    assert evidence['truncated'] and len(evidence['content_excerpt'])==4096
    assert evidence['sanitized_chars']>10000 and len(evidence['sha256_of_sanitized_content'])==64
    assert r.events[-1]['output']['parsed_content']['omitted'] is True
    assert len(json.dumps(r.events[-1]['output']))<6000


@pytest.mark.parametrize('bad_key,bad_value',[('unsupported_fields',{'unexpected':'object'}),('invalid_reason',True),('requested_fields',[{'bad':'field'}])])
def test_malformed_parse_is_model_error_not_invalid_input(monkeypatch,bad_key,bad_value):
    parsed={'url':URL,'requested_fields':['post_text'],'unsupported_fields':[],'invalid_reason':None}
    parsed[bad_key]=bad_value
    result,trace=asyncio.run(execute('post_text '+URL,model_factory=model_factory(monkeypatch,json.dumps(parsed))))
    assert result['status']=='error'
    assert any(x['reason_code']=='model_schema_error' for x in result['issues'])
    assert trace['events'][-1]['type']=='run_end'


@pytest.mark.parametrize('args',[{'seconds':float('nan')},{'seconds':float('inf')},{'seconds':0},{'max_tools':0},{'max_llm':-1},{'max_tools':1.5}])
def test_bad_budgets_have_stable_json_result(args):
    result,trace=asyncio.run(execute('post_text '+URL,mode='none',fields=['post_text'],**args))
    assert result['status']=='invalid_request'
    assert trace['events'][-1]['type']=='run_end'
    json.dumps(result,allow_nan=False)


def test_schema_survives_python_optimized_mode():
    code="from threads_agent.core import validate_field,Failure;\ntry: validate_field('post_text',{'status':'ok','value':None})\nexcept Failure: print('REJECTED')\nelse: raise SystemExit(9)"
    p=subprocess.run([sys.executable,'-O','-c',code],env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src')),capture_output=True,text=True)
    assert p.returncode==0 and 'REJECTED' in p.stdout


def test_bad_result_field_falls_back_without_losing_trace():
    r=Run();r.requested=['post_text','display_name'];r.snapshot=load();r.event('evidence',output={'observed':'KEEP'})
    result,trace=r.finish({'post_text':field('ok',None),'display_name':field('ok','valid')})
    assert result['status']=='partial' and result['data']['display_name']['value']=='valid'
    assert result['data']['post_text']['status']=='error'
    assert trace['events'][0]['output']['observed']=='KEEP'
    assert trace['events'][-1]['type']=='run_end'


def test_source_bearer_text_is_preserved_but_secret_is_partial(monkeypatch):
    secret='sk-FAKE_SECRET_NEVER_SEND_123456789'
    monkeypatch.setenv('LLM_API_KEY',secret)
    for text,expected_status in [('A public Bearer example123 discussion','complete'),('Secret '+secret,'partial')]:
        r=Run();r.requested=['post_text'];r.snapshot=load()
        result,trace=r.finish({'post_text':field('ok',text)})
        assert result['status']==expected_status
        if expected_status=='complete':assert result['data']['post_text']['value']==text
        else:
            assert result['data']['post_text']['sanitized'] and result['data']['post_text']['reason_code']=='source_text_sanitized'
            assert secret not in json.dumps([result,trace])
