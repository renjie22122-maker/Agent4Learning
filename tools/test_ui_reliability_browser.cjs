const fs=require('fs'),http=require('http'),path=require('path'),assert=require('assert');
const policy=JSON.parse(fs.readFileSync('.agent-runtime/browser-policy.json','utf8'));
const {chromium}=require(path.join(policy.module_root,'playwright'));
(async()=>{
 const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));let dead=false,connections=0;const clients=new Set();
 const server=http.createServer((req,res)=>{
  const url=new URL(req.url,'http://localhost');
  if(url.pathname.startsWith('/ui-assets/')){
   const root=path.resolve('agentplat/static'),file=path.resolve(root,url.pathname.slice(11));
   if(!file.startsWith(root+path.sep)||!fs.existsSync(file)){res.writeHead(404).end();return;}
   res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'application/octet-stream');res.end(fs.readFileSync(file));return;
  }
  if(url.pathname==='/api/attention'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({pending:[{id:'q1',session:'other',kind:'question'}]}));return;}
  if(url.pathname==='/api/human-input'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({questions:url.searchParams.get('session')==='other'?[{id:'q1',kind:'question',status:'pending',created:1,payload:{question:'Choose a format',options:['JSON','CSV']}}]:[]}));return;}
  if(url.pathname==='/api/agent-events'){
   connections++;if(dead){res.writeHead(503).end();return;}
   res.writeHead(200,{'Content-Type':'text/event-stream'});clients.add(res);req.on('close',()=>clients.delete(res));
   res.write('event: progress\ndata: '+JSON.stringify({html:fixture.thread,status:'running'})+'\n\n');return;
  }
  res.setHeader('Content-Type','text/html;charset=utf-8');res.end(fixture.page.replaceAll('FIXTURE_SESSION',url.searchParams.get('session')||'fixture'));
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 let browser;try{browser=await chromium.launch({channel:'msedge',headless:true});}catch(e){server.close();throw e;}const page=await browser.newPage({viewport:{width:1280,height:850}});
 const errors=[];page.on('pageerror',e=>{errors.push(e.message);console.error('PAGE',e.message)});page.on('response',r=>{if(r.status()>=400)console.error('HTTP',r.status(),r.url());});
 try{
  await page.goto('http://127.0.0.1:'+server.address().port+'/agent?session=fixture');
  await page.locator('.katex').first().waitFor();assert.equal(await page.locator('.katex').count(),3);
  assert.equal(await page.locator('.hljs').count(),1);
  assert.equal(await page.locator('.file-diff').count(),1);await page.locator('.step-group>summary').click();await page.locator('.file-diff>summary').click();
  assert((await page.locator('.diff-add').textContent()).includes('new'));
  const elapsed=await page.locator('.turn-elapsed').textContent();await page.waitForTimeout(1100);assert.notEqual(await page.locator('.turn-elapsed').textContent(),elapsed);
  dead=true;for(const client of clients)client.end();
  await page.getByText('连接中断：自动重连 5 次未成功。',{exact:false}).waitFor({timeout:45000});
  assert.equal(connections,6);
  dead=false;await page.getByRole('button',{name:'重新连接',exact:true}).click();await page.getByText('实时连接正常',{exact:true}).waitFor();
  await page.locator('.attention-center summary').click();await page.locator('.attention-items a').click();
  await page.locator('#input-q1').waitFor();assert.equal(await page.locator('#input-q1').getAttribute('open'),'');
  await page.screenshot({path:path.join(path.dirname(process.argv[2]),'legacy-reliability.png'),fullPage:true});
  assert.deepEqual(errors,[]);
  console.log('PASS legacy browser: math, highlight, live timer, diff, exactly five reconnects, manual reconnect, other conversation anchor');
 }finally{await browser.close();for(const c of clients)c.destroy();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});