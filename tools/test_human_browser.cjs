const fs=require('fs'),http=require('http'),assert=require('assert'),path=require('path');
const policy=JSON.parse(fs.readFileSync('.agent-runtime/browser-policy.json','utf8'));
const {chromium}=require(path.join(policy.module_root,'playwright'));
(async()=>{
 const html=fs.readFileSync(process.argv[2],'utf8');let posted=[],questions=[{id:'q1',kind:'question',payload:{question:'选择颜色 <img src=x onerror=alert(1)>',options:['蓝色','绿色']},status:'pending',answer:''}];
 const server=http.createServer((req,res)=>{
  if(req.method==='POST'){let body='';req.on('data',d=>body+=d);req.on('end',()=>{posted.push(Object.fromEntries(new URLSearchParams(body)));questions[0].status='answered';questions[0].answer=posted[0].answer;res.setHeader('Content-Type','application/json');res.end(JSON.stringify({notice:'答复已保存',continuing:true}));});}
  else if(req.url.startsWith('/api/human-input')){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({questions}));}
  else{res.setHeader('Content-Type','text/html; charset=utf-8');res.end(html);}
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));let browser;
 try{
  browser=await chromium.launch({headless:true,channel:'msedge'});const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  // Exercise Chinese controls explicitly; test_ui_language_browser covers English default.
  await page.addInitScript(()=>localStorage.setItem('agent-ui-language','zh'));
  await page.goto(`http://127.0.0.1:${server.address().port}/agent?session=fixture`);
  await page.evaluate(()=>{window.resumed=0;window.addEventListener('agent-resumed',()=>window.resumed++);});
  await page.getByRole('button',{name:'绿色',exact:true}).click();assert.equal(await page.locator('textarea').inputValue(),'绿色');assert.equal(await page.locator('img').count(),0);assert.equal(posted.length,0);
  questions.push({id:'q2',kind:'approval',payload:{reason:'子任务申请',command:'python -V',workspace:'fixture'},status:'pending',answer:''});
  await page.getByRole('button',{name:'允许这一次'}).waitFor();assert.equal(await page.locator('textarea').first().inputValue(),'绿色');assert.equal(posted.length,0);
  await page.getByRole('button',{name:'提交回答并继续'}).click();await page.locator('#human-history .hi-answer').filter({hasText:'绿色'}).waitFor();assert.equal(posted.length,1);assert.equal(posted[0].session,'fixture');assert.equal(posted[0].token,'fixture-token');assert.deepEqual(errors,[]);
  assert.equal(await page.evaluate(()=>window.resumed),1);
  const question=await page.locator('#human-history .hi-question').boundingBox();const answer=await page.locator('#human-history .hi-answer').boundingBox();assert(answer.y>question.y+question.height,'Answer must be below question');
  assert.equal(await page.locator('#human-input .hi-card').count(),1);
  await page.screenshot({path:path.join(path.dirname(process.argv[2]),'interaction-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Mobile horizontal overflow');
  await page.screenshot({path:path.join(path.dirname(process.argv[2]),'interaction-mobile.png'),fullPage:true});
  console.log('PASS human chat cards: draft, no auto approval, safe text, one reply, stream resume');
 }finally{if(browser)await browser.close();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);process.exitCode=1;});
