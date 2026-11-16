const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const base=fs.readFileSync(path.join(__dirname,'check_frontend.cjs'),'utf8');
const h=new Function('require','__dirname',base.slice(0,base.indexOf('(async () => {'))+'\nreturn {context,elements,evaluate,tick,html,root};')(require,__dirname);
const {context,elements,evaluate,tick,html,root}=h;
const response=value=>({ok:true,status:200,json:async()=>value,blob:async()=>({})});
(async()=>{
  for(const m of html.matchAll(/<script defer src="\/static\/([^"]+)"><\/script>/g)) vm.runInContext(fs.readFileSync(path.join(root,'frontend/static',m[1]),'utf8'),context);
  await tick(); evaluate('token="owner-token";me="owner";enterRoom({roomId:"r",name:"Room",createdBy:"owner"})'); await tick();
  const calls=[];
  const stored={jobId:'saved',status:'COMPLETED',kind:'meeting-summary',target:'ko',attempts:1,result:{text:'저장 요약 [1]',evidence:[{id:1,text:'original',sentAt:0,source:{kind:'message',messageSeq:1}}]}};
  context.fetch=async(url,options={})=>{
    calls.push({url,method:options.method||'GET',body:options.body&&JSON.parse(options.body)});
    if(url==='/api/rooms/r/ai-results') return response([{jobId:'saved',kind:'meeting-summary',title:'저장 요약',target:'ko',createdAt:1}]);
    if(url.startsWith('/api/rooms/r/ai-results/') || url==='/api/ai/jobs/saved') return response(stored);
    if(url==='/api/ai/jobs') return response({jobId:'saved'});
    if(url==='/api/ai/translate') return response({translated:'원문 번역'});
    if(url==='/api/media/f') return response({status:'UPLOADED',progress:0});
    if(url==='/api/auth/signup') return response({token:'new-token',nickname:'japanese'});
    if(url==='/api/users/me/preferences') return response({uiLocale:'ja',sourceLanguage:'ja',defaultTargetLanguage:'ja',preferredSubtitleLanguage:'ja'});
    return response([]);
  };
  elements.get('roomSummary').onclick(); await tick();
  assert.equal(calls.filter(c=>c.method==='POST').length,0);
  await elements.get('summaryList').children[0].onclick();
  assert.equal(calls.filter(c=>c.method==='POST').length,0,'opening saved evidence must not auto-translate');
  const evidenceBox=elements.get('summaryResult').children[1];
  await evidenceBox.children.at(-1).onclick();
  assert.equal(calls.filter(c=>c.url==='/api/ai/translate').length,1,'evidence translation is explicit');
  elements.get('summaryAdd').onclick();
  assert.equal(elements.get('summarySelection').hidden,false);
  evaluate('updateSummarySelection(1,{kind:"message",messageSeq:1},8,true)');
  assert.ok(elements.get('summaryCount').textContent.startsWith('1개'));
  const before=calls.filter(c=>c.method==='POST').length;
  await elements.get('summaryGenerate').onclick();
  assert.equal(calls.filter(c=>c.method==='POST').length,before+1);
  assert.equal(evaluate('aiSelections.size'),0);
  // A file card in UPLOADED state offers original/AI actions without scheduling extraction.
  evaluate('fileCard({mediaId:"f",filename:"sample.pdf",ext:"pdf",size:3})'); await tick();
  assert.equal(calls.filter(c=>c.url==='/api/media/f/process').length,0);
  assert.equal(calls.filter(c=>c.url==='/api/media/f/transcript').length,0);
  // A late panel response after room close cannot populate the next room.
  let finish;
  context.fetch=()=>new Promise(resolve=>finish=resolve);
  const load=evaluate('loadSummaryList()'); evaluate('resetAiPanel()'); finish(response([{kind:'meeting-summary',jobId:'old'}])); await load;
  assert.equal(elements.get('summaryList').children.length,0);
  context.fetch=async(url,options={})=>{
    calls.push({url,method:options.method||'GET',body:options.body&&JSON.parse(options.body)});
    return response(url==='/api/auth/signup'?{token:'signup-token',nickname:'japanese'}:url==='/api/users/me/preferences'?{uiLocale:'ja',defaultTargetLanguage:'ja',sourceLanguage:'ja',preferredSubtitleLanguage:'ja'}:[]);
  };
  evaluate('authMode="signup";locale="ja"'); elements.get('user').value='japanese'; elements.get('authPw').value=elements.get('authPw2').value='secret123';
  await elements.get('authForm').onsubmit({preventDefault(){}});
  assert.equal(calls.find(c=>c.url==='/api/auth/signup').body.uiLocale,'ja');
  console.log('Saved-result UI checks passed: read-only panel, explicit evidence translation, selection mode, deferred extraction, stale panel rejection, signup language');
})().catch(e=>{console.error(e);process.exitCode=1;});
