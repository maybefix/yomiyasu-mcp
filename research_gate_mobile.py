"""iPhone-friendly mobile UI for Chat Research Gate; shares the MCP research engine."""
import asyncio
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="research")
JOBS = {}
RATE = {}
QUERY_CACHE = {}
INFLIGHT = {}
LOCK = Lock()
MAX_AGE = 3600

PAGE = r'''<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#172b35"><meta name="color-scheme" content="light">
<title>Chat Research Gate | iPhone調査</title>
<style>
:root{font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",Meiryo,sans-serif;color:#16272d;background:#f4f6f5}
*{box-sizing:border-box}body{margin:0;padding:0 16px 100px}.wrap{max-width:780px;margin:auto}
header{background:#172b35;color:white;margin:0 -16px 20px;padding:22px max(16px,env(safe-area-inset-left));}
header .wrap{display:flex;align-items:center;gap:12px}.logo{font-size:24px}
h1{font-size:19px;margin:0 0 4px}header p{margin:0;font-size:12px;color:#d6e2e5}
h2{font-size:17px;margin:20px 0 10px}
.panel{background:white;border:1px solid #e2e9e6;border-radius:14px;padding:16px;margin-bottom:14px}
label{display:block;font-weight:650;font-size:14px;margin-bottom:10px}
textarea{display:block;width:100%;min-height:108px;font:inherit;font-size:16px;line-height:1.55;padding:12px;border:1px solid #abbcb8;border-radius:10px;resize:vertical;background:white;color:#14252b}
textarea:focus{outline:2px solid #477c82;outline-offset:1px}
button{border:0;border-radius:10px;padding:13px 17px;font:inherit;font-weight:700;cursor:pointer}
button.primary{background:#17636b;color:white;width:100%;margin-top:13px}
button:disabled{opacity:.55;cursor:wait}
button.secondary{background:#e3eeed;color:#174f55}
.actions{display:flex;gap:8px;flex-wrap:wrap}.actions button{flex:1}
.small{font-size:12px;line-height:1.6;color:#56706d}
.pill{display:inline-block;padding:4px 9px;border-radius:999px;background:#e8f0ed;font-size:12px}
.ok{color:#17634c}.warn{color:#9b490e}
.status{font-size:14px;line-height:1.6}
.source{padding:12px 0;border-top:1px solid #e5ebe9}
.source a{color:#006972;font-weight:650;overflow-wrap:anywhere}
blockquote{font-size:13px;border-left:3px solid #91b6b4;margin:10px 0 4px;padding:3px 0 3px 12px;white-space:pre-wrap;overflow-wrap:anywhere;color:#3b4f52}
.topic{padding:14px;border:1px solid #dce7e3;border-radius:12px;margin:12px 0;background:white}
.topic h3{font-size:15px;margin:0 0 8px;overflow-wrap:anywhere}
.topic .small{margin:4px 0}
progress{width:100%;height:9px;accent-color:#17636b}
.hidden{display:none}
#events{white-space:pre-wrap}
footer{text-align:center;font-size:12px;color:#6c8581;margin-top:24px}
@media(min-width:720px){header{padding-top:30px;padding-bottom:30px}.panel{padding:24px}}
</style>
</head>
<body><header><div class="wrap"><span class="logo">▤</span><div><h1>Chat Research Gate</h1><p>スマートフォン版・資料の取得と再検索</p></div></div></header>
<main class="wrap">
<div class="panel">
<label for="question">調査したい質問</label>
<textarea id="question" placeholder="例：Tauri 2ではRustの中間ファイルをRAM上に保存できますか。現行仕様、実例、制約を確認してください。"></textarea>
<p class="small">質問を入力すると、資料本文を取得し、証拠が不足する論点について最大3回検索します。追加の有料AI APIは不要です。</p>
<button class="primary" id="start">資料を調査する</button>
</div>
<section class="panel hidden" id="progressPanel" aria-live="polite"><div class="status" id="status">調査を開始しています</div><progress id="progress" max="100" value="10"></progress><div class="small" id="elapsed"></div></section>
<section class="hidden" id="resultPanel"><div class="panel">
<h2>調査結果</h2><div id="summary" class="status"></div>
<div class="actions"><button class="secondary" id="copy">結果をコピー</button><button class="secondary" id="share">ChatGPTへ共有</button></div>
</div><div id="topics"></div>
<div class="panel"><p class="small">資料取得率は結論の正答確率ではありません。ChatGPTへ共有すると、収集した資料を根拠に文章を組み立てられます。Safariの共有画面にChatGPTがない場合は「結果をコピー」後、ChatGPTに貼り付けてください。</p></div></section>
<footer>このページはChatGPT内のMCPアプリではなく、iPhoneからアクセスできる専用Web画面です。</footer>
</main>
<script>
(()=>{'use strict';
const $=id=>document.getElementById(id);
let output=null,started=0,activeJob=null;
function node(t,c){let n=document.createElement(t);if(c)n.className=c;return n}
function safeURL(s){try{let u=new URL(s);return (u.protocol==='https:'||u.protocol==='http:')?u.href:null}catch{return null}}
function render(data){
 output=data; $('resultPanel').classList.remove('hidden');
 const items=data.topics||[],complete=items.filter(x=>x.status==='fetched').length;
 $('summary').textContent='本文を取得できた論点：'+complete+'/'+items.length+'（'+data.retrieval_coverage_percent+'％）。検索実施：'+data.rounds_executed+'回。';
 const root=$('topics');root.replaceChildren();
 for(const row of items){
  const art=node('article','topic'), title=node('h3');title.textContent=row.topic;art.append(title);
  const count=node('div','small '+(row.status==='fetched'?'ok':'warn'));
  count.textContent=row.status==='fetched'?'資料取得 '+row.source_count+'件':'追加の証拠が不足：'+row.source_count+'件';
  art.append(count);
  for(const source of row.sources||[]){
   const card=node('div','source'), a=node('a');a.textContent=source.title||source.url;
   const url=safeURL(source.url);if(url){a.href=url;a.target='_blank';a.rel='noopener noreferrer'}
   card.append(a);const quote=node('blockquote');quote.textContent=source.quote||'';card.append(quote);art.append(card);
  }
  root.append(art);
 }
 $('resultPanel').scrollIntoView({behavior:'smooth',block:'start'});
}
function shareText(){
 if(!output)return '';
 const all=['次のWebページ本文の引用に基づいて、質問に具体的に回答してください。必要な場合は追加検索を実行し、各論点について結論と出典を表にまとめてください。資料取得が完了したというだけで結論を確定しないでください。',
 '質問：'+output.question, '取得資料の一覧：'];
 for(const t of output.topics||[]){
  all.push('論点：'+t.topic);
  for(const s of t.sources||[])all.push('出典：'+s.url+'\n引用：'+s.quote);
 }
 return all.join('\n\n').slice(0,32000);
}
async function copyText(t){
 try{await navigator.clipboard.writeText(t);return true}catch{
  const area=node('textarea');area.value=t;document.body.append(area);area.select();const ok=document.execCommand('copy');area.remove();return ok;
 }}
$('copy').onclick=async()=>{let ok=await copyText(shareText());$('copy').textContent=ok?'コピーしました':'コピーに失敗';};
$('share').onclick=async()=>{
 const text=shareText();
 if(navigator.share){try{await navigator.share({title:'Chat Research Gate 調査資料',text});return}catch(e){if(e.name==='AbortError')return}}
 const ok=await copyText(text);$('share').textContent=ok?'コピーしました。ChatGPTに貼り付け':'コピーに失敗';
};
async function poll(id){
 if(activeJob!==id)return;
 try{
  const r=await fetch('/api/research/'+encodeURIComponent(id),{cache:'no-store'});
  if(!r.ok)throw Error('状態取得エラー '+r.status);
  const d=await r.json();
  if(d.status==='finished'){
   $('progress').value=100;$('status').textContent='資料の取得を終了しました';
   $('start').disabled=false;render(d.result);return;
  }
  if(d.status==='error')throw Error(d.error||'調査中にエラーが発生しました');
  let secs=Math.round((Date.now()-started)/1000);
  $('elapsed').textContent='経過 '+secs+'秒。画面を開いたまま進行状況を確認できます。';
  $('progress').value=Math.min(90,15+Math.log1p(secs)*13);
 }catch(e){$('status').textContent='取得エラー：'+e.message;$('start').disabled=false;return}
 setTimeout(()=>poll(id),2500);
}
$('start').onclick=async()=>{
 const question=$('question').value.trim();
 if(!question){$('question').focus();return}
 if(question.length>1800){alert('質問は1800文字以内にしてください');return}
 $('start').disabled=true;$('resultPanel').classList.add('hidden');
 $('progressPanel').classList.remove('hidden');$('status').textContent='調査を開始しています';
 $('progress').value=10;started=Date.now();
 try{
  const r=await fetch('/api/research',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question})});
  const d=await r.json();if(!r.ok)throw Error(d.error||'開始できませんでした');
  activeJob=d.job_id;poll(activeJob);
 }catch(e){$('status').textContent='開始エラー：'+e.message;$('start').disabled=false}
};
const q=new URLSearchParams(location.search).get('q');if(q)$('question').value=q.slice(0,1800);
})();
</script></body></html>'''


