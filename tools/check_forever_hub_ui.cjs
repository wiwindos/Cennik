const {chromium}=require(process.argv[2]);
const assert=require('node:assert/strict'),path=require('node:path'),fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({executablePath:process.argv[3],headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1050}}),errors=[];
 const out=process.argv[4],origin=process.argv[5],profile=process.argv[6];
 page.on('pageerror',e=>errors.push(String(e)));
 page.on('response',r=>{if(r.status()>=400)errors.push(r.status()+' '+r.url());});
 try{
  await page.goto(origin+'/login');
  await page.locator('[name=username]').fill('owner');await page.locator('[name=password]').fill('hub-ui-password-123');
  await page.locator('#login-form button').click();
  await page.locator('.module-card').filter({hasText:'Cennik'}).waitFor();
  assert.equal(await page.locator('.module-card').count(),2);
  await page.screenshot({path:path.join(out,'hub-desktop.png'),fullPage:true});
  await page.locator('.module-card').filter({hasText:'Cennik'}).click();
  await page.getByRole('heading',{name:'Серверы и подключения',exact:true}).waitFor();
  await page.getByRole('link',{name:'Открыть полный Cennik',exact:true}).click();
  await page.getByRole('heading',{name:'Возможности',exact:true}).waitFor();
  await page.locator('[data-opportunity]').first().waitFor();
  assert.match(await page.locator('.beta-notice').innerText(),/Расчёт по бета-ценам/);
  assert.match(await page.locator('.beta-notice').innerText(),/forever.pvp.alliance.us/);
  const mainPages=['opportunities','bulk','sniper','market','archive','execution','accounting','production','leveling','collectors','sources','settings'];
  for(const name of mainPages){
   await page.locator('nav [data-page="'+name+'"]').click();
   await page.waitForFunction(n=>document.querySelector('nav [data-page="'+n+'"]')?.hasAttribute('aria-current'),name);
   await page.locator('#content h1').first().waitFor();
   await page.waitForTimeout(100);
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'overflow '+name);
  }
  await page.locator('nav [data-page="opportunities"]').click();
  await page.locator('[data-opportunity]').first().waitFor();
  await page.locator('[data-bulk-select]').first().check();
  await page.locator('[data-bulk-action=send]').click();
  await page.getByRole('heading',{name:'Массовая проверка',exact:true}).waitFor();
  await page.waitForFunction(()=>window.CennikBulk.batch!==null);
  await page.screenshot({path:path.join(out,'bulk-desktop.png'),fullPage:true});
  for(const width of [1440,390]){
   await page.setViewportSize({width,height:1050});
   await page.locator('nav [data-page="opportunities"]').click();await page.locator('[data-opportunity]').first().waitFor();
   await page.screenshot({path:path.join(out,'opportunities-'+width+'.png'),fullPage:true});
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await page.locator('nav [data-page="sources"]').click();await page.locator('#upstream-settings').waitFor();
   await page.screenshot({path:path.join(out,'sources-'+width+'.png'),fullPage:true});
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  }
  await page.setViewportSize({width:1440,height:1050});
  await page.locator('.theme-toggle').click();assert(await page.locator('body').evaluate(e=>e.classList.contains('light')));
  await page.screenshot({path:path.join(out,'sources-light.png'),fullPage:true});
  await page.locator('nav [data-page="opportunities"]').click();await page.locator('#own-data-mode').waitFor();
  await page.locator('#own-data-mode').check();
  await page.waitForFunction(async id=>{const d=await(await fetch('/cennik/m/'+id+'/api/v2/overview')).json();return d.settings.own_data_only&&d.analytics.settings_match&&!d.analytics.running;},profile,{timeout:30000});
  const own=await page.evaluate(async id=>(await(await fetch('/cennik/m/'+id+'/api/v2/overview')).json()),profile);
  assert(own.analytics.opportunity_count>0&&own.analytics.beta_baseline_active);
  await page.getByRole('link',{name:'← WIW Hub',exact:true}).click();await page.locator('.module-card').first().waitFor();
  assert(await page.locator('body').evaluate(e=>e.classList.contains('light')));
  assert.deepEqual(errors,[]);
  const result={hub_single_login:true,pages:mainPages,bulk_from_beta:true,own_mode_automatic_calculation:true,theme_shared:true,desktop_mobile:true,js_http_errors:errors};
  fs.writeFileSync(path.join(out,'result.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
 }catch(e){await page.screenshot({path:path.join(out,'failure.png'),fullPage:true});console.error(errors);console.error(await page.locator('body').innerText());throw e;}
 finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
