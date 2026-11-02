const {chromium}=require(process.argv[2]);
const assert=require('node:assert/strict'),path=require('node:path');
(async()=>{
  const browser=await chromium.launch({executablePath:process.argv[3],headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  page.on('response',r=>{if(r.status()>=400)errors.push(r.status()+' '+r.url());});
  try{
    await page.goto(process.argv[5]);
    assert.equal(await page.locator('[data-action="calculate"]').count(),0);
    await page.locator('[data-opportunity="joint"]').click();
    await page.locator('#batches').fill('2');
    await page.getByRole('button',{name:'Рассчитать по объёму',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('[data-shared-craft-plan] tbody tr:last-child td:nth-child(2)')?.textContent==='2');
    const rows=page.locator('[data-shared-craft-plan] tbody tr');
    assert.equal(await rows.nth(0).locator('td').nth(1).textContent(),'1');
    assert.equal(await rows.nth(0).locator('td').nth(4).textContent(),'1');
    assert.equal(await rows.nth(1).locator('td').nth(1).textContent(),'2');
    await page.screenshot({path:path.join(process.argv[4],'plan-1440.png'),fullPage:true});
    await page.locator('nav [data-page="bulk"]').click();
    await page.locator('[data-bulk-quantity="joint"]').fill('2');
    await page.locator('[data-bulk-quantity="joint"]').press('Tab');
    await page.getByRole('button',{name:'Уточнить выбранные объёмы',exact:true}).click();
    await page.waitForFunction(()=>window.CennikBulk.detail?.items[0]?.batches===2);
    const item=await page.evaluate(()=>window.CennikBulk.detail.items[0]);
    assert.equal(item.cost_copper,30);assert.equal(item.available_batches,2);
    assert.deepEqual(item.graph.steps.map(s=>s.batches),[1,2]);
    await page.locator('details').filter({has:page.getByText('Состав закупки и ограничения',{exact:true})}).locator('summary').click();
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1100});
      await page.screenshot({path:path.join(process.argv[4],`bulk-${width}.png`),fullPage:true});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    }
    await page.locator('[data-bulk-choice="joint"]').check();
    await page.getByRole('button',{name:'Передать выбранные в рабочие задания',exact:true}).click();
    await page.waitForFunction(async()=>{const o=await(await fetch('/api/v2/overview')).json();return o.jobs.some(j=>j.kind==='bulk_publish')&&!o.jobs.some(j=>['queued','running'].includes(j.state));});
    await page.waitForFunction(async()=>{const p=await(await fetch('/fixture/package')).json();return p.packages.length===1;});
    const task=await page.evaluate(async()=>(await(await fetch('/fixture/package')).json()).packages[0].tasks[0]);
    assert.equal(task.lines[0].quantity,3);assert.equal(task.opportunity.quantity,2);
    assert.deepEqual(task.opportunity.details.steps.map(s=>s.batches),[1,2]);
    assert.deepEqual(errors,[]);
    console.log('Shared graph UI passed: whole batch 2 → cost 30 → one intermediate craft → leftover 1 → two final crafts → exact addon task; desktop/mobile');
  }catch(error){
    await page.screenshot({path:path.join(process.argv[4],'failure.png'),fullPage:true});
    console.error(errors);console.error((await page.locator('#message').textContent())||'');
    console.error(await page.locator('[data-shared-craft-plan]').allTextContents());
    console.error(await page.evaluate(async()=>(await(await fetch('/api/v2/overview')).json()).jobs));
    throw error;
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
