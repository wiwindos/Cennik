"use strict";
const {chromium}=require(process.argv[2]);
const assert=require('node:assert/strict');
const path=require('node:path');
(async()=>{
  const browser=await chromium.launch({executablePath:process.argv[3],headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  try{
    await page.goto(process.argv[5]);
    await page.locator('#profession-filter').waitFor();
    assert.deepEqual(await page.locator('#profession-filter option').evaluateAll(rows=>rows.map(x=>x.value)),['','Alchemy','Tailoring']);
    await page.locator('#profession-filter').selectOption('Tailoring');
    await page.getByText('1–30 из 32',{exact:true}).waitFor();
    await page.locator('[data-action="next"]').click();
    await page.getByText('31–32 из 32',{exact:true}).waitFor();
    await page.locator('#profession-filter').selectOption('Alchemy');
    await page.getByText('1–4 из 4',{exact:true}).waitFor();
    assert.equal(await page.locator('[data-opportunity]').count(),4);
    assert.ok((await page.locator('.table-wrap').first().innerText()).includes('Алхимия'));
    await page.locator('#strategy-filter').selectOption('craft_disenchant');
    await page.getByText('1–2 из 2',{exact:true}).waitFor();
    await page.locator('[data-bulk-action="first"]').click();
    await page.waitForFunction(()=>document.querySelector('#bulk-selection-count')?.textContent==='2');
    const selected=await page.evaluate(()=>[...window.CennikBulk.selected]);
    assert.deepEqual(selected.sort(),['32','35']);
    await page.screenshot({path:path.join(process.argv[4],'profession-filter.png'),fullPage:true});
    await page.locator('#strategy-filter').selectOption('vendor_arbitrage');
    await page.getByText('1–1 из 1',{exact:true}).waitFor();
    assert.equal(await page.locator('#profession-filter').count(),0);
    await page.locator('#strategy-filter').selectOption('');
    await page.locator('#profession-filter').waitFor();
    assert.equal(await page.locator('#profession-filter').inputValue(),'');
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({profession_filter:'passed',pagination:'passed',strategy_combination:'passed',bulk_selection:'passed'}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
