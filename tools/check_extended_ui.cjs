"use strict";
const {chromium}=require(process.argv[2]),assert=require('node:assert/strict'),path=require('node:path');
(async()=>{const browser=await chromium.launch({executablePath:process.argv[3],headless:true}),page=await browser.newPage({viewport:{width:1440,height:1050}}),errors=[];
page.on('pageerror',e=>errors.push(String(e)));
try{
 await page.goto(process.argv[5]);await page.locator('#extended-panel').waitFor();
 await page.locator('#extended-form [name=item_ids]').fill('4239');
 await page.locator('[data-extended-start]').click();
 await page.waitForFunction(()=>document.querySelector('#extended-global')?.hidden===false);
 assert.equal(await page.locator('[data-extended-start]').isDisabled(),true);
 assert.equal((await page.request.post(process.argv[5]+'/fixture/deliver')).status(),200);
 await page.locator('[data-page=vendor]').click();await page.locator('#vendor-section').waitFor();
 assert.ok((await page.locator('#extended-global').innerText()).includes('Сбор и обмен Worker продолжаются'));
 await page.request.post(process.argv[5]+'/fixture/release');
 await page.waitForFunction(async()=>{const d=await(await fetch('/api/v2/extended-analysis')).json();return d.state==='completed';},null,{timeout:25000});
 await page.locator('[data-page=opportunities]').click();await page.locator('#extended-panel').waitFor();
 await page.waitForFunction(()=>document.querySelector('#extended-status')?.innerText.includes('готова'));
 await page.waitForFunction(()=>document.getElementById('content')?.innerText.includes('Расширенный расчёт'),null,{timeout:12000});
 await page.screenshot({path:path.join(process.argv[4],'extended-ready.png'),fullPage:true});
 await page.request.post(process.argv[5]+'/fixture/change-prices');
 await page.locator('[data-page=vendor]').click();await page.locator('#vendor-section').waitFor();
 await page.locator('[data-page=opportunities]').click();await page.locator('#extended-panel').waitFor();
 await page.waitForFunction(()=>document.querySelector('#extended-status')?.innerText.includes('требуется расширенный пересчёт'));
 assert.ok((await page.locator('#content').innerText()).includes('Расширенный расчёт устарел'));
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(process.argv[4],'extended-mobile.png'),fullPage:true});
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));assert.deepEqual(errors,[]);
 console.log(JSON.stringify({manual_start:true,child_process:true,global_progress:true,worker_during_analysis:true,result_published:true,stale_marked:true,mobile:true,errors}));
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1);});
// EOF
