"use strict";
const {chromium}=require(process.argv[2]);
const assert=require('node:assert/strict'),path=require('node:path');
(async()=>{
  const browser=await chromium.launch({executablePath:process.argv[3],headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[];
  page.on('pageerror',e=>errors.push(String(e)));page.on('response',r=>{if(r.status()>=400)errors.push(r.status()+' '+r.url());});
  const settled=async()=>page.waitForFunction(async()=>{const d=await(await fetch('/api/v2/overview')).json();return !d.jobs.some(j=>['running','queued'].includes(j.state));},null,{timeout:30000});
  try{
    await page.goto('http://127.0.0.1:8767');await page.locator('nav [data-page="collectors"]').click();
    await page.getByRole('heading',{name:'Сборщики',exact:true}).waitFor();
    const keys=await page.evaluate(async()=>await(await fetch('/fixture/keys')).json());
    await page.locator('#collector-connection [name="enabled"]').check();
    await page.locator('#collector-connection [name="url"]').fill('http://127.0.0.1:8768');
    await page.locator('#collector-connection [name="token"]').fill(keys.token);
    await page.locator('#collector-connection [name="admin_token"]').fill(keys.admin);
    await page.getByRole('button',{name:'Сохранить подключение',exact:true}).click();
    await page.getByRole('heading',{name:'Участники сервера',exact:true}).waitFor();
    await page.evaluate(async()=>{const r=await fetch('/fixture/proposal',{method:'POST'});if(!r.ok)throw Error(await r.text());});
    await page.getByRole('button',{name:'Загрузить сохранённое из игры',exact:true}).click();await settled();
    await page.getByRole('button',{name:'Отправить очередь',exact:true}).click();await settled();
    await page.getByRole('button',{name:'Обновить',exact:true}).click();
    const auctioneer=page.locator('.auctioneer-table tbody tr').filter({hasText:'FixtureAuctioneer'});
    await auctioneer.getByText('Ожидает проверки',{exact:true}).waitFor();
    assert((await auctioneer.innerText()).includes('50.0, 60.0'));
    assert.equal(await auctioneer.locator('select').inputValue(),'neutral');
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1100});
      await page.screenshot({path:path.join(process.argv[4],`auctioneers-pending-${width}.png`),fullPage:true});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Overflow '+width);
    }
    await auctioneer.locator('select').selectOption('ordinary');
    await auctioneer.getByRole('button',{name:'Одобрить',exact:true}).click();await settled();
    await page.getByRole('button',{name:'Обновить',exact:true}).click();
    await auctioneer.getByText('Подтверждено: Обычный',{exact:true}).waitFor();
    await page.evaluate(async()=>{const r=await fetch('/fixture/game',{method:'POST'});if(!r.ok)throw Error(await r.text());});
    await page.getByRole('button',{name:'Загрузить сохранённое из игры',exact:true}).click();await settled();
    await page.getByRole('button',{name:'Отправить очередь',exact:true}).click();await settled();
    await page.getByRole('button',{name:'Обновить',exact:true}).click();
    await page.getByText('На связи',{exact:true}).waitFor();
    await page.getByText('SCAN_TIMEOUT',{exact:false}).first().waitFor();
    const d=await page.evaluate(async()=>await(await fetch('/api/v2/collectors/members')).json());
    assert.equal(d.members[0].counts.full_scans,1);assert.equal(d.members[0].counts.rows,1);
    assert.equal(d.members[0].diagnostic.code,'SCAN_TIMEOUT');
    assert(!JSON.stringify(d).includes(keys.token));
    await page.waitForFunction(async()=>{const d=await(await fetch('/api/v2/collectors')).json();return d.pending===0;});
    await page.getByRole('button',{name:'Обновить',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('[data-collector-pending]')?.textContent==='0');
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1100});
      await page.screenshot({path:path.join(process.argv[4],`collectors-${width}.png`),fullPage:true});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Overflow '+width);
    }
    await page.locator('#collector-invite [name="label"]').fill('Другой участник');
    await page.getByRole('button',{name:'Создать приглашение',exact:true}).click();
    await page.locator('#collector-invitation').filter({hasText:'Другой участник'}).waitFor();
    await page.getByRole('button',{name:'Обновить',exact:true}).click();
    await page.getByText('Другой участник',{exact:true}).waitFor();
    const row=page.locator('tr').filter({hasText:'Другой участник'});
    await row.getByRole('button',{name:'Отозвать ключ',exact:true}).click();
    await page.getByText('Отозван',{exact:true}).waitFor();
    assert.deepEqual(errors,[]);
    console.log('Collector UI passed: player proposal → SavedVariables → HTTP upload → owner changes type/approves in web → registry download → addon unlock → delayed scan → metrics/errors → invitation/revoke; 1440/390');
  }catch(e){
    await page.screenshot({path:path.join(process.argv[4],'failure.png'),fullPage:true});
    console.error('Collector UI state:',await page.evaluate(async()=>await(await fetch('/api/v2/collectors')).json()));
    console.error('UI errors:',errors);throw e;
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
