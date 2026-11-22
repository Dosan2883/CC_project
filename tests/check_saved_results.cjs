const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const base=fs.readFileSync(path.join(__dirname,'check_frontend.cjs'),'utf8');
const h=new Function('require','__dirname',base.slice(0,base.indexOf('(async () => {'))+'\nreturn {context,elements,evaluate,tick,html,root};')(require,__dirname);
const {context,elements,evaluate,tick,html,root}=h;
const response=value=>({ok:true,status:200,json:async()=>value,blob:async()=>({})});
(async()=>{
  for(const m of html.matchAll(/<script defer src="\/static\/([^"]+)"><\/script>/g)) vm.runInContext(fs.readFileSync(path.join(root,'frontend/static',m[1]),'utf8'),context);
  await tick(); evaluate('token="owner-token";me="owner";enterRoom({roomId:"r",name:"Room",createdBy:"owner"})'); await tick();
  const calls=[];
  const result={text:'저장 요약 [1]',evidence:[{id:1,text:'original',sentAt:0,source:{kind:'message',messageSeq:1}}]};
  const stored={jobId:'saved',entryId:'saved',title:'저장 요약',requester:'owner',status:'COMPLETED',kind:'meeting-summary',target:'ko',attempts:1,result};
  const summaries=[{jobId:'saved',entryId:'saved',kind:'meeting-summary',title:'저장 요약',target:'ko',requester:'owner',createdAt:1,updatedAt:1}];
  let reuse=false;
  context.fetch=async(url,options={})=>{
    calls.push({url,method:options.method||'GET',body:options.body&&JSON.parse(options.body)});
    if(url==='/api/users/me/ai-consent') return response({allowed:true});
    if(url==='/api/ai/rooms/r/summaries') return response(summaries);
    if(url==='/api/ai/rooms/r/summaries/saved') return response({entryId:'saved',title:'저장 요약',result});
    if(url==='/api/ai/jobs') return response(reuse ? {...stored, existing:true, cached:true} : {jobId:'saved'});
    if(url==='/api/ai/jobs/saved') return response(stored);
    if(url==='/api/ai/translate') return response({translated:'원문 번역'});
    if(url==='/api/media/f') return response({status:'UPLOADED',progress:0});
    if(url==='/api/media/long-pdf/transcript') return response({pages:['가'.repeat(3600)]});
    if(url==='/api/auth/signup') return response({token:'new-token',nickname:'japanese'});
    if(url==='/api/users/me/preferences') return response({uiLocale:'ja',sourceLanguage:'ja',defaultTargetLanguage:'ja',preferredSubtitleLanguage:'ja'});
    return response([]);
  };
  elements.get('roomSummary').onclick(); await tick();
  assert.equal(elements.get('docPanel').hidden,false,'room summary opens the current summary panel');
  assert.ok(calls.some(c=>c.url==='/api/ai/rooms/r/summaries'),'opening the panel loads the current summary list');
  assert.equal(calls.filter(c=>c.method==='POST').length,0);
  const savedItem=elements.get('sumView').children[0];
  await savedItem.children[2].children[0].onclick();
  assert.ok(calls.some(c=>c.url==='/api/ai/rooms/r/summaries/saved'));
  assert.equal(calls.filter(c=>c.url==='/api/ai/translate').length,0,'opening a saved summary does not auto-translate evidence');

  evaluate('updateSummarySelection(1,{kind:"message",messageSeq:1},8,true)');
  assert.ok(elements.get('sumCount').textContent.includes('1'));
  const before=calls.filter(c=>c.method==='POST').length;
  await elements.get('sumGenerate').onclick(); await tick();
  assert.equal(calls.filter(c=>c.method==='POST').length,before+1);
  assert.ok(calls.some(c=>c.url==='/api/ai/jobs'&&c.method==='POST'));
  assert.equal(calls.filter(c=>c.url==='/api/ai/translate').length,0,'completed summary evidence remains untranslated until requested');
  const float=context.document.body.children.find(node=>node.className==='float-win');
  const evidenceBox=float.children[1].children.at(-1), translateButton=evidenceBox.children[1].children[1];
  await translateButton.onclick();
  assert.equal(calls.filter(c=>c.url==='/api/ai/translate').length,1,'only the explicit evidence action calls translation');

  reuse=true;
  const polls=calls.filter(c=>c.url==='/api/ai/jobs/saved').length;
  await elements.get('sumGenerate').onclick(); await tick();
  const reused=context.document.body.children.filter(node=>node.className==='float-win').at(-1);
  assert.equal(reused.children[1].children[0].textContent,evaluate('t("summaryExists")'));
  assert.equal(calls.filter(c=>c.url==='/api/ai/jobs/saved').length,polls,'a reused summary does not poll a job');

  await evaluate('pdfSummarizeAll({mediaId:"long-pdf"})');
  const pdfJob=calls.filter(c=>c.url==='/api/ai/jobs'&&c.method==='POST').at(-1);
  assert.deepEqual(pdfJob.body.sources,[{kind:'page',mediaId:'long-pdf',page:1}],'PDFs above 3500 characters reach the server');

  // A file card in UPLOADED state offers original/AI actions without scheduling extraction.
  evaluate('fileCard({mediaId:"f",filename:"sample.pdf",ext:"pdf",size:3})'); await tick();
  assert.equal(calls.filter(c=>c.url==='/api/media/f/process').length,0);
  assert.equal(calls.filter(c=>c.url==='/api/media/f/transcript').length,0);
  // A late panel response after the room closes cannot populate the next room.
  let finish;
  context.fetch=()=>new Promise(resolve=>finish=resolve);
  const load=evaluate('loadSummaries()'); evaluate('closeDocPanel()'); finish(response([{kind:'meeting-summary',jobId:'old'}])); await load;
  assert.equal(elements.get('sumView').children.length,0);
  context.fetch=async(url,options={})=>{
    calls.push({url,method:options.method||'GET',body:options.body&&JSON.parse(options.body)});
    return response(url==='/api/auth/signup'?{token:'signup-token',nickname:'japanese'}:url==='/api/users/me/preferences'?{uiLocale:'ja',defaultTargetLanguage:'ja',sourceLanguage:'ja',preferredSubtitleLanguage:'ja'}:[]);
  };
  evaluate('authMode="signup";locale="ja"'); elements.get('user').value='japanese'; elements.get('authPw').value=elements.get('authPw2').value='secret123';
  await elements.get('authForm').onsubmit({preventDefault(){}});
  assert.equal(calls.find(c=>c.url==='/api/auth/signup').body.uiLocale,'ja');
  console.log('Saved-result UI checks passed: current summary panel, explicit evidence translation, selection mode, deferred extraction, stale panel rejection, signup language');
})().catch(e=>{console.error(e);process.exitCode=1;});
