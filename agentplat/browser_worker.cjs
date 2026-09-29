// Isolated browser process; every remote request uses the Python pinned-IP broker.
const fs = require('fs');
const path = require('path');
const readline = require('readline');
const {promisify} = require('util');
const execFile = promisify(require('child_process').execFile);
const project = path.resolve(__dirname, '..');
const config = JSON.parse(fs.readFileSync(path.join(project,'.agent-runtime','browser-policy.json'),'utf8'));
const {chromium} = require(path.join(config.module_root,'playwright'));
const workspace = fs.realpathSync(process.argv[2]);
const python = process.argv[3];
const emit = result => process.stdout.write('BROWSER_RESULT ' + JSON.stringify(result).replace(/[\u007f-\uffff]/g, c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0')) + '\n');
function localFile(relative) {
  const file = fs.realpathSync(path.resolve(workspace, relative));
  const rel = path.relative(workspace,file);
  if (rel.startsWith('..') || path.isAbsolute(rel) || rel.split(path.sep).some(x=>x.toLowerCase()==='.agent-runtime')) throw Error('Path outside workspace');
  return file;
}
(async()=>{
  const edge = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe', 'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find(p=>fs.existsSync(p));
  const browser = await chromium.launch(edge ? {executablePath:edge,headless:true,chromiumSandbox:true} : {channel:'msedge',headless:true,chromiumSandbox:true});
  const context = await browser.newContext({serviceWorkers:'block',acceptDownloads:false});
  let preview = false, blocked = [];
  await context.route('**/*', async route=>{
    try {
      if(route.request().method() !== 'GET') throw Error('Only GET requests are permitted');
      const url = new URL(route.request().url());
      if(url.hostname==='agent-preview.invalid' && preview) {
        const file = localFile(decodeURIComponent(url.pathname).replace(/^\//,''));
        const types={'.html':'text/html','.htm':'text/html','.js':'application/javascript','.css':'text/css','.png':'image/png','.jpg':'image/jpeg','.svg':'image/svg+xml'};
        if(fs.statSync(file).size>2000000) throw Error('Preview file too large');
        await route.fulfill({status:200,body:fs.readFileSync(file),contentType:types[path.extname(file)]||'application/octet-stream'});
      } else {
        const result=await execFile(python,['-m','agentplat.browser_fetch',workspace,url.href],{cwd:project,windowsHide:true,timeout:12000,maxBuffer:3000000});
        const source=JSON.parse(result.stdout);
        if(source.error) throw Error(source.error);
        await route.fulfill({status:200,body:Buffer.from(source.body,'base64'),contentType:source.content_type||'application/octet-stream'});
      }
    } catch(error) { blocked.push(error.cmd ? 'BROKER_ERROR: transport process failed or timed out' : String(error.message).slice(0,1000)); await route.abort(); }
  });
  await context.routeWebSocket('**/*', socket=>socket.close());
  const page=await context.newPage(); page.setDefaultTimeout(15000);
  const input=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
  for await(const line of input) {
    try {
      const args=JSON.parse(line); blocked=[];
      if(args.action==='open') {
        const url=new URL(args.url);
        if(!['http:','https:'].includes(url.protocol)) throw Error('Only HTTP(S) URLs allowed');
        await page.goto(url.href,{waitUntil:'domcontentloaded'});
      } else if(args.action==='preview') {
        const file=localFile(args.path);
        if(!['.html','.htm'].includes(path.extname(file))) throw Error('Preview requires HTML');
        preview=true;
        await page.goto('https://agent-preview.invalid/'+path.relative(workspace,file).split(path.sep).map(encodeURIComponent).join('/'),{waitUntil:'domcontentloaded'});
      } else if(args.action==='check') {
        if(!args.expected_text || args.expected_text.length>2000) throw Error('Expected text must be nonempty and at most 2000 characters');
        const target=page.locator(args.selector);await target.waitFor({state:'visible'});
        const deadline=Date.now()+10000;let actual='';
        const matches=()=>args.exact===false?actual.includes(args.expected_text):actual.trim()===args.expected_text.trim();
        do {actual=await target.innerText();if(matches())break;await page.waitForTimeout(100);} while(Date.now()<deadline);
        if(!matches()) throw Error('Browser assertion failed: '+actual.slice(0,1000));
        emit({matched:true,url:page.url(),selector:args.selector,expected_text:args.expected_text,actual:actual.slice(0,2000)});continue;
      } else if(args.action==='fill') await page.locator(args.selector).fill(args.text);
      else if(args.action==='click') {
        const href=await page.locator(args.selector).evaluate(e=>e.closest('a')?.href||'');
        if(href && !['http:','https:'].includes(new URL(href).protocol)) throw Error('Non-HTTP navigation denied');
        await page.locator(args.selector).click();
      } else if(args.action==='screenshot') {
        const folder=path.join(workspace,'.browser'); fs.mkdirSync(folder,{recursive:true});
        localFile('.browser');
        const output=path.join(folder,require('crypto').randomUUID()+'.png');
        await page.screenshot({path:output,fullPage:true}); emit({path:output}); continue;
      } else if(args.action!=='snapshot') throw Error('Unknown action');
      emit({url:page.url(),title:await page.title(),text:(await page.locator('body').innerText()).slice(0,20000),
            links:await page.locator('a').evaluateAll(xs=>xs.slice(0,100).map(x=>({text:x.innerText,url:x.href}))),blocked_requests:blocked.slice(0,20),untrusted_reference:true});
    } catch(error) { emit({error:blocked.length ? blocked.join('; ').slice(0,3000) : error.message,blocked_requests:blocked.slice(0,20)}); }
  }
  await browser.close();
})().catch(error=>{emit({error:error.message});process.exitCode=1;});
