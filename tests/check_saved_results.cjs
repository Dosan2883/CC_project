const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const base=fs.readFileSync(path.join(__dirname,'check_frontend.cjs'),'utf8');
const h=new Function('require','__dirname',base.slice(0,base.indexOf('(async () => {'))+'\nreturn {context,elements,evaluate,tick,loadFrontendScripts};')(require,__dirname);
const {context,elements,evaluate,tick,loadFrontendScripts}=h;
const response=value=>({ok:true,status:200,json:async()=>value,blob:async()=>({})});
(async()=>{
  loadFrontendScripts();
  await tick(); evaluate('token="owner-token";me="owner";enterRoom({roomId:"r",name:"Room",createdBy:"owner"})'); await tick();
  const calls=[];
  const revoked=[];
  let objectCount=0, finishImage;
  context.URL={createObjectURL:()=>`blob:preview-${++objectCount}`, revokeObjectURL:url=>revoked.push(url)};
  const imageStates={photo:"UPLOADED", empty:"COMPLETED", missing:"COMPLETED", broken:"COMPLETED", late:"UPLOADED", leaving:"UPLOADED"};
  const imageResults={
    photo:{pages:["원문"],pageDetails:[{language:"ko"}]},
    empty:{pages:[""],needsOcr:true,pageDetails:[{}]},
    missing:{pages:[""],needsOcr:true,pageDetails:[{error:"OCR_NOT_INSTALLED"}]},
    broken:{pages:[""],needsOcr:true,pageDetails:[{error:"OCR_FAILED"}]},
  };
  const result={text:'저장 요약 [1]',evidence:[{id:1,text:'original',sentAt:0,source:{kind:'message',messageSeq:1}}]};
  const sources=[{kind:'message',messageSeq:1}];
  const stored={jobId:'saved',entryId:'saved',title:'저장 요약',requester:'owner',status:'COMPLETED',kind:'meeting-summary',target:'ko',attempts:1,result};
  const summaries=[{jobId:'saved',entryId:'saved',kind:'meeting-summary',title:'저장 요약',target:'ko',requester:'owner',createdAt:1,updatedAt:1}];
  let reuse=false;
  context.fetch=async(url,options={})=>{
    calls.push({url,method:options.method||'GET',body:options.body&&JSON.parse(options.body),headers:options.headers});
    if(url==='/api/users/me/ai-consent') return response({allowed:true});
    if(url==='/api/ai/rooms/r/summaries') return response(summaries);
    if(url==='/api/ai/rooms/r/summaries/saved') return response({entryId:'saved',title:'저장 요약',result,sources});
    if(url==='/api/rooms/r/events/suggest') return response({suggestions:[]});
    if(url==='/api/ai/jobs') return response(reuse ? {...stored, existing:true, cached:true} : {jobId:'saved'});
    if(url==='/api/ai/jobs/saved') return response(stored);
    if(url==='/api/ai/translate') return response({translated:'원문 번역'});
    if(url==='/api/ai/translate-page') return response({translated:'Translated photo text'});
    const [,id,action]=url.match(/^\/api\/media\/([^/]+)(?:\/([^/]+))?$/)||[];
    if(id in imageStates){
      if(action==='original') return id==='late' ? new Promise(resolve=>finishImage=resolve) : response({});
      if(action==='process') imageStates[id]='PENDING';
      if(action==='transcript') return response(imageResults[id]);
      return response({status:imageStates[id],progress:0});
    }
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
  const finder=savedItem.children.at(-1);
  assert.equal(finder.hidden,false);
  await finder.children[0].children[0].onclick();
  assert.deepEqual(calls.find(c=>c.url==='/api/rooms/r/events/suggest').body.sources,sources);

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

  const card=id=>evaluate(`fileCard({mediaId:${JSON.stringify(id)},filename:"photo.png",ext:"png",size:3},"owner")`);
  const button=(box,label)=>box.children.find(node=>node.className==='file-act').children.find(node=>node.textContent===label);
  const photo=card('photo'); await tick();
  const preview=photo.children.find(node=>node.className==='file-img-wrap');
  const image=preview.children[0];
  assert.ok(image.src.startsWith('blob:'));
  const originalUrl=image.src;
  image.onload();
  assert.equal(preview.hidden,false);
  assert.equal(calls.find(c=>c.url==='/api/media/photo/original').headers.Authorization,'Bearer owner-token');
  assert.equal(calls.filter(c=>c.url==='/api/media/photo/process').length,0);
  assert.equal(evaluate('filePollers.has("photo")'),false);
  const show=evaluate('t("viewText")'), hide=evaluate('t("hideText")');
  await button(photo,show).onclick();
  assert.equal(calls.filter(c=>c.url==='/api/media/photo/process').length,1);
  assert.equal(evaluate('filePollers.has("photo")'),true);
  evaluate('cards.photo({status:"COMPLETED",progress:100})'); await tick();
  const body=photo.children.find(node=>node.className==='file-text');
  assert.equal(body.hidden,false);
  const translate=body.children.find(node=>node.className==='page-translate');
  await translate.onclick();
  assert.deepEqual(calls.find(c=>c.url==='/api/ai/translate-page').body,{mediaId:'photo',page:1,target:evaluate('targetLang')});
  assert.equal(body.querySelector('.translation[data-page="1"]').children[1].textContent,'Translated photo text');
  await translate.onclick();
  assert.equal(body.querySelector('.translation[data-page="1"]'),null);
  await button(photo,hide).onclick();
  await button(photo,show).onclick();
  assert.equal(calls.filter(c=>c.url==='/api/media/photo/transcript').length,1);
  assert.equal(calls.filter(c=>c.url==='/api/media/photo/process').length,1);
  evaluate('cards.photo({status:"DELETED"}); cards.photo({status:"COMPLETED"})');
  assert.equal(preview.hidden,true);
  assert.equal(body.hidden,true);
  assert.deepEqual(revoked,[originalUrl]);

  for(const [id,key] of [['empty','ocrNoText'],['missing','fe_OCR_NOT_INSTALLED'],['broken','fe_OCR_FAILED']]){
    const box=card(id); await tick();
    await button(box,show).onclick();
    const content=box.children.find(node=>node.className==='file-text');
    assert.equal(content.children[0].textContent,evaluate(`t("${key}")`));
    assert.equal(content.querySelector('.translation'),null);
    assert.equal(content.children[1].className,'file-ocr-options');
    assert.ok(!content.children[1].open);
  }
  const leaving=card('leaving'), late=card('late'); await tick();
  const leavingPreview=leaving.children.find(node=>node.className==='file-img-wrap');
  leavingPreview.children[0].onload();
  const objectCountBeforeLeave=objectCount;
  evaluate('leaveView()');
  finishImage(response({})); await tick();
  assert.equal(leavingPreview.hidden,true);
  assert.equal(late.children.find(node=>node.className==='file-img-wrap').hidden,true);
  assert.equal(objectCount,objectCountBeforeLeave,'late previews do not acquire an object URL after leaving');
  assert.equal(revoked.length,objectCount);
  evaluate('enterRoom({roomId:"r",name:"Room",createdBy:"owner"})'); await tick();

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
  console.log('Saved-result and image UI checks passed: preview cleanup, on-demand OCR, page translation, explicit evidence translation, and stale panel rejection');
})().catch(e=>{console.error(e);process.exitCode=1;});
