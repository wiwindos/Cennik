"use strict";
const {chromium}=require(process.argv[2]);
const assert=require('node:assert/strict'),path=require('node:path');
(async()=>{
  const browser=await chromium.launch({executablePath:process.argv[3],headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  page.on('response',r=>{if(r.status()>=400)errors.push(r.status()+' '+r.url());});
  const data=async path=>page.evaluate(async p=>await(await fetch('/api/v2/'+p)).json(),path);
  const settled=async()=>page.waitForFunction(async()=>{const o=await(await fetch('/api/v2/overview')).json();return !o.jobs.some(j=>['running','queued'].includes(j.state));},null,{timeout:30000});
  try{
    await page.goto('http://127.0.0.1:8767');
    await page.locator('nav [data-page="production"]').click();
    await page.getByRole('heading',{name:'Производство',exact:true}).waitFor();
    await page.locator('[data-prod-task]').check();
    await page.getByRole('button',{name:'Создать производственный заказ',exact:true}).click();
    await page.waitForFunction(async()=>{const d=await(await fetch('/api/v2/production')).json();return d.orders.length===1;});
    await settled();
    await page.getByRole('button',{name:'Обновить',exact:true}).click();
    await page.getByRole('heading',{name:/Заказ /}).waitFor();
    let d=await data('production');assert.equal(d.orders[0].steps[0].crafts,2);
    assert.equal(d.orders[0].transfers[0].lines[0].quantity,6);
    await page.evaluate(async()=>{const r=await fetch('/fixture/production',{method:'POST'});if(!r.ok)throw Error(await r.text());});
    await page.getByRole('button',{name:'Синхронизировать оба окна',exact:true}).click();
    await settled();await page.getByRole('button',{name:'Обновить',exact:true}).click();
    await page.getByText('Получение подтверждено обеими сторонами',{exact:false}).first().waitFor();
    assert.equal(await page.getByText('Получение подтверждено обеими сторонами',{exact:false}).count(),2);
    d=await data('production');assert.equal(d.orders[0].steps[0].done,2);
    let ledger=await data('accounting');assert.equal(ledger.summary.known_profit_copper,40);assert.equal(ledger.summary.inventory_cost_copper,30);
    // The form retains stable addressing identities across saves.
    await page.getByText('Аккаунты и роли',{exact:true}).click();
    await page.getByRole('button',{name:'Сохранить аккаунты',exact:true}).click();
    await page.getByRole('heading',{name:'Производство',exact:true}).waitFor();
    assert.deepEqual((await data('production')).clients.map(c=>c.id),d.clients.map(c=>c.id));
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1100});
      await page.screenshot({path:path.join(process.argv[4],`production-${width}.png`),fullPage:true});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Overflow '+width);
    }
    await page.getByRole('button',{name:'Синхронизировать оба окна',exact:true}).click();await settled();
    ledger=await data('accounting');assert.equal(ledger.summary.known_profit_copper,40);
    await page.getByRole('button',{name:'Архивировать заказ, сохранив остатки и историю',exact:true}).click();
    await page.getByRole('heading',{name:/Заказ .*архив/}).waitFor();
    assert.deepEqual(errors,[]);
    console.log('Production UI end-to-end passed: ordinary task → order → two routed clients → mail → craft → trade return → partial sale → both SavedVariables → confirmed hand-offs/FIFO profit → repeat sync → archive; 1440/390');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
