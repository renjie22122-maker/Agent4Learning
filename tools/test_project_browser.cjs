const fs=require('fs'),http=require('http'),assert=require('assert');
const {chromium}=require('playwright');
(async()=>{
 const fixture=JSON.parse(fs.readFileSync('.diagnostics/project-ui.json','utf8'));
 let posted;
 const server=http.createServer((req,res)=>{
  if(req.method==='POST') {let body='';req.on('data',d=>body+=d);req.on('end',()=>{posted=new URLSearchParams(body);res.setHeader('Content-Type','application/json');res.end(JSON.stringify(req.url==='/workspaces/pick'?{paths:['D:\\app','D:\\docs']}:{redirect:'/saved'}));});return;}
  res.setHeader('Content-Type','text/html; charset=utf-8');res.end(req.url==='/permissions'?fixture.permissions:fixture.project);
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 let browser;
 try {
  browser=await chromium.launch({headless:true,channel:'msedge'});const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  // Exercise Chinese controls explicitly; test_ui_language_browser covers English default.
  await page.addInitScript(()=>localStorage.setItem('agent-ui-language','zh'));
  const base='http://127.0.0.1:'+server.address().port;await page.goto(base);
  await page.locator('#native-folders').click();await page.waitForFunction(()=>document.querySelectorAll('.project-folder').length===2);
  assert.equal(await page.locator('#project-paths').inputValue(),'D:\\app\nD:\\docs');
  await page.locator('#project-paths').fill('D:\\source\nD:\\assets');
  assert.equal(await page.locator('.project-folder').count(),2);
  await page.locator('#project-form [type=submit]').click();await page.waitForURL('**/saved');
  assert.deepEqual(JSON.parse(posted.get('folders_json')),['D:\\source','D:\\assets']);
  await page.goto(base+'/permissions');assert.equal(await page.locator('[name=value][type=radio]').count(),3);
  assert.deepEqual(errors,[]);console.log('PASS: multi-folder chooser response, path input, save payload, three web modes, no JS errors');
 }finally{if(browser)await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exit(1)});
