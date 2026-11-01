"use strict";
const {chromium}=require(process.argv[2]);
const assert=require('node:assert/strict'),path=require('node:path');
(async()=>{
  const browser=await chromium.launch({executablePath:process.argv[3],headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[];
  page.on('pageerror',e=>errors.push(String(e)));page.on('response',r=>{if(r.status()>=400)errors.push(r.status()+' '+r.url());});
  try{
    await page.goto('http://127.0.0.1:8767');
    await page.locator('[data-page="accounting"]').click();
    await page.getByRole('heading',{name:'История операций',exact:true}).waitFor();
    await page.evaluate(async()=>{const r=await fetch('/fixture/capture',{method:'POST'});if(!r.ok)throw Error(await r.text());});
    await page.getByRole('button',{name:'Загрузить из игры',exact:true}).click();
    await page.locator('[data-account-profit="65"]').waitFor({timeout:30000});
    let data=await page.evaluate(async()=>await(await fetch('/api/v2/accounting')).json());
    assert.equal(data.total,5);assert.equal(data.summary.inventory_cost_copper,30);assert.equal(data.summary.cash_flow_copper,30);
    // A correction changes downstream craft/sale costs without deleting the purchase.
    const purchase=data.events.find(e=>e.kind==='purchase');
    await page.locator(`[data-account-edit="${purchase.event_id}"]`).click();
    await page.locator('#account-edit-form [name="money_copper"]').fill('90');
    await page.locator('#account-edit-form [name="reason"]').fill('Проверена фактическая стоимость покупки');
    await page.locator('#account-edit-form button.primary').click();
    await page.locator('[data-account-profit="50"]').waitFor();
    data=await page.evaluate(async()=>await(await fetch('/api/v2/accounting')).json());
    assert.equal(data.summary.inventory_cost_copper,45);assert.equal(data.total,5);
    const audit=await page.evaluate(async id=>await(await fetch('/api/v2/accounting/events/'+id)).json(),purchase.event_id);
    assert.equal(audit.original.money_copper,60);assert.equal(audit.revisions.length,1);
    // Add a legacy opening stock through the normal form.
    await page.getByRole('button',{name:'Добавить пропущенную операцию',exact:true}).click();
    await page.locator('#account-edit-form [name="character"]').fill('FixturePlayer');
    await page.locator('#account-edit-form [name="realm"]').fill('FixtureRealm');
    await page.locator('#account-edit-form [data-field="item_id"]').fill('99999');
    await page.locator('#account-edit-form [data-field="quantity"]').fill('2');
    await page.locator('#account-edit-form [name="money_copper"]').fill('40');
    await page.locator('#account-edit-form [name="reason"]').fill('Стоимость начальных запасов');
    await page.locator('#account-edit-form button.primary').click();
    await page.waitForFunction(async()=>{const d=await(await fetch('/api/v2/accounting')).json();return d.total===6;},null,{timeout:30000});
    await page.locator('#account-filter [name="kind"]').selectOption('craft');
    await page.locator('#account-filter button').click();
    await page.getByText('Найдено 2;', {exact:false}).waitFor();
    await page.locator('#account-filter [name="kind"]').selectOption('');await page.locator('#account-filter button').click();
    await page.getByText('Найдено 6;',{exact:false}).waitFor();
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1100});
      await page.screenshot({path:path.join(process.argv[4],`accounting-${width}.png`),fullPage:true});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Overflow '+width);
    }
    await page.getByRole('button',{name:'Загрузить из игры',exact:true}).click();
    await page.waitForFunction(async()=>{const o=await(await fetch('/api/v2/overview')).json();return !o.jobs.some(j=>['running','queued'].includes(j.state));},null,{timeout:30000});
    data=await page.evaluate(async()=>await(await fetch('/api/v2/accounting')).json());
    assert.equal(data.total,6);assert.equal(data.summary.inventory_cost_copper,85);assert.equal(data.summary.known_profit_copper,50);
    assert.deepEqual(errors,[]);
    console.log('Accounting end-to-end passed: automatic vendor → crafts → AH listing → partial sale mail → SavedVariables → sync → FIFO profit → audited correction; 1440/390');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