def _purge(now):
    expired = [key for key, val in JOBS.items() if now - val["created"] > MAX_AGE]
    for key in expired:
        JOBS.pop(key, None)


def _run_job(job_id, question):
    from research_gate_app import research_to_evidence
    try:
        result = json.loads(research_to_evidence(question, rounds=3, minimum_sources=2))
        with LOCK:
            if job_id in JOBS:
                JOBS[job_id]["status"] = "finished"
                JOBS[job_id]["result"] = result
    except Exception as exc:
        with LOCK:
            if job_id in JOBS:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["error"] = type(exc).__name__ + ": " + str(exc)[:240]


async def homepage(request: Request):
    return HTMLResponse(PAGE,headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff","Content-Security-Policy":"default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'"})


async def health(request: Request):
    return JSONResponse({"status":"ok","mobile":True,"mcp":True})


async def start_research(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error":"JSON形式が不正です"},status_code=400)
    question = payload.get("question","") if isinstance(payload,dict) else ""
    if not isinstance(question,str) or not 1 <= len(question.strip()) <= 1800:
        return JSONResponse({"error":"質問は1～1800文字で指定してください"},status_code=400)
    # Limit concurrent jobs and rate of public endpoint usage.
    client = request.client.host if request.client else "unknown"
    now = time.time()
    with LOCK:
        _purge(now)
        if len(JOBS) >= 30:
            return JSONResponse({"error":"処理枠が混雑しています"},status_code=429)
        recent = [v for v in RATE.get(client,[]) if now-v < 600]
        if len(recent) >= 5:
            return JSONResponse({"error":"10分間の実行上限に達しました"},status_code=429)
        recent.append(now)
        RATE[client]=recent
        jid = uuid.uuid4().hex
        JOBS[jid]={"created":now,"status":"running"}
    POOL.submit(_run_job,jid,question.strip())
    return JSONResponse({"job_id":jid,"status":"running"},status_code=202,
                        headers={"Cache-Control":"no-store"})


async def get_research(request: Request):
    jid = request.path_params["job_id"]
    with LOCK:
        _purge(time.time())
        result = JOBS.get(jid)
        if result is not None:
            result = {k:v for k,v in result.items() if k!="created"}
    if result is None:
        return JSONResponse({"error":"調査データが期限切れまたは存在しません"},status_code=404)
    return JSONResponse(result,headers={"Cache-Control":"no-store"})



async def get_chat_research(request: Request):
    """Single GET for Chat-mode web readers: search first, return evidence after completion."""
    question = request.query_params.get("q", "").strip()
    if not 1 <= len(question) <= 900:
        return JSONResponse({"error": "qは1～900文字で指定してください"}, status_code=400)
    from research_gate_app import research_to_evidence
    now = time.time()
    with LOCK:
        cached = QUERY_CACHE.get(question)
        if cached and now - cached["time"] < 900:
            return JSONResponse(cached["data"], headers={"Cache-Control": "no-store"})
        future = INFLIGHT.get(question)
        if future is None:
            future = POOL.submit(research_to_evidence, question, None, 3, 2)
            INFLIGHT[question] = future
    try:
        result = await asyncio.wait_for(asyncio.wrap_future(future), timeout=75)
        parsed = json.loads(result)
        with LOCK:
            QUERY_CACHE[question] = {"time": time.time(), "data": parsed}
            INFLIGHT.pop(question, None)
            if len(QUERY_CACHE) > 50:
                oldest = min(QUERY_CACHE, key=lambda key: QUERY_CACHE[key]["time"])
                QUERY_CACHE.pop(oldest, None)
        return JSONResponse(parsed, headers={"Cache-Control": "no-store"})
    except asyncio.TimeoutError:
        return JSONResponse({"status": "timeout", "error": "検索時間が上限に達しました", "question": question}, status_code=504)
    except Exception as exc:
        with LOCK:
            INFLIGHT.pop(question, None)
        return JSONResponse({"status": "error", "error": type(exc).__name__ + ": " + str(exc)[:200]}, status_code=500)

async def search_probe(request: Request):
    """Temporary test for public metasearch reachability."""
    from urllib.parse import urlencode
    import httpx
    from lxml import html as lh
    query = request.query_params.get("q", "Python 3.12 release notes")[:120]
    hosts = ["https://search.rhscz.eu", "https://search.utilibre.org", "https://search.banke.cloud", "https://sx.xo.st", "https://searx.tiekoetter.com"]
    def probe(base):
        try:
            if base.endswith("bing.com"):
                url=base+"/search?"+urlencode({"q":query,"format":"rss"})
            else:
                url=base+"/search?"+urlencode({"q":query})
            with httpx.Client(timeout=12,follow_redirects=True,headers={"User-Agent":"Mozilla/5.0 (compatible; ResearchGate/0.2)"}) as client:
                r=client.get(url)
            doc=lh.fromstring(r.text)
            links=[(el.text_content().strip()[:90],el.get("href")) for el in doc.xpath("//article[contains(@class,'result')]//h3/a")[:6]]
            return {"provider":base,"status":r.status_code,"size":len(r.content),"title":doc.xpath("//title/text()")[:1],
                    "links":links, "first":r.text[:220]}
        except Exception as e:
            return {"provider":base,"error":type(e).__name__+": "+str(e)[:200]}
    result=await asyncio.gather(*(asyncio.to_thread(probe,url) for url in hosts))
    return JSONResponse({"q":query,"results":result},headers={"Cache-Control":"no-store"})



def mount_mobile(app):
    app.router.routes.extend([
        Route("/",homepage,methods=["GET"]),
        Route("/healthz",health,methods=["GET"]),
        Route("/api/research",start_research,methods=["POST"]),
        Route("/api/chat-research",get_chat_research,methods=["GET"]),
        Route("/api/search-probe",search_probe,methods=["GET"]),
        Route("/api/research/{job_id}",get_research,methods=["GET"])
    ])
