import asyncio
import json
from pathlib import Path

from threads_agent.agent import execute
from threads_agent.browser import Browser
from threads_agent.core import Run
from threads_agent.extraction import public_snapshot,build_data
from test_browser import html,no_dns

URL='https://www.threads.com/@fixture_author/post/ROOT'
FIXTURE=Path(__file__).parent/'fixtures/thread.json'


def test_one_parse_call_and_only_needed_post_tool():
    result,trace=asyncio.run(execute('貼文內容 '+URL,mode='mock',fixture=FIXTURE))
    assert result['status']=='complete' and result['usage']['mock_calls']==1
    assert [e['phase'] for e in trace['events'] if e['type']=='llm_call']==['parse']
    assert [e['name'] for e in trace['events'] if e['type']=='tool_call']==['read_post']


def test_visible_reply_context_without_internal_parent_ids():
    # Semantic HTML fixture: public permalinks/reply links and visible body text only.
    def card(code,author,text,parent=None):
        return '<article><h2>Example Author</h2><a href="https://www.threads.com/@'+author+'/post/'+code+'"><time>1h</time></a>' + (
            '<a rel="in-reply-to" href="https://www.threads.com/@fixture_author/post/'+parent+'">Reply context</a>' if parent else '') + '<div dir="auto">'+text+'</div></article>'
    body='<body><form action="/accounts/login"><button>Log in</button></form>'
    body+=card('ROOT','fixture_author','Original first line<br>second line')
    body+=card('A1','fixture_author','Visible author continuation','ROOT')
    body+=card('R1','reader','Reader comment','ROOT')
    body+=card('AR','fixture_author','Reply to reader','R1')
    body+='</body>'
    async def transport(url):
        assert url==URL
        return html(body)
    result,trace=asyncio.run(execute(URL,mode='none',fields=['display_name','post_text','continuations'],
        browser_factory=lambda run:Browser(run,transport=transport,dns=no_dns,settle_ms=100)))
    assert result['status']=='complete'
    assert result['data']['post_text']['value']=='Original first line\nsecond line'
    assert [x['post_id'] for x in result['data']['continuations']['value']]==['A1']
    assert result['data']['continuations']['value'][0]['order_evidence']['relationship_source']=='visible_reply_context_link'
    assert not any(i['reason_code']=='login_or_challenge' for i in result['issues'])


def test_root_only_json_does_not_mean_thread_region_observed():
    post={'code':'ROOT','user':{'username':'fixture_author'},'caption':{'text':'Main post'},'is_caption_truncated':False}
    doc={'canonical':URL,'scripts':[json.dumps(post)]}
    snapshot=public_snapshot(doc,URL,URL,[],'synthetic-time')
    data,_=build_data(snapshot,['post_text','continuations'])
    assert data['post_text']['status']=='ok'
    assert snapshot['thread_observed'] is False
    assert data['continuations']['status']=='unconfirmed' and data['continuations']['value'] is None
    run=Run();run.source=URL;run.requested=['post_text','continuations'];run.snapshot=snapshot
    assert run.finish(data)[0]['status']=='partial'
    # A real observed reply region is sufficient; no whole-platform termination proof is needed.
    doc['thread_observed']=True
    observed=public_snapshot(doc,URL,URL,[],'synthetic-time')
    continuation=build_data(observed,['continuations'])[0]['continuations']
    assert continuation['status']=='ok' and continuation['value']==[]


def test_same_id_visible_dom_truncation_is_not_discarded():
    post={'code':'ROOT','user':{'username':'fixture_author'},'caption':{'text':'Visible prefix'}}
    doc={'canonical':URL,'scripts':[json.dumps(post)],
         'visible_posts':[{'url':URL,'text':'Visible prefix','truncated':True}]}
    snapshot=public_snapshot(doc,URL,URL,[],'synthetic-time')
    main=build_data(snapshot,['post_text'])[0]['post_text']
    assert main['status']=='partial' and main['value']=='Visible prefix'
    assert main['reason_code']=='text_completeness_unconfirmed'
    assert snapshot['posts'][0]['dom_truncated'] is True


def test_same_id_json_missing_parent_preserves_visible_reply_context():
    cards=[{'url':URL,'text':'Main','truncated':False},
           {'url':URL.replace('ROOT','C1'),'text':'Continuation','truncated':False,'parent_url':URL}]
    posts=[{'code':code,'user':{'username':'fixture_author'},'caption':{'text':text},'is_caption_truncated':False}
           for code,text in [('ROOT','Main'),('C1','Continuation')]]
    for scripts in [[],[json.dumps(posts)]]:
        snapshot=public_snapshot({'canonical':URL,'scripts':scripts,'visible_posts':cards},URL,URL,[],'synthetic-time')
        continuation=build_data(snapshot,['continuations'])[0]['continuations']
        assert continuation['status']=='ok'
        assert [p['post_id'] for p in continuation['value']]==['C1']
        assert continuation['value'][0]['parent_post_id']=='ROOT'
        assert continuation['value'][0]['order_evidence']['relationship_source']=='visible_reply_context_link'
    # Same author and adjacent cards alone do not establish a parent.
    cards[1].pop('parent_url')
    snapshot=public_snapshot({'canonical':URL,'scripts':[json.dumps(posts)],'visible_posts':cards},URL,URL,[],'synthetic-time')
    assert build_data(snapshot,['continuations'])[0]['continuations']['value'] is None


def test_same_id_json_dom_reply_context_conflicts_remain_unconfirmed():
    root={'code':'ROOT','user':{'username':'fixture_author'},'caption':{'text':'Main'},'is_caption_truncated':False}
    for parent,text in [('OTHER','Continuation'),(None,'Different observed text')]:
        child={'code':'C1','user':{'username':'fixture_author'},'caption':{'text':'Continuation'},'is_caption_truncated':False}
        if parent:child['text_post_app_info']={'reply_to_post':{'code':parent}}
        doc={'canonical':URL,'scripts':[json.dumps([root,child])],
             'visible_posts':[{'url':URL.replace('ROOT','C1'),'text':text,'truncated':False,'parent_url':URL}]}
        snapshot=public_snapshot(doc,URL,URL,[],'synthetic-time')
        node=next(p for p in snapshot['posts'] if p['id']=='C1')
        assert node['evidence_conflict'] and node['parent'] is None and node['order'] is None
        assert node['conflicts'][0]['dom_parent']=='ROOT'
        data,decisions=build_data(snapshot,['continuations'])
        assert data['continuations']['status']=='unconfirmed' and data['continuations']['value'] is None
        assert decisions==[{'id':'C1','classification':'ambiguous_evidence_conflict'}]
