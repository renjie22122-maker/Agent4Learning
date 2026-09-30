// Browser regression: default, persistence, dynamic UI, protected content, injection.
const fs=require('fs'),path=require('path'),http=require('http'),assert=require('assert');
const policy=JSON.parse(fs.readFileSync('.agent-runtime/browser-policy.json','utf8'));
const {chromium}=require(path.join(policy.module_root,'playwright'));
(async()=>{
 const html=fs.readFileSync(process.argv[2],'utf8');
 const server=http.createServer((req,res)=>{res.setHeader('Content-Type','text/html; charset=utf-8');res.end(html)});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));let browser;
 try{
  browser=await chromium.launch({channel:'msedge',headless:true});
  const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const url=`http://127.0.0.1:${server.address().port}/`;
  await page.goto(url);await page.getByRole('button',{name:'Copy reply',exact:true}).waitFor();
  assert.equal(await page.locator('html').getAttribute('lang'),'en');
  for(const selector of ['#answer','#title','#code','#question','#table'])assert.equal(await page.locator(selector).textContent(),'复制回复');
  assert.equal(await page.locator('#draft').inputValue(),'复制回复');
  await page.locator('#ui-language').selectOption('zh');
  await page.getByRole('button',{name:'复制回复',exact:true}).waitFor();
  await page.reload();assert.equal(await page.locator('#ui-language').inputValue(),'zh');
  await page.locator('#ui-language').selectOption('en');
  await page.evaluate(()=>{const b=document.createElement('button');b.id='dynamic';b.textContent='复制代码';document.body.appendChild(b)});
  await page.getByRole('button',{name:'Copy code',exact:true}).waitFor();
  await page.locator('#ui-language').selectOption('zh');assert.equal(await page.locator('#dynamic').textContent(),'复制代码');
  await page.locator('#ui-language').selectOption('en');assert.equal(await page.locator('#dynamic').textContent(),'Copy code');
  assert.deepEqual(errors,[]);console.log('PASS: English default, Chinese switch, persistence, dynamic controls, protected chat/code/data, no JS errors');
 }finally{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve))}
})().catch(e=>{console.error(e);process.exitCode=1});
