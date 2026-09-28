// Real DOM regression: async send, scroll position, cumulative SSE output, errors.
const fs = require('fs');
const http = require('http');
const assert = require('assert');
const {chromium} = require('playwright');
(async () => {
  const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  let reject = false, posted = '', connections = 0,uploads=0;
  const server = http.createServer((req, res) => {
    if (req.method === 'POST') {
      let body='';req.on('data', data => {posted += data;body+=data;});
      req.on('end', () => {
        if(req.url==='/agent/attachments'){
          const value=JSON.parse(body);uploads++;
          res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({id:'a'.repeat(31)+uploads,name:value.name,bytes:20,chunks:1,warnings:[]}));return;
        }
        if(req.url==='/agent/conversation'){
          fixture.page=fixture.page.replaceAll('UI fixture','Renamed fixture');
          res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({ok:true}));return;
        }
        res.writeHead(reject ? 400 : 200, {'Content-Type':'application/json'});res.end(JSON.stringify(reject ? {error:'fixture rejected'} : {session:'fixture'}));
      });
    } else if (req.url.startsWith('/api/agent-events')) {
      connections++;
      res.writeHead(200, {'Content-Type':'text/event-stream'});
      res.write('event: progress\ndata: '+JSON.stringify({status:'running',html:fixture.progress})+'\n\n');
      setTimeout(() => res.end('event: progress\ndata: '+JSON.stringify({status:'done',html:fixture.done})+'\n\n'), 1800);
    } else {res.writeHead(200, {'Content-Type':'text/html; charset=utf-8'});res.end(fixture.page);}
  });
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    browser = await chromium.launch({headless:true,channel:'msedge'});
    const page = await browser.newPage({viewport:{width:1400,height:850}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    let navigations=0;page.on('request',request=>{if(request.resourceType()==='document')navigations++;});
    await page.goto(`http://127.0.0.1:${server.address().port}/agent?session=fixture`);
    await page.locator('#m').fill('additional instruction');
    await page.locator('#scroll').evaluate(el=>el.scrollTop=220);
    const before=await page.locator('#scroll').evaluate(el=>el.scrollTop);
    await page.locator('#m').press('Enter');
    await page.getByText('Second cumulative output',{exact:true}).waitFor();
    assert.strictEqual(navigations,1,'submit navigated');
    assert.strictEqual(await page.locator('#m').inputValue(),'');
    assert.ok(Math.abs((await page.locator('#scroll').evaluate(el=>el.scrollTop))-before)<4,'scroll jumped');
    await page.getByText('First cumulative output',{exact:true}).waitFor();
    await page.getByText('Final conclusion',{exact:true}).waitFor();
    await page.getByText('First cumulative output',{exact:true}).waitFor();
    assert.ok(posted.includes('message=additional+instruction'));
    assert.strictEqual(connections,1);
    reject=true;
    await page.locator('#m').fill('keep this on error');
    await page.locator('#m').press('Enter');
    await page.getByText('fixture rejected',{exact:true}).waitFor();
    assert.strictEqual(await page.locator('#m').inputValue(),'keep this on error');
    await page.locator('.conversation-row').first().click({button:'right'});
    await page.getByRole('menuitem',{name:'重命名',exact:true}).click();
    await page.locator('dialog input').fill('Renamed fixture');
    await page.getByRole('button',{name:'保存',exact:true}).click();
    await page.locator('.conversation-row[data-title="Renamed fixture"]').waitFor();
    assert.strictEqual(await page.locator('#m').inputValue(),'keep this on error','menu action lost draft');
    await page.locator('.conversation-menu-button').first().click();
    await page.getByRole('menuitem',{name:'移入回收站',exact:true}).waitFor();
    await page.keyboard.press('Escape');
    await page.locator('.side-b').evaluate(el=>el.scrollTop=el.scrollHeight);
    const settings=page.locator('.side-tools .tools-heading');
    const settingsBox=await settings.boundingBox();assert.ok(settingsBox.y<160,'settings not fixed at upper left');
    await page.locator('.side-tools').getByText('文档知识库',{exact:true}).waitFor();
    assert.strictEqual(await page.locator('.side-tools details').count(),0,'settings still nested');
    await page.setViewportSize({width:700,height:850});
    await page.locator('.top-tools').waitFor();
    await page.setViewportSize({width:1400,height:850});
    reject=false;
    assert.strictEqual(await page.locator('#attachment-add').textContent(),'＋','SSE changed attachment button');
    await page.locator('#attachment-picker').setInputFiles({name:'notes.txt',mimeType:'text/plain',buffer:Buffer.from('attachment fact')});
    await page.locator('.attachment-chip').filter({hasText:'notes.txt · 已就绪'}).waitFor();
    await page.getByRole('button',{name:'移除 notes.txt',exact:true}).click();
    assert.strictEqual(await page.locator('#attachment-list .attachment-chip').count(),0);
    const transfer=await page.evaluateHandle(()=>{const d=new DataTransfer();d.items.add(new File(['csv fact'],'data.csv',{type:'text/csv'}));return d;});
    await page.locator('#sendform').dispatchEvent('drop',{dataTransfer:transfer});
    await page.locator('.attachment-chip').filter({hasText:'data.csv · 已就绪'}).waitFor();
    await page.evaluate(()=>{const d=new DataTransfer();d.items.add(new File(['png fixture'],'clipboard.png',{type:'image/png'}));document.getElementById('m').dispatchEvent(new ClipboardEvent('paste',{clipboardData:d,bubbles:true,cancelable:true}));});
    await page.locator('.attachment-chip').filter({hasText:'clipboard.png · 已就绪'}).waitFor({timeout:5000}).catch(async error=>{console.error('Attachment UI:',await page.locator('#attachment-list').innerText(),'uploads',uploads);throw error;});
    await page.locator('#m').fill('');await page.locator('button.send').click();
    await page.waitForFunction(()=>document.querySelector('#attachment-list').children.length===0);
    assert.ok(posted.includes('attachments=%5B%22'),'attachment IDs missing from message');
    assert.deepStrictEqual(errors,[]);
    await page.screenshot({path:process.argv[2]+'.png'});
    console.log(JSON.stringify({async_send:true,scroll_preserved:true,cumulative_output:true,context_menu:true,direct_settings:true,file_picker:true,file_drop:true,image_paste:true,attachment_only_send:true}));
  } finally {if(browser)await browser.close();server.closeAllConnections();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
